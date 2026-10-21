"""Post to Slack as one named agent through a single shared bot.

Every agent shares one bot token; `chat:write.customize` lets each post carry the
agent's own display name and icon, so a channel reads like a team conversation.

Two guards make the persona trustworthy enough for the loop gate:
  * Identity: if the registry pins a session id for the agent, the caller's
    AGENTORCH_SESSION_ID must match, otherwise the post is refused (exit code 4).
  * Depth: the chain depth is computed from the agent's watcher state, not passed
    in by the caller, and attached as Slack message metadata.
"""
from __future__ import annotations

import json
import os
import time

from . import gate, state
from .config import Config

SESSION_ENV = "AGENTORCH_SESSION_ID"


class IdentityError(PermissionError):
    pass


def check_identity(cfg: Config, agent_name: str, env=None) -> None:
    agent = cfg.agent(agent_name)
    env = os.environ if env is None else env
    if agent.session_id and env.get(SESSION_ENV, "") != agent.session_id:
        raise IdentityError(f"caller is not the registered session for agent {agent.name!r}")


def build_payload(cfg: Config, agent_name: str, channel: str, text: str, thread_ts: str | None = None,
                  now: float | None = None, env=None) -> tuple[dict, int]:
    now = now or time.time()
    check_identity(cfg, agent_name, env)
    agent = cfg.agent(agent_name)
    try:
        st = state.read(cfg.state_dir, agent.name)
    except state.Unreadable:
        st = None
    depth, cause = gate.sender_depth(st, now, cfg.limits)
    if depth == 1 and gate.targets(text):
        text = text.rstrip() + gate.HELD_MARK  # humans can see why the receiver did not react
    payload = {
        "channel": channel,
        "text": text,
        "username": agent.display_name,
        "metadata": gate.metadata(depth, agent.name, cause),
    }
    if agent.icon_emoji:
        payload["icon_emoji"] = agent.icon_emoji
    if thread_ts:
        payload["thread_ts"] = thread_ts
    return payload, depth


def post(cfg: Config, client, agent_name: str, channel: str, text: str, thread_ts: str | None = None,
         now: float | None = None, env=None) -> dict:
    payload, depth = build_payload(cfg, agent_name, channel, text, thread_ts, now, env)
    res = client.post(**payload)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    with open(cfg.state_dir / "post.log", "a", encoding="utf-8", newline="\n") as f:
        # no message body and no token in the log
        f.write(json.dumps({"at": time.time(), "agent": agent_name, "channel": channel,
                            "ts": res.get("ts"), "depth": depth}) + "\n")
    return {"ok": True, "ts": res.get("ts"), "channel": res.get("channel", channel), "depth": depth}
