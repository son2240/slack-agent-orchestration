"""Read-only health table for all agents: is each watcher alive, what woke it last,
how much is held, how much is waiting in its inbox, how many read gaps remain."""
from __future__ import annotations

import time

from . import inbox, state, watch
from .config import Config


def report(cfg: Config, now: float | None = None) -> list[dict]:
    now = now or time.time()
    rows = []
    for name, a in cfg.agents.items():
        try:
            st = state.read(cfg.state_dir, name)
            ok = True
        except state.Unreadable:
            st, ok = state.empty(), False
        wake = st.get("wake") or {}
        alive_at = st.get("alive_at")
        rows.append({
            "agent": name,
            "mode": "inbox" if a.inbox_only else ("router" if a.router else "channels"),
            "state_ok": ok,
            "alive": watch.is_alive(st, now, cfg.watch["liveness_sec"]),
            "alive_age_sec": round(now - float(alive_at)) if alive_at else None,
            "last_wake": wake.get("cause"),
            "last_wake_age_sec": round(now - float(wake["at"])) if wake.get("at") else None,
            "held_24h": sum(1 for h in st.get("held") or [] if now - float(h.get("at", 0)) < 86400),
            "inbox_pending": len(inbox.pending(cfg.inbox_dir, name)),
            "read_gaps": sum(len(v) for v in (st.get("gaps") or {}).values()),
        })
    return rows


def table(rows: list[dict]) -> str:
    cols = ["agent", "mode", "alive", "alive_age_sec", "last_wake", "held_24h", "inbox_pending", "read_gaps", "state_ok"]
    cells = [[str(r[c]) if r[c] is not None else "-" for c in cols] for r in rows]
    width = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(cols)]
    line = lambda vals: "  ".join(v.ljust(w) for v, w in zip(vals, width))  # noqa: E731
    return "\n".join([line(cols), line(["-" * w for w in width])] + [line(r) for r in cells])
