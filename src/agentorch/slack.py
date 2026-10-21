"""Minimal Slack Web API client (stdlib only).

The bot token is read from an environment variable and never logged or written.
Bot scopes used: chat:write, chat:write.customize (per-agent name/icon),
channels:history (and groups:history for private channels).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://slack.com/api/"
GET_METHODS = {"conversations.history", "conversations.replies", "auth.test"}


class SlackError(RuntimeError):
    pass


class SlackClient:
    def __init__(self, token: str, timeout: float = 15.0):
        if not token or not token.startswith("xoxb-"):
            raise SlackError("a bot token (xoxb-...) is required")
        self._token = token
        self.timeout = timeout

    @classmethod
    def from_env(cls, var: str = "SLACK_BOT_TOKEN") -> "SlackClient":
        return cls(os.environ.get(var, ""))

    def __repr__(self) -> str:  # keep the token out of tracebacks and logs
        return "SlackClient(token=***)"

    def call(self, method: str, **params) -> dict:
        headers = {"Authorization": f"Bearer {self._token}"}
        if method in GET_METHODS:
            q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            req = urllib.request.Request(f"{API}{method}?{q}", headers=headers)
        else:
            body = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
            headers["Content-Type"] = "application/json; charset=utf-8"
            req = urllib.request.Request(API + method, data=body, headers=headers)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 2:
                    time.sleep(float(e.headers.get("Retry-After", "1")))
                    continue
                raise SlackError(f"{method}: HTTP {e.code}") from None
        if not data.get("ok"):
            raise SlackError(f"{method}: {data.get('error', 'unknown_error')}")
        return data

    def history(self, channel: str, oldest: str | None = None, limit: int = 100) -> list[dict]:
        """Messages newer than `oldest`, oldest first."""
        data = self.call("conversations.history", channel=channel, oldest=oldest, limit=limit,
                         inclusive="false", include_all_metadata="true")
        return sorted(data.get("messages") or [], key=lambda m: float(m.get("ts", 0)))

    def post(self, **payload) -> dict:
        return self.call("chat.postMessage", **payload)
