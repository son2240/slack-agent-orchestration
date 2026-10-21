"""Threads, read gaps, inbox routing, offline cover, attachments, code reload, health."""
import json
import unittest

from agentorch import health, inbox, post, quiet, state, watch

from fakes import Clock, FakeSlack, make_config

INBOX_AGENT = {"notes": {"display_name": "Notes", "inbox_only": True}}


class Base(unittest.TestCase):
    def setUp(self, **cfg_kw):
        self.clock = Clock()
        self.slack = FakeSlack(self.clock)
        self.cfg = make_config(**cfg_kw)
        for a in self.cfg.agents:
            watch.scan(self.cfg, self.slack, a, self.clock())
        self.clock.sleep(1)

    def scan(self, agent):
        self.clock.sleep(1)
        return watch.scan(self.cfg, self.slack, agent, self.clock())

    def say(self, agent, channel, text, thread_ts=None):
        self.clock.sleep(1)
        return post.post(self.cfg, self.slack, agent, channel, text, thread_ts=thread_ts, now=self.clock())


class Threads(Base):
    def test_new_reply_in_old_thread_is_seen(self):
        parent = self.slack.human("C2", "-> [impl] start the task")
        self.assertEqual(self.scan("impl")[0]["kind"], "human_message")
        self.slack.human("C2", "-> [impl] also handle the edge case", thread_ts=parent["ts"])
        ev = self.scan("impl")
        self.assertEqual([(e["kind"], e["thread_ts"]) for e in ev], [("human_message", parent["ts"])])
        self.assertEqual(self.scan("impl"), [])  # the same reply is not delivered twice

    def test_agent_reply_in_thread_goes_through_the_gate(self):
        parent = self.slack.human("C1", "-> [lead] plan it")
        self.scan("lead")
        self.say("lead", "C1", "-> [impl] take step 1", thread_ts=parent["ts"])  # depth 0
        ev = self.scan("impl")
        self.assertEqual([e["kind"] for e in ev], ["agent_request"])

    def test_threads_present_at_first_run_are_not_replayed(self):
        cfg = make_config()
        clock, slack = Clock(), FakeSlack(Clock())
        slack.clock = clock
        p = slack.human("C2", "old question")
        slack.human("C2", "-> [impl] old reply", thread_ts=p["ts"])
        clock.sleep(1)
        self.assertEqual(watch.scan(cfg, slack, "impl", clock()), [])
        clock.sleep(1)
        self.assertEqual(watch.scan(cfg, slack, "impl", clock()), [])


class Gaps(Base):
    def setUp(self):
        super().setUp(watch={"history_window": 3, "backfill_calls": 1})

    def test_burst_larger_than_window_is_backfilled(self):
        for i in range(7):
            self.slack.human("C2", f"-> [impl] item {i}")
        first = self.scan("impl")  # newest 3, then one backfill call for 3 more
        second = self.scan("impl")  # the last one
        texts = sorted(e["text"] for e in first + second if e["kind"] == "human_message")
        self.assertEqual(texts, sorted(f"-> [impl] item {i}" for i in range(7)))
        self.assertTrue(any(e["kind"] == "notice" and "backlog" in e["text"] for e in first))
        self.assertEqual(state.read(self.cfg.state_dir, "impl")["gaps"], {})

    def test_merge_gaps(self):
        self.assertEqual(watch.merge_gaps([["5", "9"], ["1", "3"], ["2", "4"], ["7", "7"], "junk"]),
                         [["1", "4"], ["5", "9"]])


class Inbox(Base):
    def setUp(self):
        super().setUp(extra_agents=INBOX_AGENT, lead={"router": True})

    def test_router_delivers_and_inbox_agent_needs_no_slack(self):
        self.slack.human("C1", "-> [lead] kick off")
        self.scan("lead")
        self.say("lead", "C1", "-> [notes] write the changelog")
        self.scan("lead")  # the router reads its channel and routes the post
        self.assertEqual(len(inbox.pending(self.cfg.inbox_dir, "notes")), 1)
        calls_before = len(self.slack.calls)
        ev = watch.scan(self.cfg, None, "notes", self.clock())  # client=None: must not touch Slack
        self.assertEqual(len(self.slack.calls), calls_before)
        self.assertEqual([(e["kind"], e["via"]) for e in ev], [("agent_request", "inbox")])
        self.assertEqual(inbox.pending(self.cfg.inbox_dir, "notes"), [])

    def test_routed_posts_still_pass_the_gate(self):
        self.slack.human("C2", "-> [impl] go")
        self.scan("impl")
        self.say("impl", "C1", "-> [rev] check")   # depth 0, wakes rev
        self.scan("rev")
        self.say("rev", "C1", "-> [notes] fyi")    # depth 1
        self.scan("lead")
        ev = watch.scan(self.cfg, None, "notes", self.clock() + 1)
        self.assertEqual([(e["kind"], e["reason"]) for e in ev], [("held", "depth")])

    def test_human_message_is_routed(self):
        self.slack.human("C1", "-> [notes] draft release notes")
        self.scan("lead")
        ev = watch.scan(self.cfg, None, "notes", self.clock() + 1)
        self.assertEqual(ev[0]["kind"], "human_message")

    def test_publish_is_idempotent_and_redacts_tokens(self):
        rec = {"ts": "1800000000.000001", "author": "human", "text": "token xoxb-123-abc", "user": "U0HUMAN0001"}
        self.assertEqual(inbox.publish(self.cfg.inbox_dir, rec, ["notes"])[0], ["notes"])
        self.assertEqual(inbox.publish(self.cfg.inbox_dir, rec, ["notes"])[0], [])  # already there
        body = inbox.pending(self.cfg.inbox_dir, "notes")[0].read_text(encoding="utf-8")
        self.assertNotIn("xoxb-123", body)

    def test_bad_and_stale_files(self):
        box = self.cfg.inbox_dir / "notes"
        box.mkdir(parents=True, exist_ok=True)
        (box / "1800000000.000002.json").write_text("{not json", encoding="utf-8")
        old = {"ts": "1700000000.000001", "author": "human", "text": "-> [notes] old", "user": "U0HUMAN0001"}
        (box / "1700000000.000001.json").write_text(json.dumps(old), encoding="utf-8")
        ev = watch.scan(self.cfg, None, "notes", self.clock())
        self.assertEqual(sorted(e["kind"] for e in ev), ["held", "notice"])
        self.assertTrue((box / "1800000000.000002.bad").exists())  # kept as evidence

    def test_inbox_agent_with_channels_is_rejected(self):
        from agentorch.config import ConfigError
        with self.assertRaises(ConfigError):
            make_config(extra_agents={"x": {"display_name": "X", "inbox_only": True, "channels": ["C9"]}})


