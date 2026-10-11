"""Feedback tolerance must never widen recorded targets or command steps."""
import unittest
from motion_program import JOINTS, MotionProgram, validate_pose, approach_demo_start, speed_for_duration, MAX_SPEED, DEFAULT_BOUNDS


class FeedbackTests(unittest.TestCase):
    def test_thirty_second_timing_includes_approach_and_preserves_values(self):
        pose = {k: 0.0 for k in JOINTS}
        frames = [{'t': 0, 'pose': pose}, {'t': .1, 'pose': {k: 80.0 for k in JOINTS}}]
        measured = {k: 6.0 for k in JOINTS}
        speed = speed_for_duration(frames, 30, measured)
        prepared, _ = approach_demo_start(frames, measured, speed)
        self.assertAlmostEqual(MotionProgram(prepared, speed).duration, 30, places=6)
        self.assertEqual(prepared[1]['pose'], frames[0]['pose'])
        self.assertEqual(prepared[2]['pose'], frames[1]['pose'])
        self.assertEqual(frames[1]['t'], .1)

    def test_duration_request_cannot_exceed_speed_cap(self):
        frames = [{'t': 0, 'pose': {k: 0.0 for k in JOINTS}}, {'t': .1, 'pose': {k: 80.0 for k in JOINTS}}]
        self.assertEqual(speed_for_duration(frames, 1), MAX_SPEED)

    def test_live_start_approach_preserves_demo_and_step_caps(self):
        start = {k: 0.0 for k in JOINTS}
        start['elbow_flex.pos'] = 80.2
        start['shoulder_lift.pos'] = -100
        measured = dict(start, **{'elbow_flex.pos': 96, 'shoulder_lift.pos': -101.14})
        demo = [{'t': 0, 'pose': start}, {'t': 1, 'pose': dict(start, **{'elbow_flex.pos': 79})}]
        prepared, approach = approach_demo_start(demo, measured, speed=3)
        self.assertAlmostEqual(approach, 15.8/3)
        self.assertEqual([f['pose'] for f in prepared[1:]], [f['pose'] for f in demo])
        self.assertEqual(demo[0]['t'], 0)
        program = MotionProgram(prepared, speed=3)
        program.start(measured, 0)
        for i in range(1, 200):
            target = program.tick(measured, i/30)
            self.assertLessEqual(max(abs(target[k]-measured[k]) for k in JOINTS), .1+1e-9)
            measured = target
        self.assertAlmostEqual(measured['elbow_flex.pos'], 79)

    def test_automatic_approach_rejects_extreme_offsets(self):
        pose = {k: 0.0 for k in JOINTS}
        with self.assertRaisesRegex(ValueError, 'limit is 25'):
            approach_demo_start([{'t': 0, 'pose': pose}, {'t': 1, 'pose': pose}], dict(pose, **{'elbow_flex.pos': 50}))

    def test_observed_rest_pose_recovers_inward_at_command_cap(self):
        target = {k: 0.0 for k in JOINTS}
        target['shoulder_lift.pos'] = -100.0
        frames = [{'t': 0, 'pose': dict(target)}, {'t': 1, 'pose': dict(target)}]
        measured = dict(target, **{'shoulder_lift.pos': -101.142857})
        program = MotionProgram(frames, speed=3)
        program.start(measured, 0)
        for i in range(1, 20):
            command = program.tick(measured, i/30)
            self.assertLessEqual(abs(command['shoulder_lift.pos']-measured['shoulder_lift.pos']), .1+1e-9)
            self.assertGreaterEqual(command['shoulder_lift.pos'], measured['shoulder_lift.pos'])
            self.assertLessEqual(command['shoulder_lift.pos'], -100)
            measured = command
        self.assertAlmostEqual(measured['shoulder_lift.pos'], -100)

    def test_targets_remain_strict_and_feedback_rejects_large_overshoot(self):
        pose = {k: 0.0 for k in JOINTS}
        for value in (-101.14, 101.14):
            with self.assertRaises(ValueError):
                validate_pose(dict(pose, **{'shoulder_lift.pos': value}))
        for value in (-102.01, 102.01, float('nan'), True):
            with self.assertRaises(ValueError):
                validate_pose(dict(pose, **{'shoulder_lift.pos': value}), measured=True)

    def test_degree_shoulder_uses_actual_calibration_and_returns_inward(self):
        pose = {k: 0.0 for k in JOINTS}
        measured = dict(pose, **{'shoulder_lift.pos': -103.956043956})
        start = dict(pose, **{'shoulder_lift.pos': -100})
        bounds = dict(DEFAULT_BOUNDS, **{"shoulder_lift.pos": (-104.66, 104.66)})
        validate_pose(measured, bounds=bounds)
        with self.assertRaises(ValueError):
            validate_pose(dict(measured, **{"shoulder_lift.pos": -105}), bounds=bounds)
        program = MotionProgram([{'t': 0, 'pose': start}, {'t': 1, 'pose': start}], bounds=bounds)
        program.start(measured, 0)
        for i in range(1, 30):
            target = program.tick(measured, i/30)
            self.assertGreaterEqual(target['shoulder_lift.pos'], measured['shoulder_lift.pos'])
            self.assertLessEqual(abs(target['shoulder_lift.pos']-measured['shoulder_lift.pos']), program.speed/30+1e-8)
            measured = target
        self.assertAlmostEqual(measured['shoulder_lift.pos'], -100)
        with self.assertRaises(ValueError):
            validate_pose(dict(pose, **{'elbow_flex.pos': -103.956}), measured=True)

    def test_alignment_still_blocks_different_start_pose(self):
        pose = {k: 0.0 for k in JOINTS}
        program = MotionProgram([{'t': 0, 'pose': pose}, {'t': 1, 'pose': pose}])
        with self.assertRaisesRegex(ValueError, 'elbow_flex.pos'):
            program.start(dict(pose, **{'elbow_flex.pos': 16}), 0)

    def test_short_turn_holds_and_resumes_without_wall_time_jump(self):
        pose = {k: 0.0 for k in JOINTS}
        end = dict(pose, **{'elbow_flex.pos': 6})
        program = MotionProgram([{'t': 0, 'pose': pose}, {'t': 2, 'pose': end}])
        program.horizon = 1
        program.start(pose, 0)
        measured = pose
        for i in range(1, 41):
            measured = program.tick(measured, i*.05)
        self.assertAlmostEqual(measured['elbow_flex.pos'], 3)
        self.assertEqual(program.elapsed(2), 1)
        self.assertFalse(program.advance_turn(dict(measured, **{'elbow_flex.pos': 0}), 2))
        self.assertTrue(program.advance_turn(measured, 2))
        command = program.tick(measured, 2.05)
        self.assertAlmostEqual(command['elbow_flex.pos'], 3.15)

    def test_checkpoint_cannot_hold_indefinitely(self):
        pose = {k: 0.0 for k in JOINTS}
        program = MotionProgram([{'t': 0, 'pose': pose}, {'t': 10, 'pose': pose}])
        program.horizon = 2
        program.start(pose, 0)
        for i in range(1, 71):
            program.tick(pose, i*.1)
        with self.assertRaisesRegex(RuntimeError, 'checkpoint'):
            program.tick(pose, 7.1)

    def test_final_verification_hold_still_expires_with_turns(self):
        pose = {k: 0.0 for k in JOINTS}
        program = MotionProgram([{'t': 0, 'pose': pose}, {'t': 1, 'pose': pose}])
        program.horizon = 1
        program.start(pose, 0)
        for i in range(1, 911):
            program.tick(pose, i*.1)
        with self.assertRaisesRegex(RuntimeError, 'lease expired'):
            program.tick(pose, 91.1)


if __name__ == '__main__':
    unittest.main()
