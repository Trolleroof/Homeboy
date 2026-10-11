"""Torque-off confirmation with lost write acknowledgements."""
import unittest
from unittest.mock import Mock

from record_server import release_all_torque


class TorqueReleaseTests(unittest.TestCase):
    def bus(self, readback):
        bus = Mock()
        bus.motors = {"shoulder_lift": object(), "elbow_flex": object()}
        bus.disable_torque.side_effect = [RuntimeError("ack lost"), None]
        bus.read.side_effect = readback
        return bus

    def test_lost_ack_with_repeated_off_readback_succeeds(self):
        bus = self.bus([0, 0])
        release_all_torque(bus)
        self.assertEqual(bus.disable_torque.call_count, 2)
        self.assertEqual(bus.read.call_count, 2)
        bus.read.assert_called_with("Torque_Enable", "shoulder_lift", normalize=False, num_retry=3)

    def test_torque_on_or_inconsistent_readback_is_rejected(self):
        for readback in ([1], [0, 1], [None]):
            with self.subTest(readback=readback):
                bus = self.bus(readback)
                with self.assertRaisesRegex(RuntimeError, "shoulder_lift"):
                    release_all_torque(bus)
                self.assertEqual(bus.disable_torque.call_count, 2)

    def test_unreadable_motor_is_rejected_but_other_joints_are_attempted(self):
        bus = self.bus(RuntimeError("read timeout"))
        with self.assertRaisesRegex(RuntimeError, "read timeout"):
            release_all_torque(bus)
        self.assertEqual(bus.disable_torque.call_count, 2)

    def test_successful_ack_needs_no_fallback(self):
        bus = Mock()
        bus.motors = {"gripper": object()}
        release_all_torque(bus)
        bus.read.assert_not_called()