class OfflineCover(Base):
    def setUp(self):
        super().setUp(lead={"router": True, "channels": ["C1", "C2"]})

    def test_router_covers_offline_watcher(self):
        self.slack.human("C2", "-> [impl] are you there?")
        self.clock.sleep(self.cfg.watch["liveness_sec"] + 1)  # impl's heartbeat is now stale
        ev = self.scan("lead")
        self.assertEqual([(e["kind"], e["for"]) for e in ev], [("offline_cover", "impl")])

    def test_no_cover_when_owner_is_alive(self):
        self.slack.human("C2", "-> [impl] are you there?")
        self.scan("impl")
        self.assertEqual(self.scan("lead"), [])


class Attachments(Base):
    def test_only_safe_metadata_is_passed(self):
        files = [{"id": "F0ABC12345", "name": "spec/../v2.pdf", "mimetype": "application/pdf", "size": 1234,
                  "url_private": "https://files.example/secret"}, {"id": "nope"}]
        self.slack.human("C2", "-> [impl] see attached", files=files)
        ev = self.scan("impl")
        self.assertEqual(ev[0]["files"], [{"id": "F0ABC12345", "name": "spec..v2.pdf",
                                          "mimetype": "application/pdf", "size": 1234}])


class Notices(Base):
    def test_bot_not_in_channel_is_reported_once(self):
        self.slack.errors["C3"] = "not_in_channel"
        first, second = self.scan("rev"), self.scan("rev")
        self.assertEqual([e["kind"] for e in first], ["notice"])
        self.assertEqual(second, [])

    def test_quiet_wrapper_does_not_wake_on_notices(self):
        self.slack.errors["C3"] = "not_in_channel"
        out = quiet.run(self.cfg, self.slack, "rev", budget=200, interval=20, clock=self.clock, sleep=self.clock.sleep)
        self.assertEqual(out["type"], "timeout")
        self.assertEqual([e["kind"] for e in out["held"]], ["notice"])


class CodeReload(Base):
    def test_watch_exits_when_code_changes(self):
        stamps = iter([("v1",), ("v2",)])
        out = watch.run(self.cfg, self.slack, "impl", timeout=600, interval=20,
                        clock=self.clock, sleep=self.clock.sleep, code=lambda: next(stamps))
        self.assertEqual(out["type"], "code_updated")

    def test_quiet_restarts_on_code_update_without_waking(self):
        outs = iter([{"type": "code_updated"}, {"type": "timeout"},
                     {"type": "events", "events": [{"kind": "human_message"}]}])
        out = quiet.run(self.cfg, None, "impl", budget=6900, clock=self.clock, sleep=self.clock.sleep,
                        runner=lambda timeout: next(outs))
        self.assertEqual(out["events"][0]["kind"], "human_message")

    def test_quiet_passes_errors_through(self):
        out = quiet.run(self.cfg, None, "impl", runner=lambda timeout: {"type": "error", "exit_code": 2})
        self.assertEqual(out["type"], "error")


class Health(Base):
    def setUp(self):
        super().setUp(extra_agents=INBOX_AGENT, lead={"router": True})

    def test_report(self):
        self.slack.human("C1", "-> [notes] hi")
        self.scan("lead")
        rows = {r["agent"]: r for r in health.report(self.cfg, self.clock())}
        self.assertEqual(rows["notes"]["inbox_pending"], 1)
        self.assertEqual(rows["lead"]["mode"], "router")
        self.assertTrue(rows["lead"]["alive"])
        self.assertIn("inbox_pending", health.table(list(rows.values())))


if __name__ == "__main__":
    unittest.main()
