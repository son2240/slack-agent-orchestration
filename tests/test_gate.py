import unittest

from agentorch import gate
from agentorch.config import DEFAULT_LIMITS, validate_limits

from fakes import make_config

NOW = 1_800_000_000.0


def msg(agent, depth, text="-> [impl] hi"):
    return {"text": text, "metadata": gate.metadata(depth, agent, "test")}


def active(now=NOW):
    """States in which a human was seen recently (so the absent pause is not in play)."""
    return {"lead": {"last_human_at": now - 60, "wakes": []}, "impl": {"wakes": []}, "rev": {"wakes": []}}


class SenderDepth(unittest.TestCase):
    lim = DEFAULT_LIMITS

    def test_idle_agent_is_depth0(self):
        self.assertEqual(gate.sender_depth({"wake": None}, NOW, self.lim), (0, "idle"))

    def test_recent_human_wake_is_depth0(self):
        self.assertEqual(gate.sender_depth({"wake": {"cause": "human", "at": NOW - 10}}, NOW, self.lim)[0], 0)

    def test_expired_human_wake_is_depth1(self):
        st = {"wake": {"cause": "human", "at": NOW - self.lim["human_wake_ttl_sec"] - 1}}
        self.assertEqual(gate.sender_depth(st, NOW, self.lim), (1, "human_expired"))

    def test_agent_wake_is_depth1(self):
        self.assertEqual(gate.sender_depth({"wake": {"cause": "agent", "at": NOW}}, NOW, self.lim), (1, "agent"))

    def test_fail_closed(self):
        self.assertEqual(gate.sender_depth(None, NOW, self.lim)[0], 1)
        self.assertEqual(gate.sender_depth({"wake": {"cause": "human", "at": "x"}}, NOW, self.lim)[0], 1)
        self.assertEqual(gate.sender_depth({"wake": {"cause": "human", "at": NOW + 3600}}, NOW, self.lim)[0], 1)
        self.assertEqual(gate.sender_depth({"wake": {"cause": "weird", "at": NOW}}, NOW, self.lim)[0], 1)


class Targets(unittest.TestCase):
    def test_arrow_forms(self):
        self.assertEqual(gate.targets("-> [impl] a, → [Rev] b"), {"impl", "rev"})

    def test_code_is_ignored(self):
        self.assertEqual(gate.targets("see `-> [impl]` and ```-> [rev]```"), set())


class Decide(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()

    def d(self, m, poster="lead", me="impl", states=None, **kw):
        return gate.decide(m, poster, me, self.cfg, states if states is not None else active(), now=NOW,
                           kill_switch=kw.pop("kill", False), **kw)

    def test_depth0_wakes(self):
        self.assertEqual(self.d(msg("lead", 0))["action"], "wake")

    def test_depth1_is_held(self):
        r = self.d(msg("lead", 1))
        self.assertEqual((r["action"], r["reason"]), ("hold", "depth"))

    def test_missing_or_forged_metadata_is_held(self):
        self.assertEqual(self.d({"text": "-> [impl] hi"})["reason"], "no_metadata")
        self.assertEqual(self.d(msg("rev", 0))["reason"], "metadata_mismatch")  # posted as lead, claims rev

    def test_kill_switch_beats_everything(self):
        self.assertEqual(self.d(msg("lead", 0), kill=True)["reason"], "kill_switch")

    def test_mode_off_and_shadow(self):
        self.cfg.limits["mode"] = "off"
        self.assertEqual(self.d(msg("lead", 1))["reason"], "mode_off")
        self.cfg.limits["mode"] = "shadow"
        r = self.d(msg("lead", 1))
        self.assertEqual((r["action"], r["shadow_reason"]), ("wake", "depth"))

    def test_pingpong(self):
        st = active()
        st["lead"]["wakes"] = [{"at": NOW - 30, "from": "impl", "to": "lead", "urgent": False}]
        self.assertEqual(self.d(msg("lead", 0), states=st)["reason"], "pingpong")

    def test_user_absent_pause(self):
        st = active()
        st["lead"]["last_human_at"] = NOW - self.cfg.limits["user_absent_pause_sec"] - 1
        self.assertEqual(self.d(msg("lead", 0), states=st)["reason"], "user_absent")

    def test_urgent_to_coordinator_passes_depth_but_is_capped(self):
        m = msg("impl", 1, "-> [lead] [urgent] build is broken")
        r = self.d(m, poster="impl", me="lead")
        self.assertEqual((r["action"], r["reason"]), ("wake", "urgent"))
        st = active()
        st["lead"]["wakes"] = [{"at": NOW - 10, "from": "impl", "to": "lead", "urgent": True}] * 3
        self.assertEqual(self.d(m, poster="impl", me="lead", states=st)["reason"], "cap_urgent")

    def test_urgent_to_non_coordinator_is_not_special(self):
        self.assertEqual(self.d(msg("lead", 1, "-> [impl] [urgent] x"))["reason"], "depth")

    def test_caps(self):
        def wakes(n, **kw):
            return [{"at": NOW - 10, "urgent": False, **kw} for _ in range(n)]
        st = active()
        st["impl"]["wakes"] = wakes(6, **{"from": "rev", "to": "impl"})
        self.assertEqual(self.d(msg("lead", 0), states=st)["reason"], "cap_receiver_hour")
        st = active()
        st["rev"]["wakes"] = wakes(3, **{"from": "lead", "to": "rev"})
        self.assertEqual(self.d(msg("lead", 0), states=st)["reason"], "cap_sender_hour")
        self.cfg.limits["global_per_hour"] = 2
        st = active()
        st["rev"]["wakes"] = wakes(2, **{"from": "x", "to": "rev"})
        self.assertEqual(self.d(msg("lead", 0), states=st)["reason"], "cap_global_hour")

    def test_unreadable_state_holds_normal_posts(self):
        self.assertEqual(self.d(msg("lead", 0), unreadable=["rev"])["reason"], "state_unreadable")


class Limits(unittest.TestCase):
    def test_bad_values_fall_back_to_defaults(self):
        lim = validate_limits({"receiver_per_hour": -1, "sender_per_hour": "9", "mode": "loud", "pingpong_sec": 5})
        self.assertEqual(lim["receiver_per_hour"], DEFAULT_LIMITS["receiver_per_hour"])
        self.assertEqual(lim["sender_per_hour"], DEFAULT_LIMITS["sender_per_hour"])
        self.assertEqual(lim["mode"], "on")
        self.assertEqual(lim["pingpong_sec"], 5)


if __name__ == "__main__":
    unittest.main()
