"""Loop prevention for agents that wake each other through Slack.

The problem: agent A posts "-> [b] please check X", B's watcher wakes B, B replies
"-> [a] done", A wakes, replies again... Each wake costs a model turn. Without a gate
a single human request can fan out into an unbounded agent-to-agent conversation.

The rule ("depth 1"):
  * An agent woken by a human message is at depth 0. Its posts may wake other agents.
  * An agent woken by another agent's post is at depth 1. Its posts are *held*: they
    are delivered as data the next time the receiver wakes for another reason, but
    they never wake anyone by themselves.

So a human message can cause at most one hop of agent-to-agent wakes.

The poster does not choose its depth. `sender_depth` derives it from the state file
the sender's own watcher wrote when it woke the sender, and `post.py` attaches it to
the Slack message as metadata. The receiving watcher re-checks the metadata against
the poster identity before trusting it.

On top of the depth rule: ping-pong detection, hourly/daily caps (per sender, per
receiver, global), a kill-switch file, a pause when no human has been seen for a
while, and an `off|shadow|on` mode so the gate can be rolled out by first logging
what it *would* hold.
"""
from __future__ import annotations

import re
import time

from .config import Config

EVENT_TYPE = "agentorch_chain"
TARGET_RE = re.compile(r"(?:→|->)\s*\[([a-z][a-z0-9_-]{0,31})\]", re.I)
CODE_RE = re.compile(r"```.*?```|`[^`\n]*`", re.S)
URGENT_TAG = "[urgent]"
HELD_MARK = "\n:link: depth 1 (held: will not wake the receiver)"


def targets(text: str) -> set[str]:
    """Agent names addressed with `-> [name]`, ignoring code spans."""
    return {t.lower() for t in TARGET_RE.findall(CODE_RE.sub(" ", text or ""))}


def sender_depth(state: dict | None, now: float, limits: dict) -> tuple[int, str]:
    """(depth, cause) for an agent about to post, from its own watcher state.

    Fail-closed: anything unknown or malformed is depth 1."""
    if state is None:
        return 1, "unreadable"
    wake = state.get("wake")
    if not wake:
        return 0, "idle"  # never woken by the watcher: started by a human at the terminal
    try:
        at = float(wake.get("at"))
    except (TypeError, ValueError):
        return 1, "unknown"
    cause = wake.get("cause")
    if at > now + 60:
        return 1, "future_at"
    if cause == "human":
        return (0, "human") if now - at < limits["human_wake_ttl_sec"] else (1, "human_expired")
    if cause in ("agent", "urgent"):
        return 1, cause
    return 1, "unknown"


def metadata(depth: int, agent: str, cause: str) -> dict:
    return {"event_type": EVENT_TYPE, "event_payload": {"depth": int(depth), "agent": agent, "cause": cause}}


def payload_depth(msg: dict, poster: str) -> tuple[int, str]:
    """Trust the metadata only if it names the same agent as the Slack username did."""
    md = msg.get("metadata") or {}
    p = md.get("event_payload") if md.get("event_type") == EVENT_TYPE else None
    if not isinstance(p, dict):
        return 1, "no_metadata"
    if p.get("agent") != poster:
        return 1, "metadata_mismatch"
    d = p.get("depth")
    if d == 0:
        return 0, "ok"
    return 1, "depth"


def _count(wakes, now, window=None, **match) -> int:
    n = 0
    for w in wakes:
        try:
            at = float(w.get("at", 0))
        except (TypeError, ValueError):
            continue
        if window is not None and now - at >= window:
            continue
        if all(w.get(k) == v for k, v in match.items()):
            n += 1
    return n


def decide(msg: dict, poster: str, me: str, cfg: Config, states: dict[str, dict],
           unreadable: list[str] | None = None, now: float | None = None, kill_switch: bool | None = None) -> dict:
    """Should an agent post from `poster` that addresses `me` wake `me`?

    Returns {"action": "wake"|"hold", "reason", "depth", "urgent"[, "shadow_reason"]}."""
    now = now or time.time()
    lim = cfg.limits
    text = msg.get("text") or ""
    depth, why = payload_depth(msg, poster)
    verified = why in ("ok", "depth")
    urgent = verified and cfg.agent(me).coordinator and URGENT_TAG in CODE_RE.sub(" ", text).lower()
    out = {"urgent": urgent, "depth": depth}

    if kill_switch if kill_switch is not None else cfg.kill_switch.exists():
        return {**out, "action": "hold", "reason": "kill_switch"}
    if lim["mode"] == "off":
        return {**out, "action": "wake", "reason": "mode_off"}

    def verdict(reason):
        if lim["mode"] == "shadow":
            return {**out, "action": "wake", "reason": "shadow", "shadow_reason": reason}
        return {**out, "action": "hold", "reason": reason}

    wakes = [w for s in states.values() for w in (s.get("wakes") or [])]
    if depth == 0 and _count(wakes, now, lim["pingpong_sec"], **{"from": me, "to": poster}):
        depth, why = 1, "pingpong"
        out["depth"] = 1

    last_human = max((float(s.get("last_human_at") or 0) for s in states.values()), default=0.0)
    if not urgent and now - last_human > lim["user_absent_pause_sec"]:
        return verdict("user_absent")
    if depth == 1 and not urgent:
        return verdict(why)
    if urgent:
        if _count(wakes, now, 3600, to=me, urgent=True) >= lim["urgent_per_hour"] or \
                _count(wakes, now, 86400, to=me, urgent=True) >= lim["urgent_per_day"]:
            return verdict("cap_urgent")
        return {**out, "action": "wake", "reason": "urgent"}
    if unreadable:
        return verdict("state_unreadable")  # counts below cannot be trusted
    normal = [w for w in wakes if not w.get("urgent")]
    if _count(normal, now, 3600) >= lim["global_per_hour"]:
        return verdict("cap_global_hour")
    if _count(normal, now, 86400) >= lim["global_per_day"]:
        return verdict("cap_global_day")
    if _count(normal, now, 3600, to=me) >= lim["receiver_per_hour"]:
        return verdict("cap_receiver_hour")
    if _count(normal, now, 3600, **{"from": poster}) >= lim["sender_per_hour"]:
        return verdict("cap_sender_hour")
    if _count(normal, now, 86400, **{"from": poster}) >= lim["sender_per_day"]:
        return verdict("cap_sender_day")
    return {**out, "action": "wake", "reason": "ok"}
