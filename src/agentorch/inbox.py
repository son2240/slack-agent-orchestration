"""File inbox: one router reads Slack, many agents receive without calling Slack.

Slack rate-limits conversations.history (roughly 50 calls/min per workspace for
Tier 3). With one watcher per agent, every extra agent costs more API budget. So an
agent can be `inbox_only`: a `router` agent's watcher, which reads the channels anyway,
copies posts addressed to it into

    <state_dir>/inbox/<agent>/<ts>.json

and the inbox-only watcher only reads that folder. It needs no token and makes no
Slack calls. The receiving side still runs the full loop gate on every record, so
routing never grants extra trust.

Publishing is atomic: the record is written to a temp file and hard-linked to its
final name. Readers only ever see complete files, and an existing name means another
router already delivered it (the same as an exclusive create).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

TS_RE = re.compile(r"^[0-9]{1,12}\.[0-9]{1,9}$")
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
TOKEN_RE = re.compile(r"xox[abposr]-[A-Za-z0-9-]+")
MAX_BYTES = 64 * 1024
MAX_TEXT = 3000
FIELDS = ("ts", "channel", "thread_ts", "username", "bot_id", "user", "text", "metadata", "files", "author")


def box(inbox_dir: Path, agent: str) -> Path | None:
    """The agent's inbox folder, or None if it is a link (never write through links)."""
    if not NAME_RE.match(agent):
        return None
    root, b = Path(inbox_dir), Path(inbox_dir) / agent
    for p in (root, b):
        if p.is_symlink() or getattr(p, "is_junction", lambda: False)():  # is_junction: Python 3.12+
            return None
    return b


def record(msg: dict, channel: str, thread_ts: str | None, author: str, routed_by: str) -> dict:
    rec = {k: msg.get(k) for k in FIELDS if msg.get(k) is not None}
    rec.update({"channel": channel, "thread_ts": thread_ts, "author": author, "routed_by": routed_by,
                "text": TOKEN_RE.sub("[TOKEN-REDACTED]", msg.get("text") or "")[:MAX_TEXT]})
    return rec


def publish(inbox_dir: Path, rec: dict, to: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Deliver `rec` to each agent. Returns (delivered, [(agent, error)]); never raises."""
    made, failed = [], []
    ts = str(rec.get("ts") or "")
    if not TS_RE.match(ts):
        return [], [(a, "bad_ts") for a in to]
    data = TOKEN_RE.sub("[TOKEN-REDACTED]", json.dumps(rec, ensure_ascii=False, indent=1)).encode("utf-8", "replace")
    for agent in to:
        tmp = None
        try:
            b = box(inbox_dir, agent)
            if b is None:
                raise OSError("inbox_link_refused")
            b.mkdir(parents=True, exist_ok=True)
            tmp = b / f"{ts}.{os.getpid()}.tmp"
            tmp.write_bytes(data)
            try:
                os.link(tmp, b / f"{ts}.json")
                made.append(agent)
            except FileExistsError:
                pass  # already delivered
        except OSError as e:
            failed.append((agent, str(e) or type(e).__name__))
        finally:
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass
    return made, failed


def pending(inbox_dir: Path, agent: str) -> list[Path]:
    b = box(inbox_dir, agent)
    if b is None or not b.is_dir():
        return []
    files = [p for p in b.glob("*.json") if p.is_file()]
    return sorted(files, key=lambda p: _ts_key(p.stem))


def _ts_key(stem: str) -> float:
    try:
        return float(stem)
    except ValueError:
        return float("inf")


def load(path: Path) -> dict:
    """Parse one inbox file. Raises ValueError for anything malformed."""
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("too_large")
    rec = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rec, dict) or str(rec.get("ts")) != path.stem or not TS_RE.match(path.stem):
        raise ValueError("bad_shape")
    if rec.get("author") not in ("human", "agent") or not isinstance(rec.get("text", ""), str):
        raise ValueError("bad_shape")
    return rec


def quarantine(path: Path) -> None:
    """Keep a malformed file as evidence instead of deleting it."""
    try:
        os.replace(path, path.with_suffix(".bad"))
    except OSError:
        pass
