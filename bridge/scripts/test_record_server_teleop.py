"""Teleop mirrors the leader without an automatic tracking-gap stop."""
import threading
import unittest
from unittest.mock import Mock

import record_server


def recorder_fixture():
    recorder = record_server.Recorder.__new__(record_server.Recorder)
    recorder.lock = threading.RLock()
    recorder.hw_lock = threading.RLock()
    recorder.fps = 30
    recorder.state = record_server.IDLE
    recorder.engaged = True
    recorder.estop = False
    recorder.delta_limit = 25
    recorder._delta = {}
    recorder._last_robot_pos_at = recorder._last_teleop_action_at = 0.0
    recorder.robot = Mock()
    recorder.robot.send_action.side_effect = lambda action: dict(action)
    recorder._read_robot_observation = Mock(return_value={"elbow_flex.pos": 0.0})
    recorder._read_teleop_action = Mock(return_value={"elbow_flex.pos": 90.0})

    def stop(reason):
        recorder.estop = True
        recorder.engaged = False
    recorder.emergency_stop = Mock(side_effect=stop)
    return recorder


class TeleopTests(unittest.TestCase):
    def test_fast_leader_does_not_trip_when_follower_tracks_sent_commands(self):
        recorder = recorder_fixture()
        measured = 0.0
        for tick in range(20):
            leader = 40.0 if tick % 2 == 0 else 0.0
            recorder._read_teleop_action.return_value = {"elbow_flex.pos": leader}
            recorder._read_robot_observation.return_value = {"elbow_flex.pos": measured}
            _, sent = recorder._teleop()
            self.assertEqual(sent["elbow_flex.pos"], leader)
            measured = sent["elbow_flex.pos"]
        recorder.emergency_stop.assert_not_called()
        self.assertGreater(recorder._delta["max"], 25)  # UI still shows leader gap.

    def test_persistent_gap_never_pauses_or_cuts_torque(self):
        recorder = recorder_fixture()
        for _ in range(60):
            recorder._teleop()
        recorder.emergency_stop.assert_not_called()
        self.assertTrue(recorder.engaged)
        self.assertFalse(recorder.estop)
        self.assertEqual(recorder.robot.send_action.call_count, 60)
        recorder.robot.send_action.assert_called_with({"elbow_flex.pos": 90.0})
        recorder._read_teleop_action.return_value = {"elbow_flex.pos": -90.0}
        recorder._teleop()
        recorder.robot.send_action.assert_called_with({"elbow_flex.pos": -90.0})
        self.assertTrue(recorder._control_ready)

    def test_stale_joint_readings_still_emergency_stop(self):
        recorder = recorder_fixture()
        recorder._read_robot_observation.side_effect = RuntimeError("joint connection lost")
        recorder._teleop()
        recorder.emergency_stop.assert_called_once()

    def test_estop_never_sends_a_leader_command(self):
        recorder = recorder_fixture()
        recorder.estop = True
        recorder._teleop()
        recorder.robot.send_action.assert_not_called()

    def test_forwards_all_six_live_joint_positions_without_slowing(self):
        recorder = recorder_fixture()
        joints = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper")
        action = {f"{joint}.pos": float(index * 7) for index, joint in enumerate(joints)}
        recorder._read_teleop_action.return_value = action
        recorder._read_robot_observation.return_value = {key: 0.0 for key in action}
        _, sent = recorder._teleop()
        self.assertEqual(sent, action)
        recorder.robot.send_action.assert_called_once_with(action)

    def test_observing_never_sends_motion(self):
        recorder = recorder_fixture()
        recorder.engaged = False
        for _ in range(10):
            recorder._teleop()
        recorder.robot.send_action.assert_not_called()


if __name__ == "__main__":
    unittest.main()
