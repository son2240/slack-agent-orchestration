"""Agent registry and limit settings.

The config is one JSON file (see config.example.json). It names every agent that
may post through the shared Slack bot, the channels each agent watches, the human
users whose messages count as "human activity", and the loop-prevention limits.
"""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
CONFIG_ENV = "AGENTORCH_CONFIG"

DEFAULT_LIMITS = {
    "mode": "on",                     # off = wake everything, shadow = wake but log would-be holds, on = enforce
    "human_wake_ttl_sec": 3 * 3600,   # a human-triggered wake keeps an agent at depth 0 for this long
    "pingpong_sec": 600,              # A->B then B->A inside this window is treated as depth 1
    "user_absent_pause_sec": 3 * 3600,  # no human message for this long -> only urgent posts wake anyone
    "receiver_per_hour": 6,           # agent-to-agent wakes one agent may receive per hour
    "sender_per_hour": 3,             # agent-to-agent wakes one agent may cause per hour
    "sender_per_day": 15,
    "global_per_hour": 30,            # agent-to-agent wakes across the whole workspace
    "global_per_day": 120,
    "urgent_per_hour": 3,             # urgent posts to a coordinator bypass depth, but are capped separately
    "urgent_per_day": 10,
}
_INT_LIMITS = tuple(k for k in DEFAULT_LIMITS if k != "mode")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Agent:
    name: str
    display_name: str
    icon_emoji: str = ""
    session_id: str = ""              # optional: required value of AGENTORCH_SESSION_ID when posting as this agent
    channels: tuple[str, ...] = ()    # channel IDs this agent's watcher reads
    coordinator: bool = False         # coordinators accept [urgent] posts past the depth rule


@dataclass(frozen=True)
class Config:
    agents: dict[str, Agent]
    humans: frozenset[str]
    limits: dict
    state_dir: Path
    kill_switch: Path
    token_env: str = "SLACK_BOT_TOKEN"
    extra: dict = field(default_factory=dict)

    def agent(self, name: str) -> Agent:
        try:
            return self.agents[name.lower()]
        except KeyError:
            raise ConfigError(f"unknown agent: {name}") from None

    def agent_by_display(self, display_name: str) -> Agent | None:
        for a in self.agents.values():
            if a.display_name == display_name:
                return a
        return None


def validate_limits(raw: dict | None) -> dict:
    """Merge user limits over defaults. A bad key falls back to its default (never to 'no limit')."""
    out = dict(DEFAULT_LIMITS)
    for k, v in (raw or {}).items():
        if k.startswith("_"):
            continue
        if k == "mode":
            if v in ("off", "shadow", "on"):
                out[k] = v
            continue
        if k in _INT_LIMITS and isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            out[k] = v
        elif k in ("global_per_hour", "global_per_day") and v is None:
            out[k] = math.inf
    return out


def load(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get(CONFIG_ENV) or "agentorch.json")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"config not found: {path} (copy config.example.json)") from None
    return from_dict(raw, base=path.parent)


def from_dict(raw: dict, base: Path | None = None) -> Config:
    base = base or Path.cwd()
    agents = {}
    for name, a in (raw.get("agents") or {}).items():
        name = name.lower()
        if not NAME_RE.match(name):
            raise ConfigError(f"bad agent name: {name!r}")
        agents[name] = Agent(
            name=name,
            display_name=a.get("display_name") or name,
            icon_emoji=a.get("icon_emoji", ""),
            session_id=a.get("session_id", ""),
            channels=tuple(a.get("channels") or ()),
            coordinator=bool(a.get("coordinator", False)),
        )
    if not agents:
        raise ConfigError("config has no agents")
    displays = [a.display_name for a in agents.values()]
    if len(set(displays)) != len(displays):
        raise ConfigError("display_name must be unique (it identifies the poster)")
    state_dir = Path(raw.get("state_dir") or ".agentorch")
    if not state_dir.is_absolute():
        state_dir = base / state_dir
    kill = state_dir / (raw.get("kill_switch_file") or "STOP")
    return Config(
        agents=agents,
        humans=frozenset(raw.get("humans") or ()),
        limits=validate_limits(raw.get("limits")),
        state_dir=state_dir,
        kill_switch=kill,
        token_env=raw.get("token_env") or "SLACK_BOT_TOKEN",
        extra={k: v for k, v in raw.items() if k not in ("agents", "humans", "limits", "state_dir", "kill_switch_file", "token_env")},
    )
