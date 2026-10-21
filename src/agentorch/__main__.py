"""Command line: python -m agentorch <post|watch|quiet|ledger> ..."""
from __future__ import annotations

import argparse
import json
import sys

from . import config, ledger, post, quiet, watch
from .slack import SlackClient, SlackError


def _emit(obj) -> None:
    # ASCII JSON survives any console encoding and is lossless for Unicode
    sys.stdout.write(json.dumps(obj, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="agentorch")
    ap.add_argument("--config", help="path to agentorch.json (default: $AGENTORCH_CONFIG or ./agentorch.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("post", help="post as an agent")
    p.add_argument("--as", dest="agent", required=True)
    p.add_argument("--channel", required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--thread-ts")
    p.add_argument("--dry-run", action="store_true", help="print the payload instead of sending")

    for name in ("watch", "quiet"):
        w = sub.add_parser(name, help="wait for work, print one JSON line, exit")
        w.add_argument("--as", dest="agent", required=True)
        w.add_argument("--interval", type=float, default=20)
        w.add_argument("--timeout" if name == "watch" else "--budget", type=float,
                       default=3600 if name == "watch" else 6900)

    lg = sub.add_parser("ledger", help="shared task ledger")
    lg.add_argument("--db", default=".agentorch/ledger.sqlite3")
    lsub = lg.add_subparsers(dest="op", required=True)
    lsub.add_parser("status")
    s = lsub.add_parser("show")
    s.add_argument("id")
    c = lsub.add_parser("create")
    for k in ("id", "title", "objective", "acceptance"):
        c.add_argument(f"--{k}", required=True)
    c.add_argument("--file", dest="files", action="append", required=True)
    c.add_argument("--assigned", choices=ledger.PROVIDERS, required=True)
    c.add_argument("--actor", required=True)
    for op in ("claim", "progress", "release", "submit", "review"):
        t = lsub.add_parser(op)
        t.add_argument("id")
        t.add_argument("--actor", required=True)
        t.add_argument("--revision", type=int, required=True)
        t.add_argument("--note", default="")
        if op == "submit":
            t.add_argument("--report", required=True)
        if op == "review":
            t.add_argument("--decision", choices=("approve", "changes"), required=True)

    a = ap.parse_args(argv)
    try:
        if a.cmd == "ledger":
            lgr = ledger.Ledger(a.db)
            if a.op == "status":
                _emit(lgr.status())
            elif a.op == "show":
                _emit(lgr.show(a.id))
            elif a.op == "create":
                _emit(lgr.create(a.id, a.title, a.objective, a.acceptance, a.files, a.assigned, a.actor))
            else:
                _emit(lgr.transition(a.id, a.op, a.actor, a.revision, a.note,
                                     report=getattr(a, "report", None), decision=getattr(a, "decision", None)))
            return 0
        cfg = config.load(a.config)
        if a.cmd == "post" and a.dry_run:
            payload, _ = post.build_payload(cfg, a.agent, a.channel, a.text, a.thread_ts)
            _emit({"dry_run": True, "payload": payload})
            return 0
        client = SlackClient.from_env(cfg.token_env)
        if a.cmd == "post":
            _emit(post.post(cfg, client, a.agent, a.channel, a.text, a.thread_ts))
        elif a.cmd == "watch":
            _emit(watch.run(cfg, client, a.agent, timeout=a.timeout, interval=a.interval))
        else:
            _emit(quiet.run(cfg, client, a.agent, budget=a.budget, interval=a.interval))
        return 0
    except post.IdentityError as e:
        _emit({"ok": False, "error": "identity", "detail": str(e)})
        return 4
    except ledger.LedgerError as e:
        _emit({"ok": False, "error": e.code, "detail": str(e)})
        return 1
    except (config.ConfigError, SlackError) as e:
        _emit({"ok": False, "error": type(e).__name__, "detail": str(e)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
