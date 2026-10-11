import unittest
import uuid
from unittest.mock import patch

import run_analytics
from run_analytics import speed_policy, trace_events


def outcomes(success, failure, **extra):
    return {"success": success, "failure": failure, "uncertain": 0, "blocked": 0, **extra}


class SpeedPolicyTests(unittest.TestCase):
    def test_no_history_uses_default(self):
        policy = speed_policy({})
        self.assertEqual((policy["speed"], policy["policy"], policy["hold"]), (18.0, "no_history", False))

    def test_proven_default_speed_is_kept(self):
        policy = speed_policy({18.0: outcomes(9, 1)})
        self.assertEqual((policy["speed"], policy["policy"]), (18.0, "default"))

    def test_failures_step_down_one_rung(self):
        policy = speed_policy({18.0: outcomes(1, 4)})
        self.assertEqual((policy["speed"], policy["policy"]), (6.0, "slowed_by_history"))
        self.assertIn("Faster speeds performed poorly", policy["reason"])

    def test_learns_to_settle_on_slower_proven_speed(self):
        policy = speed_policy({18.0: outcomes(1, 4), 6.0: outcomes(2, 3), 3.0: outcomes(2, 3), 2.0: outcomes(6, 0)})
        self.assertEqual(policy["speed"], 2.0)

    def test_too_few_runs_do_not_change_speed(self):
        self.assertEqual(speed_policy({18.0: outcomes(0, 2)})["speed"], 18.0)

    def test_near_tracking_limit_counts_as_bad(self):
        policy = speed_policy({18.0: outcomes(5, 0, avg_max_tracking=7.1)})
        self.assertEqual(policy["speed"], 6.0)

    def test_uncertain_and_blocked_do_not_count(self):
        policy = speed_policy({18.0: {"success": 0, "failure": 0, "uncertain": 9, "blocked": 9}})
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
        self.assertEqual((policy["speed"], policy["policy"], policy["hold"]), (18.0, "keewano_unavailable", False))

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


class RunContextTests(unittest.TestCase):
    ENTITY = "281532bc-df22-5213-a6c5-7acc7a7e245f"

    def keewano(self, sql):
        if "MotionVerdict\" IS NOT NULL" in sql:
            # Two final rows per run (MotionVerdict, then RunCompleted) carry the same values.
            row = [self.ENTITY, "2026-10-11T18:33:04", "a" * 32, "v" * 16, "sms", 20, "slowed_by_history", 66, "failure",
                   93, 71, 44, "tracking", "tracking", "execute_motor_program", 107]
            return [row, row, [str(uuid.uuid4()), "2026-10-10", None, "old-video-hash00", "dashboard", 30, "default", 80,
                               "success", 95, 40, 41, None, None, None, 90]]
        if "BlockedReason\" AS value" in sql:
            return [["tracking", 2], ["camera", 5]]
        if "ErrorClass\" AS value" in sql:
            return [["camera", 3]]
        if "user_id IN" in sql:
            self.assertIn(self.ENTITY, sql)
            return [[self.ENTITY, "t1", "planning", None, None, None, None, None],
                    [self.ENTITY, "t2", "planning", "execute_motor_program", None, None, None, None],
                    [self.ENTITY, "t3", "tool_failed", "execute_motor_program", "execute_motor_program", "tracking", None, None]]
        raise AssertionError(sql)

    def context(self):
        with patch.dict("os.environ", {"KEEWANO_PROJECT_ID": "p", "KEEWANO_EMAIL": "e", "KEEWANO_PAT": "t"}), \
                patch.object(run_analytics, "_connect"), patch.object(run_analytics, "_query", side_effect=self.keewano):
            return run_analytics.run_context("episode-2", "f" * 64, "v" * 64)

    def test_recent_runs_carry_telemetry_and_video_link(self):
        context = self.context()
        self.assertTrue(context["available"])
        latest, older = context["recent_runs"]
        self.assertEqual((latest["verdict"], latest["replay_speed"], latest["max_tracking"]), ("failure", 2.0, 7.1))
        self.assertEqual((latest["retrieval_score"], latest["verdict_confidence"]), (.66, .93))
        self.assertTrue(latest["same_video"])
        self.assertFalse(older["same_video"])
        self.assertEqual(context["summary"]["same_video_decided_runs"], 1)
        self.assertEqual(context["summary"]["same_video_success_rate"], 0)
        self.assertEqual(list(context["failure_modes"]["blocked_reasons"]), ["camera", "tracking"])

    def test_failed_runs_get_deduplicated_lifecycle_logs(self):
        log = self.context()["recent_runs"][0]["log"]
        self.assertEqual([(l["event"], l["value"]) for l in log],
                         [("HarnessEvent", "planning"), ("ToolCalled", "execute_motor_program"), ("HarnessEvent", "tool_failed"),
                          ("ToolFailed", "execute_motor_program"), ("ErrorClass", "tracking")])

    def test_unavailable_keewano_is_reported_not_raised(self):
        with patch.dict("os.environ", {"KEEWANO_PROJECT_ID": "", "KEEWANO_EMAIL": "", "KEEWANO_PAT": ""}):
            self.assertFalse(run_analytics.run_context("episode-2", "abc")["available"])
        with patch.dict("os.environ", {"KEEWANO_PROJECT_ID": "p", "KEEWANO_EMAIL": "e", "KEEWANO_PAT": "t"}), \
                patch.object(run_analytics, "_connect", side_effect=OSError("down")):
            self.assertFalse(run_analytics.run_context("episode-2", "abc")["available"])

    def test_log_query_rejects_non_uuid_entities(self):
        with patch.object(run_analytics, "_query") as query:
            self.assertEqual(run_analytics.fetch_run_logs(["x') OR ('1'='1"]), {})
        query.assert_not_called()


