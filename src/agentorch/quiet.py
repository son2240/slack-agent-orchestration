"""Quiet wrapper: keep watching inside one background task, wake only when needed.

A plain watcher exits on every event, including ones the agent cannot act on
(held posts, notices), on every timeout, and when its code changes. Each exit is a
model turn. This wrapper loops the watcher and only returns when:
  * an actionable event arrives (non-actionable events collected so far are attached), or
  * the total budget is nearly used up (background tasks usually have a hard cap,
    e.g. 2 hours, so the default budget stops a little before that).

From the command line each watcher round runs in a fresh subprocess, so a
`code_updated` exit restarts the watcher on the new code without waking the agent.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import watch
from .config import Config

QUIET_TYPES = ("timeout", "code_updated")


def in_process(cfg: Config, client, me: str, interval: float, clock=time.time, sleep=time.sleep):
    def runner(timeout: float) -> dict:
        return watch.run(cfg, client, me, timeout=timeout, interval=interval, clock=clock, sleep=sleep)
    return runner


def subprocess_runner(me: str, interval: float, config_path: str | None = None, python: str = sys.executable):
    src = str(Path(__file__).resolve().parents[1])

    def runner(timeout: float) -> dict:
        cmd = [python, "-m", "agentorch"] + (["--config", config_path] if config_path else []) + \
              ["watch", "--as", me, "--timeout", str(int(timeout)), "--interval", str(interval)]
        env = {**os.environ, "PYTHONPATH": src}
        p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout + 300)
        lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
        try:
            out = json.loads(lines[-1])
        except (IndexError, ValueError):
            return {"type": "error", "agent": me, "exit_code": p.returncode, "stderr": p.stderr[-500:]}
        if p.returncode != 0 and out.get("type") is None:
            out = {"type": "error", "agent": me, "exit_code": p.returncode, **out}
        return out
    return runner


def run(cfg: Config, client, me: str, budget: float = 6900, watch_timeout: float = 1800, interval: float = 20,
        clock=time.time, sleep=time.sleep, runner=None) -> dict:
    runner = runner or in_process(cfg, client, me, interval, clock, sleep)
    start = clock()
    held: list[dict] = []
    while True:
        left = budget - (clock() - start)
        if left <= interval:
            return {"type": "timeout", "agent": me, "held": held}
        out = runner(min(watch_timeout, left - interval))
        if out.get("type") in QUIET_TYPES:
            continue
        if out.get("type") != "events":
            return {**out, "held": held}  # errors and unknown shapes go to the agent (a human should look)
        events = out.get("events") or []
        if any(e.get("kind") in watch.ACTIONABLE for e in events):
            return {**out, "held": held + [e for e in events if e.get("kind") not in watch.ACTIONABLE]}
        held.extend(events)  # non-actionable only: remember, do not wake
