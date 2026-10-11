import unittest
from unittest.mock import patch

import run_analytics
from run_analytics import speed_policy, trace_events


def outcomes(success, failure, **extra):
    return {"success": success, "failure": failure, "uncertain": 0, "blocked": 0, **extra}


class SpeedPolicyTests(unittest.TestCase):
    def test_no_history_uses_default(self):
        policy = speed_policy({})
        self.assertEqual((policy["speed"], policy["policy"], policy["hold"]), (3.0, "no_history", False))

    def test_proven_default_speed_is_kept(self):
        policy = speed_policy({3.0: outcomes(9, 1)})
        self.assertEqual((policy["speed"], policy["policy"]), (3.0, "default"))

    def test_failures_step_down_one_rung(self):
        policy = speed_policy({3.0: outcomes(1, 4)})
        self.assertEqual((policy["speed"], policy["policy"]), (2.0, "slowed_by_history"))
        self.assertIn("Faster speeds performed poorly", policy["reason"])

    def test_learns_to_settle_on_slower_proven_speed(self):
        policy = speed_policy({3.0: outcomes(1, 4), 2.0: outcomes(2, 3), 1.5: outcomes(6, 0)})
        self.assertEqual(policy["speed"], 1.5)

    def test_too_few_runs_do_not_change_speed(self):
        self.assertEqual(speed_policy({3.0: outcomes(0, 2)})["speed"], 3.0)

    def test_near_tracking_limit_counts_as_bad(self):
        policy = speed_policy({3.0: outcomes(5, 0, avg_max_tracking=7.1)})
        self.assertEqual(policy["speed"], 2.0)

    def test_uncertain_and_blocked_do_not_count(self):
        policy = speed_policy({3.0: {"success": 0, "failure": 0, "uncertain": 9, "blocked": 9}})
        self.assertEqual(policy["policy"], "no_history")

    def test_all_speeds_bad_holds_skill(self):
        policy = speed_policy({s: outcomes(0, 5) for s in run_analytics.SPEED_LADDER})
        self.assertTrue(policy["hold"])

    def test_never_exceeds_controller_limit(self):
        from motion_program import MAX_SPEED
        self.assertTrue(all(s <= MAX_SPEED for s in run_analytics.SPEED_LADDER))

    def test_unconfigured_or_failing_keewano_keeps_default(self):
        with patch.dict("os.environ", {"KEEWANO_PROJECT_ID": "", "KEEWANO_EMAIL": "", "KEEWANO_PAT": ""}):
            self.assertEqual(run_analytics.recommend("episode-2", "abc")["policy"], "keewano_unconfigured")
        with patch.dict("os.environ", {"KEEWANO_PROJECT_ID": "p", "KEEWANO_EMAIL": "e", "KEEWANO_PAT": "t"}), \
                patch.object(run_analytics, "_query", side_effect=OSError("down")):
            policy = run_analytics.recommend("episode-2", "abc")
        self.assertEqual((policy["speed"], policy["policy"], policy["hold"]), (3.0, "keewano_unavailable", False))

    def test_query_rejects_unsafe_identifiers(self):
        with self.assertRaises(ValueError):
            run_analytics.fetch_history("x' OR '1'='1", "abc")

    def test_history_rows_become_ladder_stats(self):
        rows = iter([[[30, "failure", 4], [30, "success", 1], [20, "success", 3]], [[30, 72.0]]])
        with patch.object(run_analytics, "_query", side_effect=lambda _sql: next(rows)):
            stats = run_analytics.fetch_history("episode-2", "a" * 64)
        self.assertEqual(stats[3.0]["failure"], 4)
        self.assertEqual(stats[3.0]["avg_max_tracking"], 7.2)
        self.assertEqual(speed_policy(stats)["speed"], 2.0)


class TraceTests(unittest.TestCase):
    def test_execution_trace_is_ordered_and_categorized(self):
        run = {"kind": "execution", "result": "failure", "max_tracking": 4.26,
               "initial_state": {"temperatures": {"a": 41, "b": 44.6}},
               "verification": {"confidence": .93},
               "events": [{"at": 100, "event": "checking_scene"}, {"at": 101, "event": "protocol_event"},
                          {"at": 102, "event": "tool_failed", "tool": "execute_motor_program", "error": "Wrist camera lost during motion"},
                          {"at": 103, "event": "finished", "verdict": "failure"}]}
        plan = {"skill_id": "episode-2", "sha256": "f" * 64, "speed": 2.0,
                "policy": {"policy": "slowed_by_history", "reason": "r", "success_rate": .5}}
        names = {e["eventTypeId"]: e for e in trace_events(run, plan)}
        by_name = {spec["name"]: names[spec["eventTypeId"]]["data"] for spec in run_analytics.keewano.CUSTOM_EVENTS if spec["eventTypeId"] in names}
        self.assertEqual(by_name["ReplaySpeedTenths"], 20)
        self.assertEqual(by_name["MotionVerdict"], "failure")
        self.assertEqual(by_name["MaxTrackingTenths"], 43)
        self.assertEqual(by_name["MaxMotorTempC"], 45)
        self.assertEqual(by_name["ErrorClass"], "camera")
        self.assertEqual(by_name["MotionHash"], "f" * 16)
        self.assertFalse(by_name["RunCompleted"])
        events = trace_events(run, plan)
        self.assertEqual([e["timestamp"] for e in events], sorted(e["timestamp"] for e in events))
        self.assertNotIn("protocol_event", [e["data"] for e in events])


if __name__ == "__main__":
    unittest.main()
