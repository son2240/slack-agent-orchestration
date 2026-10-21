"""Quiet wrapper: keep watching inside one background task, wake only when needed.

A plain watcher exits on every event, including ones the agent cannot act on
(held posts) and on every timeout. Each exit is a model turn. This wrapper loops
the watcher in-process and only returns when:
  * an actionable event arrives (held posts collected so far are attached), or
  * the total budget is nearly used up (background tasks usually have a hard cap,
    e.g. 2 hours, so the default budget stops a little before that).
"""
from __future__ import annotations

import time

from . import watch
from .config import Config


def run(cfg: Config, client, me: str, budget: float = 6900, watch_timeout: float = 1800, interval: float = 20,
        clock=time.time, sleep=time.sleep) -> dict:
    start = clock()
    held: list[dict] = []
    while True:
        left = budget - (clock() - start)
        if left <= interval:
            return {"type": "timeout", "agent": me, "held": held}
        out = watch.run(cfg, client, me, timeout=min(watch_timeout, left - interval), interval=interval,
                        clock=clock, sleep=sleep)
        if out["type"] == "timeout":
            continue
        events = out.get("events") or []
        if any(e.get("kind") in watch.ACTIONABLE for e in events):
            return {**out, "held": held + [e for e in events if e.get("kind") not in watch.ACTIONABLE]}
        held.extend(events)  # non-actionable only: remember, do not wake
