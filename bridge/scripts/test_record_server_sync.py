"""The explicit sync path stays slow, direct, and gated on healthy arms."""
import math
import threading
import time
import unittest
from unittest.mock import patch

import record_server


class SyncTests(unittest.TestCase):
    def test_large_gap_extends_cosine_ramp_to_speed_limit(self):
        self.assertAlmostEqual(record_server.sync_duration(88.3, 2.0), 88.3 * math.pi / 12)
        self.assertEqual(record_server.sync_duration(1.0, 2.0), 2.0)

    def test_sync_ramps_directly_to_leader_pose(self):
        recorder = record_server.Recorder.__new__(record_server.Recorder)
        recorder.sync_seconds = 2.0
        joints = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper")
        leader = {joint: (88.3 if joint == "shoulder_lift" else 0.0) for joint in joints}
        follower = {f"{joint}.pos": 0.0 for joint in joints}
        with patch.object(recorder, "_leader_pose", return_value=leader), \
                patch.object(recorder, "_read_robot_observation", return_value=follower), \
                patch.object(recorder, "_ramp_to") as ramp:
            recorder._sync_to_leader()
        self.assertEqual(ramp.call_args.args[0], leader)
        self.assertAlmostEqual(ramp.call_args.args[1], 88.3 * math.pi / 12)

    def test_sync_rejects_arm_with_missing_motor(self):
        recorder = record_server.Recorder.__new__(record_server.Recorder)
        recorder.offline = False
        recorder.lock = threading.RLock()
        recorder.state = record_server.IDLE
        recorder.estop = False
        recorder.calib = None
        recorder._autonomy = None
        recorder._command = None
        recorder._last_robot_pos_at = recorder._last_teleop_action_at = time.time()
        recorder._delta = {"joints": {name: {} for name in
                         ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper")}}
        recorder._hardware = {"follower": {"motors_ok": 5}, "teleop": {"motors_ok": 6}}
        result = recorder.command("sync", {"confirmed": True})
        self.assertFalse(result["ok"])
        self.assertIn("5/6", result["error"])

    def test_rearm_refuses_faulted_arm(self):
        recorder = record_server.Recorder.__new__(record_server.Recorder)
        recorder.lock = threading.RLock()
        recorder.estop = True
        recorder._hardware = {"follower": {"motors_ok": 5}, "teleop": {"motors_ok": 6}}
        result = recorder.rearm()
        self.assertFalse(result["ok"])
        self.assertIn("inspect the reported motor fault", result["error"])


if __name__ == "__main__":
    unittest.main()