class TraceTests(unittest.TestCase):
    def test_execution_trace_is_ordered_and_categorized(self):
        run = {"id": "r" * 32, "kind": "execution", "result": "failure", "max_tracking": 4.26,
               "initial_state": {"temperatures": {"a": 41, "b": 44.6}},
               "verification": {"confidence": .93},
               "events": [{"at": 100, "event": "checking_scene"}, {"at": 101, "event": "protocol_event"},
                          {"at": 102, "event": "tool_failed", "tool": "execute_motor_program", "error": "Wrist camera lost during motion"},
                          {"at": 103, "event": "finished", "verdict": "failure"}]}
        plan = {"skill_id": "episode-2", "sha256": "f" * 64, "speed": 2.0, "retrieval": {"video_sha256": "v" * 64},
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
        self.assertEqual(by_name["HarnessRunId"], "r" * 32)
        self.assertEqual(by_name["VideoSampleHash"], "v" * 16)
        self.assertEqual(by_name["RunDurationSec"], 3)
        events = trace_events(run, plan)
        self.assertEqual([e["timestamp"] for e in events], sorted(e["timestamp"] for e in events))
        self.assertNotIn("protocol_event", [e["data"] for e in events])


class ProjectOverviewTests(unittest.TestCase):
    ROWS = [
        [["robot_harness", 10], ["claude_code", 2]],
        [[6]],
        [["pick_cup", "sim-pick_cup", "success", 4], ["pick_cup", "sim-pick_cup", "failure", 2],
         ["episode-2", "abc123", "success", 1], [None, None, "blocked", 3], ["wave", "sim-wave", "success", 5]],
        [["no_confident_match", 3], ["deadline", 5]],
        [["Bash", 1]],
    ]

    def overview(self):
        rows = iter(self.ROWS)
        with patch.dict("os.environ", {"KEEWANO_PROJECT_ID": "p", "KEEWANO_EMAIL": "e", "KEEWANO_PAT": "t"}), \
                patch.object(run_analytics, "_connect"), patch.object(run_analytics, "_query", side_effect=lambda _sql: next(rows)):
            return run_analytics.project_overview()

    def test_rollup_ranks_worst_skill_first_and_flags_simulated(self):
        o = self.overview()
        self.assertEqual((o["total_runs"], o["simulated_runs"]), (12, 6))
        self.assertEqual([s["skill_id"] for s in o["skills"]], ["pick_cup", "wave", "episode-2"])
        self.assertEqual(o["skills"][0]["success_rate"], .67)
        self.assertTrue(o["skills"][0]["simulated"])
        self.assertFalse(o["skills"][2]["simulated"])
        self.assertEqual(list(o["blocked_reasons"]), ["deadline", "no_confident_match"])

    def test_unavailable_keewano_is_reported_not_raised(self):
        with patch.dict("os.environ", {"KEEWANO_PROJECT_ID": "", "KEEWANO_EMAIL": "", "KEEWANO_PAT": ""}):
            self.assertFalse(run_analytics.project_overview()["available"])
        with patch.dict("os.environ", {"KEEWANO_PROJECT_ID": "p", "KEEWANO_EMAIL": "e", "KEEWANO_PAT": "t"}), \
                patch.object(run_analytics, "_connect", side_effect=OSError("down")):
            self.assertFalse(run_analytics.project_overview()["available"])


if __name__ == "__main__":
    unittest.main()
