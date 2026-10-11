"""Torque-off confirmation with lost or fault-flagged write acknowledgements."""
import unittest
import threading
from unittest.mock import Mock, patch

import record_server
from record_server import read_torque_enable, release_all_torque

COMM_SUCCESS, COMM_RX_TIMEOUT = 0, -3000


class TorqueReleaseTests(unittest.TestCase):
    def test_emergency_stop_uses_per_joint_verified_release(self):
        recorder = record_server.Recorder.__new__(record_server.Recorder)
        recorder.hw_lock = threading.RLock()
        recorder.robot = Mock()
        recorder.teleop = Mock()
        with patch.object(record_server, "release_all_torque") as release:
            self.assertEqual(recorder._kill_torque(), ["follower", "teleop"])
        self.assertEqual(release.call_args_list[0].args, (recorder.robot.bus,))
        self.assertEqual(release.call_args_list[1].args, (recorder.teleop.bus,))

    def test_fault_prevents_configuration_and_reports_named_motor(self):
        robot = Mock()
        robot.bus.motors = {"gripper": Mock(id=6, model="sts3215")}
        robot.bus._read.return_value = (0, COMM_SUCCESS, 0x20)
        robot.bus._is_comm_success.return_value = True
        with patch.object(record_server, "release_all_torque"), \
             patch("lerobot.motors.motors_bus.get_address", return_value=(40, 1)):
            with self.assertRaisesRegex(RuntimeError, "gripper.*overload"):
                record_server.configure_at_measured_pose(robot)
        robot.configure.assert_not_called()
        robot.bus.sync_write.assert_not_called()

    def bus(self, readback):
        bus = Mock()
        bus.motors = {"shoulder_lift": object(), "elbow_flex": object()}
        bus.disable_torque.side_effect = [RuntimeError("ack lost"), None]
        return bus

    def release(self, bus, readback):
        with patch.object(record_server, "read_torque_enable", side_effect=readback) as read:
            release_all_torque(bus)
        return read

    def test_lost_ack_with_repeated_off_readback_succeeds(self):
        bus = self.bus([0, 0])
        read = self.release(bus, [0, 0])
        self.assertEqual(bus.disable_torque.call_count, 2)
        self.assertEqual(read.call_count, 2)
        read.assert_called_with(bus, "shoulder_lift")

    def test_torque_on_or_inconsistent_readback_is_rejected(self):
        for readback in ([1], [0, 1], [None]):
            with self.subTest(readback=readback):
                bus = self.bus(readback)
                with self.assertRaisesRegex(RuntimeError, "shoulder_lift"):
                    self.release(bus, readback)
                self.assertEqual(bus.disable_torque.call_count, 2)

    def test_unreadable_motor_is_rejected_but_other_joints_are_attempted(self):
        bus = self.bus(None)
        with self.assertRaisesRegex(RuntimeError, "read timeout"):
            self.release(bus, ConnectionError("read timeout"))
        self.assertEqual(bus.disable_torque.call_count, 2)

    def test_successful_ack_needs_no_fallback(self):
        bus = Mock()
        bus.motors = {"gripper": object()}
        with patch.object(record_server, "read_torque_enable") as read:
            release_all_torque(bus)
        read.assert_not_called()


class ReadTorqueEnableTests(unittest.TestCase):
    def bus(self, value, comm, error):
        bus = Mock()
        bus.motors = {"shoulder_lift": Mock(id=2, model="sts3215")}
        bus._read.return_value = (value, comm, error)
        bus._is_comm_success.side_effect = lambda c: c == COMM_SUCCESS
        bus._is_error.side_effect = lambda e: e != 0
        return bus

    def read(self, bus):
        with patch("lerobot.motors.motors_bus.get_address", return_value=(40, 1)):
            return read_torque_enable(bus, "shoulder_lift")

    def test_latched_fault_flag_still_returns_value(self):
        # 0x10: STS angle-limit fault after an out-of-range goal write.
        self.assertEqual(self.read(self.bus(0, COMM_SUCCESS, 0x10)), 0)

    def test_comm_failure_is_fatal(self):
        with self.assertRaises(ConnectionError):
            self.read(self.bus(0, COMM_RX_TIMEOUT, 0))


if __name__ == "__main__":
    unittest.main()
