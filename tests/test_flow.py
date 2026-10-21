"""End-to-end: human -> agent -> agent through the fake Slack, the real watcher and poster."""
import unittest

from agentorch import post, quiet, state, watch

from fakes import Clock, FakeSlack, make_config


class Flow(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.slack = FakeSlack(self.clock)
        self.cfg = make_config()
        for a in self.cfg.agents:  # first scan only sets cursors
            watch.scan(self.cfg, self.slack, a, self.clock())
        self.clock.sleep(1)

    def scan(self, agent):
        self.clock.sleep(1)
        return watch.scan(self.cfg, self.slack, agent, self.clock())

    def say(self, agent, channel, text):
        self.clock.sleep(1)
        return post.post(self.cfg, self.slack, agent, channel, text, now=self.clock())

    def test_one_human_message_allows_exactly_one_agent_hop(self):
        self.slack.human("C1", "-> [lead] please plan the release")
        ev = self.scan("lead")
        self.assertEqual([e["kind"] for e in ev], ["human_message"])

        r = self.say("lead", "C2", "-> [impl] implement step 1")   # lead woke from a human: depth 0
        self.assertEqual(r["depth"], 0)
        ev = self.scan("impl")
        self.assertEqual([e["kind"] for e in ev], ["agent_request"])

        r = self.say("impl", "C1", "-> [lead] done, please review")  # impl woke from an agent: depth 1
        self.assertEqual(r["depth"], 1)
        self.assertIn("held", self.slack.channels["C1"][-1]["text"])
        ev = self.scan("lead")
        self.assertEqual([(e["kind"], e["reason"]) for e in ev], [("held", "depth")])
        self.assertEqual(state.read(self.cfg.state_dir, "lead")["held"][-1]["from"], "impl")

    def test_new_human_message_resets_depth(self):
        self.slack.human("C1", "-> [impl] fix the typo")
        self.assertEqual(self.scan("impl")[0]["kind"], "human_message")
        self.assertEqual(self.say("impl", "C1", "-> [rev] please check")["depth"], 0)

    def test_home_channel_message_without_target_wakes_owner_only(self):
        self.slack.human("C2", "anyone around?")
        self.assertEqual(len(self.scan("impl")), 1)   # C2 is impl's home channel
        self.slack.human("C1", "general note")
        self.assertEqual(len(self.scan("impl")), 0)   # C1 is not impl's home channel
        self.assertEqual(len(self.scan("lead")), 1)   # but it is lead's

    def test_own_posts_and_unknown_bots_are_ignored(self):
        self.slack.human("C1", "-> [lead] hello")
        self.scan("lead")
        self.say("lead", "C1", "-> [lead] note to self")
        self.slack.channels["C1"].append({"ts": "1900000000.000001", "bot_id": "B0X", "username": "Stranger",
                                          "text": "-> [lead] hi"})
        self.assertEqual(self.scan("lead"), [])

    def test_identity_check(self):
        cfg = make_config(impl={"session_id": "s-123"})
        with self.assertRaises(post.IdentityError):
            post.build_payload(cfg, "impl", "C1", "hi", env={})
        payload, _ = post.build_payload(cfg, "impl", "C1", "hi", env={post.SESSION_ENV: "s-123"})
        self.assertEqual(payload["username"], "Impl")

    def test_watch_run_times_out_quietly(self):
        out = watch.run(self.cfg, self.slack, "impl", timeout=60, interval=20, clock=self.clock, sleep=self.clock.sleep)
        self.assertEqual(out["type"], "timeout")

    def test_quiet_wrapper_does_not_wake_on_held_posts(self):
        self.slack.human("C2", "-> [impl] go")
        self.scan("impl")
        self.say("impl", "C1", "-> [rev] look")            # depth 0: wakes rev
        self.scan("rev")
        self.say("rev", "C1", "-> [lead] fyi")             # depth 1: held for lead
        self.slack.human("C1", "-> [lead] status?")         # then a human asks lead
        out = quiet.run(self.cfg, self.slack, "lead", budget=600, interval=20,
                        clock=self.clock, sleep=self.clock.sleep)
        kinds = [e["kind"] for e in out["events"]]
        self.assertIn("human_message", kinds)
        self.assertTrue(any(e["kind"] == "held" for e in out["events"] + out["held"]))

    def test_quiet_wrapper_returns_timeout_with_held_only(self):
        self.slack.human("C2", "-> [impl] go")
        self.scan("impl")
        self.say("impl", "C1", "-> [rev] look")
        self.scan("rev")
        self.say("rev", "C1", "-> [lead] fyi")
        out = quiet.run(self.cfg, self.slack, "lead", budget=200, interval=20,
                        clock=self.clock, sleep=self.clock.sleep)
        self.assertEqual(out["type"], "timeout")
        self.assertEqual([e["kind"] for e in out["held"]], ["held"])


if __name__ == "__main__":
    unittest.main()
