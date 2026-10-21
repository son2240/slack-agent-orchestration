"""Per-agent watcher state, one JSON file per agent.

Only an agent's own watcher writes its file. Posters and other watchers only read.
That split matters: the chain depth a poster attaches is derived from a file the
agent's model cannot choose (the watcher wrote it when it woke the agent).

    state_<agent>.json = {
      "cursors": {"<channel>": "<last seen ts>"},
      "wake": {"cause": "human"|"agent"|"urgent", "at": <epoch>, "from": "<agent or null>"},
      "wakes": [{"at", "from", "to", "urgent"}],     # agent-to-agent wakes this agent received
      "held":  [{"at", "ts", "channel", "from", "reason"}],
      "last_human_at": <epoch>
    }
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

KEEP_WAKES_SEC = 2 * 86400
KEEP_HELD = 200


class Unreadable(Exception):
    """A state file exists but cannot be parsed. Callers treat this fail-closed."""


def path_for(state_dir: Path, agent: str) -> Path:
    return Path(state_dir) / f"state_{agent}.json"


def empty() -> dict:
    return {"cursors": {}, "wake": None, "wakes": [], "held": [], "last_human_at": 0.0}


def read(state_dir: Path, agent: str) -> dict:
    p = path_for(state_dir, agent)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty()
    except (OSError, ValueError) as e:
        raise Unreadable(str(p)) from e
    if not isinstance(d, dict):
        raise Unreadable(str(p))
    return {**empty(), **d}


def write(state_dir: Path, agent: str, data: dict, now: float | None = None) -> None:
    """Atomic replace, pruning old wakes and held entries."""
    now = now or time.time()
    data = dict(data)
    data["wakes"] = [w for w in data.get("wakes") or [] if now - float(w.get("at", 0)) < KEEP_WAKES_SEC]
    data["held"] = (data.get("held") or [])[-KEEP_HELD:]
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=state_dir, prefix=f".state_{agent}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path_for(state_dir, agent))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_all(state_dir: Path, agents) -> tuple[dict[str, dict], list[str]]:
    """({agent: state}, [unreadable agents])."""
    out, bad = {}, []
    for a in agents:
        try:
            out[a] = read(state_dir, a)
        except Unreadable:
            bad.append(a)
            out[a] = empty()
    return out, bad
