"""Offline stand-ins: an in-memory Slack and a controllable clock."""
from __future__ import annotations

import tempfile
from pathlib import Path

from agentorch import config

HUMAN = "U0HUMAN0001"


class FakeSlack:
    def __init__(self, clock):
        self.clock = clock
        self.channels: dict[str, list[dict]] = {}
        self._seq = 0

    def _ts(self) -> str:
        self._seq += 1
        return f"{self.clock.now:.0f}.{self._seq:06d}"

    def history(self, channel, oldest=None, limit=100):
        msgs = self.channels.get(channel, [])
        return [m for m in msgs if oldest is None or float(m["ts"]) > float(oldest)][:limit]

    def post(self, **payload):
        m = {"ts": self._ts(), "bot_id": "B0BOT", "subtype": "bot_message", **payload}
        self.channels.setdefault(payload["channel"], []).append(m)
        return {"ok": True, "ts": m["ts"], "channel": payload["channel"]}

    def human(self, channel, text, user=HUMAN):
        m = {"ts": self._ts(), "user": user, "text": text}
        self.channels.setdefault(channel, []).append(m)
        return m


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def sleep(self, sec):
        self.now += sec


def make_config(limits=None, **agent_overrides) -> config.Config:
    tmp = Path(tempfile.mkdtemp(prefix="agentorch-test-"))
    agents = {
        "lead": {"display_name": "Lead", "channels": ["C1"], "coordinator": True},
        "impl": {"display_name": "Impl", "channels": ["C2", "C1"]},
        "rev": {"display_name": "Rev", "channels": ["C3", "C1"]},
    }
    for k, v in agent_overrides.items():
        agents[k].update(v)
    return config.from_dict({"humans": [HUMAN], "agents": agents, "limits": {"mode": "on", **(limits or {})},
                             "state_dir": str(tmp)})
