import tempfile
import unittest
from pathlib import Path

from agentorch.ledger import Ledger, LedgerError, overlaps


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ledger-test-"))
        self.l = Ledger(self.dir / "l.sqlite3")
        self.l.create("t1", "Add login", "Implement login", "tests pass", ["src/auth"], "claude", "codex:lead")

    def report(self, text="done"):
        p = self.dir / "report.md"
        p.write_text(text, encoding="utf-8")
        return str(p)

    def err(self, code, fn, *a, **kw):
        with self.assertRaises(LedgerError) as cm:
            fn(*a, **kw)
        self.assertEqual(cm.exception.code, code)

    def test_happy_path_and_history(self):
        t = self.l.transition("t1", "claim", "claude:impl", 1)
        t = self.l.transition("t1", "submit", "claude:impl", t["revision"], report=self.report())
        self.assertEqual(t["state"], "review")
        self.assertEqual(len(t["report_sha256"]), 64)
        t = self.l.transition("t1", "review", "codex:lead", t["revision"], note="lgtm", decision="approve")
        self.assertEqual((t["state"], t["revision"]), ("done", 4))
        actions = [e["action"] for e in self.l.show("t1")["events"]]
        self.assertEqual(actions, ["create", "claim", "submit", "review"])

    def test_stale_revision_is_rejected(self):
        self.l.transition("t1", "claim", "claude:impl", 1)
        self.err("stale_revision", self.l.transition, "t1", "progress", "claude:impl", 1)

    def test_provider_owner_and_reviewer_rules(self):
        self.err("wrong_provider", self.l.transition, "t1", "claim", "codex:other", 1)
        self.l.transition("t1", "claim", "claude:impl", 1)
        self.err("not_owner", self.l.transition, "t1", "submit", "claude:other", 2, report=self.report())
        self.l.transition("t1", "submit", "claude:impl", 2, report=self.report())
        self.err("self_review", self.l.transition, "t1", "review", "claude:impl", 3, decision="approve")
        self.err("bad_actor", self.l.transition, "t1", "review", "lead", 3, decision="approve")

    def test_changes_requested_goes_back_to_owner(self):
        self.l.transition("t1", "claim", "claude:impl", 1)
        self.l.transition("t1", "submit", "claude:impl", 2, report=self.report())
        t = self.l.transition("t1", "review", "codex:lead", 3, note="add tests", decision="changes")
        self.assertEqual((t["state"], t["owner"]), ("active", "claude:impl"))

    def test_report_pinned_by_hash(self):
        self.l.transition("t1", "claim", "claude:impl", 1)
        path = self.report("v1")
        self.l.transition("t1", "submit", "claude:impl", 2, report=path)
        Path(path).write_text("v2 edited after submit", encoding="utf-8")
        self.err("report_changed", self.l.transition, "t1", "review", "codex:lead", 3, decision="approve")

    def test_scope_conflicts(self):
        self.l.create("t2", "Auth tests", "x", "y", ["src/auth/tests"], "claude", "codex:lead")
        self.l.create("t3", "Docs", "x", "y", ["docs"], "claude", "codex:lead")
        self.l.transition("t1", "claim", "claude:impl", 1)
        self.err("scope_conflict", self.l.transition, "t2", "claim", "claude:impl2", 1)
        self.assertEqual(self.l.transition("t3", "claim", "claude:impl2", 1)["state"], "active")
        self.l.transition("t1", "release", "claude:impl", 2)
        self.assertEqual(self.l.transition("t2", "claim", "claude:impl2", 1)["state"], "active")

    def test_bad_scope_and_duplicates(self):
        self.err("bad_scope", self.l.create, "t9", "x", "x", "x", ["../etc"], "claude", "codex:lead")
        self.err("exists", self.l.create, "t1", "x", "x", "x", ["a"], "claude", "codex:lead")

    def test_overlaps(self):
        self.assertTrue(overlaps("src", "src/a.py"))
        self.assertTrue(overlaps("", "anything"))
        self.assertFalse(overlaps("src/a", "src/ab"))

    def test_status_groups_by_state(self):
        self.l.transition("t1", "claim", "claude:impl", 1)
        self.assertEqual([t["id"] for t in self.l.status()["active"]], ["t1"])


if __name__ == "__main__":
    unittest.main()
