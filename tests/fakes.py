"""Offline stand-ins: an in-memory Slack and a controllable clock."""
from __future__ import annotations

import tempfile
from pathlib import Path

from agentorch import config
from agentorch.slack import SlackError

HUMAN = "U0HUMAN0001"


class FakeSlack:
    """Top-level messages per channel, thread replies kept apart (like the real API)."""

    def __init__(self, clock):
        self.clock = clock
        self.channels: dict[str, list[dict]] = {}
        self.threads: dict[tuple[str, str], list[dict]] = {}
        self.errors: dict[str, str] = {}
        self.calls: list[tuple] = []
        self._seq = 0

    def _ts(self) -> str:
        self._seq += 1
        return f"{self.clock.now:.0f}.{self._seq:06d}"

    def history(self, channel, oldest=None, latest=None, limit=100):
        self.calls.append(("history", channel, oldest, latest))
        if channel in self.errors:
            raise SlackError(f"conversations.history: {self.errors[channel]}")
        msgs = [m for m in self.channels.get(channel, [])
                if (oldest is None or float(m["ts"]) > float(oldest)) and (latest is None or float(m["ts"]) < float(latest))]
        return [dict(m) for m in msgs[-limit:]]  # the newest `limit`, oldest first

    def replies(self, channel, ts, oldest=None, limit=200):
        self.calls.append(("replies", channel, ts, oldest))
        parent = next(m for m in self.channels[channel] if m["ts"] == ts)
        rs = [m for m in self.threads.get((channel, ts), []) if oldest is None or float(m["ts"]) > float(oldest)]
        return [dict(parent)] + [dict(m) for m in rs[:limit]]

    def _add(self, m):
        ch, parent = m["channel"], m.get("thread_ts")
        if parent:
            self.threads.setdefault((ch, parent), []).append(m)
            p = next(x for x in self.channels[ch] if x["ts"] == parent)
            p["latest_reply"], p["reply_count"] = m["ts"], p.get("reply_count", 0) + 1
        else:
            self.channels.setdefault(ch, []).append(m)

    def post(self, **payload):
        m = {"ts": self._ts(), "bot_id": "B0BOT", "subtype": "bot_message", **payload}
        self._add(m)
        return {"ok": True, "ts": m["ts"], "channel": payload["channel"]}

    def human(self, channel, text, user=HUMAN, thread_ts=None, files=None):
        m = {"ts": self._ts(), "user": user, "text": text, "channel": channel}
        if thread_ts:
            m["thread_ts"] = thread_ts
        if files:
            m["files"], m["subtype"] = files, "file_share"
        self._add(m)
        return m


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def sleep(self, sec):
        self.now += sec


def make_config(limits=None, watch=None, extra_agents=None, **agent_overrides) -> config.Config:
    tmp = Path(tempfile.mkdtemp(prefix="agentorch-test-"))
    agents = {
        "lead": {"display_name": "Lead", "channels": ["C1"], "coordinator": True},
        "impl": {"display_name": "Impl", "channels": ["C2", "C1"]},
        "rev": {"display_name": "Rev", "channels": ["C3", "C1"]},
        **(extra_agents or {}),
    }
    for k, v in agent_overrides.items():
        agents[k].update(v)
    return config.from_dict({"humans": [HUMAN], "agents": agents, "limits": {"mode": "on", **(limits or {})},
                             "watch": watch or {}, "state_dir": str(tmp)})
