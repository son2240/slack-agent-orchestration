"""Shared task ledger for two kinds of agents (e.g. Codex plans/reviews, Claude Code implements).

Slack is good for talking and bad as a source of truth, so task ownership lives
in a small SQLite file both sides can reach:

    queued --claim--> active --submit--> review --approve--> done
                        ^  |                |
                        |  +--release--> queued
                        +------changes------+

Guarantees:
  * Optimistic concurrency: every write names the revision it read. A stale
    revision is rejected, so two agents cannot silently overwrite each other.
  * Provider assignment: a task is assigned to a provider ("claude", "codex");
    only an actor of that provider can claim it. Actors look like "claude:impl-1".
  * Separation of duties: the reviewer can never be the owner.
  * File scopes: a task declares the paths it will touch; claiming is refused if an
    active or in-review task already holds an overlapping path.
  * Reports are pinned by SHA-256 at submit time, so the reviewer reviews exactly
    the bytes that were submitted.
  * Every transition is appended to an event history.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, objective TEXT NOT NULL, acceptance TEXT NOT NULL,
    files TEXT NOT NULL, assigned TEXT NOT NULL, owner TEXT, state TEXT NOT NULL,
    revision INTEGER NOT NULL, report TEXT, report_sha256 TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, at TEXT NOT NULL,
    actor TEXT NOT NULL, action TEXT NOT NULL, note TEXT NOT NULL, revision INTEGER NOT NULL
);
"""
PROVIDERS = ("claude", "codex")
HOLDING = ("active", "review")


class LedgerError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def provider(actor: str) -> str:
    p, sep, name = actor.partition(":")
    if not sep or p not in PROVIDERS or not name:
        raise LedgerError("bad_actor", f"{actor!r} (expected '<claude|codex>:<name>')")
    return p


def norm_scope(raw: str) -> str:
    p = PurePosixPath(raw.replace("\\", "/").strip())
    if p.is_absolute() or ".." in p.parts:
        raise LedgerError("bad_scope", raw)
    s = str(p)
    return "" if s == "." else s


def overlaps(a: str, b: str) -> bool:
    if a == "" or b == "":
        return True  # whole repository
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


class Ledger:
    def __init__(self, db: str | Path):
        self.db = Path(db)
        self.db.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        con = sqlite3.connect(self.db, timeout=10, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            yield con
        finally:
            con.close()

    @contextmanager
    def _tx(self):
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")  # one writer at a time, readers unaffected
            try:
                yield con
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise

    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        d["files"] = json.loads(d["files"])
        return d

    def _get(self, con, task_id: str) -> dict:
        r = con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if r is None:
            raise LedgerError("not_found", task_id)
        return self._row(r)

    def _event(self, con, task_id, actor, action, note, revision):
        con.execute("INSERT INTO events(task_id, at, actor, action, note, revision) VALUES (?,?,?,?,?,?)",
                    (task_id, _now(), actor, action, note, revision))

    # -- reads -------------------------------------------------------------
    def show(self, task_id: str) -> dict:
        with self._connect() as con:
            t = self._get(con, task_id)
            t["events"] = [dict(e) for e in con.execute(
                "SELECT at, actor, action, note, revision FROM events WHERE task_id=? ORDER BY sequence", (task_id,))]
            return t

    def status(self) -> dict:
        with self._connect() as con:
            rows = [self._row(r) for r in con.execute("SELECT * FROM tasks ORDER BY created_at, id")]
        out: dict[str, list] = {}
        for t in rows:
            out.setdefault(t["state"], []).append({k: t[k] for k in ("id", "title", "assigned", "owner", "revision")})
        return out

    # -- writes ------------------------------------------------------------
    def create(self, task_id: str, title: str, objective: str, acceptance: str, files: list[str],
               assigned: str, actor: str) -> dict:
        provider(actor)
        if assigned not in PROVIDERS:
            raise LedgerError("bad_assigned", assigned)
        scopes = sorted({norm_scope(f) for f in files})
        if not scopes:
            raise LedgerError("no_scope", "declare at least one file or directory")
        now = _now()
        with self._tx() as con:
            if con.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
                raise LedgerError("exists", task_id)
            con.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (task_id, title, objective, acceptance, json.dumps(scopes), assigned, None, "queued", 1,
                         None, None, now, now))
            self._event(con, task_id, actor, "create", title, 1)
            return self._get(con, task_id)

    def transition(self, task_id: str, action: str, actor: str, revision: int, note: str = "",
                   report: str | None = None, decision: str | None = None) -> dict:
        prov = provider(actor)
        with self._tx() as con:
            t = self._get(con, task_id)
            if t["revision"] != revision:
                raise LedgerError("stale_revision", f"read {revision}, current {t['revision']}")
            st, owner = t["state"], t["owner"]
            upd: dict = {}
            if action == "claim":
                self._need(st, "queued")
                if prov != t["assigned"]:
                    raise LedgerError("wrong_provider", f"task is assigned to {t['assigned']}")
                self._check_scope(con, t)
                upd = {"state": "active", "owner": actor}
            elif action == "progress":
                self._need(st, "active")
                self._need_owner(owner, actor)
            elif action == "release":
                self._need(st, "active")
                self._need_owner(owner, actor)
                upd = {"state": "queued", "owner": None}
            elif action == "submit":
                self._need(st, "active")
                self._need_owner(owner, actor)
                if not report:
                    raise LedgerError("no_report")
                data = Path(report).read_bytes()
                upd = {"state": "review", "report": report, "report_sha256": hashlib.sha256(data).hexdigest()}
            elif action == "review":
                self._need(st, "review")
                if actor == owner:
                    raise LedgerError("self_review", "the owner cannot review their own task")
                if decision not in ("approve", "changes"):
                    raise LedgerError("bad_decision", str(decision))
                if Path(t["report"]).exists() and \
                        hashlib.sha256(Path(t["report"]).read_bytes()).hexdigest() != t["report_sha256"]:
                    raise LedgerError("report_changed", "the report changed after submit")
                upd = {"state": "done"} if decision == "approve" else {"state": "active"}
                note = f"{decision}: {note}".rstrip(": ")
            else:
                raise LedgerError("bad_action", action)
            upd["revision"] = revision + 1
            upd["updated_at"] = _now()
            cols = ", ".join(f"{k}=?" for k in upd)
            cur = con.execute(f"UPDATE tasks SET {cols} WHERE id=? AND revision=?", (*upd.values(), task_id, revision))
            if cur.rowcount != 1:
                raise LedgerError("stale_revision")
            self._event(con, task_id, actor, action, note, revision + 1)
            return self._get(con, task_id)

    @staticmethod
    def _need(state: str, want: str):
        if state != want:
            raise LedgerError("bad_state", f"is {state}, needs {want}")

    @staticmethod
    def _need_owner(owner, actor):
        if owner != actor:
            raise LedgerError("not_owner", f"owned by {owner}")

    def _check_scope(self, con, t: dict):
        for r in con.execute("SELECT id, files FROM tasks WHERE state IN (?,?) AND id<>?", (*HOLDING, t["id"])):
            other = json.loads(r["files"])
            for a in t["files"]:
                for b in other:
                    if overlaps(a, b):
                        raise LedgerError("scope_conflict", f"{a!r} overlaps {b!r} held by task {r['id']}")
