"""Run with the LeRobot Python: python -m unittest discover -s bridge/scripts -p 'test_*.py'.

Fake hardware verifies the real interpreter/recorder/harness integration, not
physical contact, collision clearance, grasping, or visual model accuracy.
"""
import base64
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from motion_program import JOINTS, MotionProgram
from record_server import Recorder, encode_jpeg
import task_harness
from task_harness import TaskHarness, outcome, task_contract


def pose(value=0):
    return {k: float(value) for k in JOINTS}


def frames():
    return [{"t": 0, "pose": pose()}, {"t": .3, "pose": pose(1)}]


class FakeBus:
    def __init__(self):
        self.positions = {k.removesuffix(".pos"): v for k, v in pose().items()}
        self.motors = dict(self.positions)
        self.port = "fake"
        self.torque = False
        self.partial = False
        self.history = []

    def sync_read(self, *args, **kwargs):
        result = dict(self.positions)
        if self.partial:
            result.pop("gripper")
        return result

    def disable_torque(self, *args, **kwargs):
        self.torque = False
        self.history.append("disable")

    def enable_torque(self, *args, **kwargs):
        self.torque = True
        self.history.append("enable")


class FakeCamera:
    is_connected = True
    thread = SimpleNamespace(is_alive=lambda: True)
    latest_timestamp = 0
    failed = False
    def read_latest(self, **kwargs):
        if self.failed:
            raise RuntimeError("Camera disconnected")
        self.latest_timestamp = time.perf_counter()
        return np.full((48, 64, 3), 120, dtype=np.uint8)


class FakeRobot:
    def __init__(self):
        self.bus = FakeBus()
        self.cameras = {"wrist": FakeCamera()}
        self.observation_features = {**{k: float for k in JOINTS}, "wrist": (48, 64, 3)}
        self.action_features = {k: float for k in JOINTS}
        self.is_connected = True

    def send_action(self, target):
        self.bus.history.append(dict(target))
        if self.bus.torque:
            self.bus.positions = {k.removesuffix(".pos"): v for k, v in target.items()}
        return dict(target)


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = patch.object(task_harness, "STORE", Path(self.tmp.name))
        self.store.start()
        dataset = SimpleNamespace(features={"observation.images.wrist": {"dtype": "video"}}, fps=30,
                                  repo_id="fake", meta=SimpleNamespace(total_episodes=3))
        self.recorder = Recorder(FakeRobot(), FakeRobot(), dataset, 30, "test", 120, camera_meta={"wrist": 0})
        self.recorder._update_hardware = lambda _: None
        self.recorder.step()
        self.harness = self.recorder.harness
        from skill_memory import SkillMemory
        self.harness.memory = SkillMemory(Path(self.tmp.name)/"memory.sqlite3")
        self.embed = patch("skill_memory.embed", return_value=[1., 0.])
        self.embed.start()
        # Never ship test runs to the real Keewano project, even with a key configured.
        self.analytics = patch.dict("os.environ", {"KEEWANO_API_KEY": "", "KEEWANO_PROJECT_ID": "", "KEEWANO_EMAIL": "", "KEEWANO_PAT": ""})
        self.analytics.start()
        self.loop_stop = threading.Event()
        self.loop = None

    def tearDown(self):
        self.loop_stop.set()
        if self.loop:
            self.loop.join(2)
        self.store.stop()
        self.embed.stop()
        self.analytics.stop()
        self.tmp.cleanup()

    def loop_start(self):
        def loop():
            while not self.loop_stop.wait(1/30):
                self.recorder.step()
        self.loop = threading.Thread(target=loop)
        self.loop.start()

    def add_skill(self):
        skill = {"id": "episode-2", "episode": 2, "description": "Pick and place the orange block in the bin", "approved": True,
                 "frames": frames(), "duration": .3, "conversion": "none", "start_pose": pose(), "sha256": "test",
                 "contract": task_contract("Pick and place the orange block in the bin"),
                 "reference": base64.b64encode(self.recorder.fresh_jpeg("wrist")).decode()}
        (Path(self.tmp.name)/"skill-2.json").write_text(json.dumps(skill))
        self.harness.plan = {"id": "plan", "at": time.time(), "skill_id": "episode-2", "sha256": "test", "prompt": "Place the orange block in the bin"}

    def wait_job(self):
        deadline = time.monotonic()+10
        while self.harness.active and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertFalse(self.harness.active)

    def test_follower_tracks_each_fresh_leader_command(self):
        self.recorder.engaged = True
        self.recorder.robot.bus.torque = True
        for value in (2, 4, 1):
            self.recorder.teleop.bus.positions = {k.removesuffix(".pos"): v for k, v in pose(value).items()}
            _, action = self.recorder._teleop()
            self.assertEqual(action, pose(value))
            self.assertEqual(self.recorder.robot.bus.positions, self.recorder.teleop.bus.positions)
            # The following observation confirms that the commanded pose was reached.
            self.recorder._teleop()
            self.assertTrue(self.recorder._control_ready)

    def test_partial_leader_read_never_sends_cached_commands(self):
        self.recorder.engaged = True
        self.recorder.robot.bus.torque = True
        self.recorder.teleop.bus.partial = True
        before = len(self.recorder.robot.bus.history)
        observation, action = self.recorder._teleop()
        self.assertIsNone(observation)
        self.assertIsNone(action)
        self.assertEqual(len(self.recorder.robot.bus.history), before)
        self.assertFalse(self.recorder._control_ready)

    def test_nonfinite_leader_read_is_rejected(self):
        self.recorder.teleop.bus.positions["gripper"] = float("nan")
        with self.assertRaises(RuntimeError):
            self.recorder._read_teleop_action()

    def test_queued_record_rechecks_control_before_capture(self):
        self.recorder._command = "record"
        self.recorder.engaged = True
        self.recorder._control_ready = False
        with patch.object(self.recorder, "_begin") as begin:
            self.recorder._dispatch()
        begin.assert_not_called()
        self.assertFalse(self.recorder._command_result["ok"])

    def test_sms_confirmation_is_bound_to_plan_and_never_starts_motion(self):
        self.add_skill()
        request_id = "a"*32
        self.harness.plan.update(source="dial_sms", request_id=request_id, sms_confirmed=False)
        with self.assertRaisesRegex(ValueError, "customer's SMS"):
            self.harness.command("run", {"plan_id": "plan", "scene_confirmed": True})
        with self.assertRaisesRegex(ValueError, "missing, replaced"):
            self.harness.command("confirm_sms", {"plan_id": "plan", "request_id": "b"*32})
        self.harness.command("confirm_sms", {"plan_id": "plan", "request_id": request_id})
        self.assertTrue(self.harness.plan["sms_confirmed"])
        self.assertFalse(self.harness.active)
        self.assertFalse(self.recorder.robot.bus.torque)
        with self.assertRaisesRegex(ValueError, "workcell"):
            self.harness.command("run", {"plan_id": "plan"})
        self.harness.plan["at"] = time.time()-301
        with self.assertRaisesRegex(ValueError, "expired"):
            self.harness.command("confirm_sms", {"plan_id": "plan", "request_id": request_id})

    def test_sms_execution_run_retains_parent_request_and_plan(self):
        self.add_skill()
        request_id = "a"*32
        self.harness.plan.update(source="dial_sms", request_id=request_id, sms_confirmed=True)
        def refused(_payload):
            self.harness.run["result"] = "blocked"
        with patch.object(self.harness, "_execute_codex", side_effect=refused):
            self.harness.command("run", {"plan_id": "plan", "scene_confirmed": True})
            self.wait_job()
        self.assertEqual(self.harness.run["request_id"], request_id)
        self.assertEqual(self.harness.run["plan_id"], "plan")
        self.assertEqual(self.harness.run["kind"], "execution")
        self.assertFalse(self.recorder.robot.bus.torque)

    def test_sms_adapter_reaches_actual_http_harness_without_motor_motion(self):
        import asyncio
        from datetime import datetime, timezone
        from http.server import ThreadingHTTPServer
        from dial_sms import Adapter, Bridge, State
        from record_server import make_handler
        self.add_skill()
        skill_file = Path(self.tmp.name)/"skill-2.json"
        skill = json.loads(skill_file.read_text())
        skill["demonstration_evidence"] = {"source": "test_fixture", "measured_samples": 0, "samples": []}
        skill_file.write_text(json.dumps(skill))
        prompt = "Put the orange block in the bin"
        def plan_model(_prompt, _tools, dispatch, timeout=120):
            dispatch("retrieve_skills", {"query": prompt})
            dispatch("read_motor_state", {})
            dispatch("read_example", {"skill_id": "episode-2"})
            dispatch("read_robot_logs", {})
            dispatch("build_motor_program", {"skill_id": "episode-2", "reason": "Replay the taught fixed-cell task."})
        matches = {"matches": [{"skill_id": "episode-2", "score": .99}], "history": []}
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.recorder))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        state = State(Path(self.tmp.name)/"sms")
        messages = []
        async def exercise():
            async def send(to, body):
                messages.append((to, body))
            adapter = Adapter(state, Bridge(f"http://127.0.0.1:{server.server_port}"), send,
                              "+14155550999", {"+14155550123"})
            def event(body, event_id):
                return {"id": event_id, "type": "message.received", "createdAt": datetime.now(timezone.utc).isoformat(),
                        "data": {"messageId": event_id, "from": "+14155550123", "to": "+14155550999",
                                 "channel": "sms", "source": "external", "body": body}}
            request = event(prompt, "request")
            await adapter.handle(request)
            self.wait_job()
            await adapter.poll()
            row = dict(state.active()[0])
            self.assertIn("RUN "+row["token"], messages[-1][1])
            self.assertEqual(self.harness.plan["request_id"], row["id"])
            self.assertFalse(self.harness.plan["sms_confirmed"])
            await adapter.handle(event("RUN "+row["token"], "confirmation"))
            await adapter.handle(request)
            self.assertTrue(self.harness.plan["sms_confirmed"])
            self.assertEqual(state.active()[0]["status"], "confirmed")
            self.assertEqual(self.harness.run["kind"], "planning")
            self.assertFalse(self.recorder.robot.bus.torque)
            self.assertFalse(self.harness.active)
        try:
            with patch.object(self.harness, "_codex", side_effect=plan_model), patch.object(self.harness.memory, "search", return_value=matches):
                asyncio.run(exercise())
        finally:
            state.close()
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_rate_limited_interpolation_and_completion(self):
        program = MotionProgram([{"t": 0, "pose": pose()}, {"t": .1, "pose": pose(10)}])
        self.assertAlmostEqual(program.duration, 10/3)
        program.start(pose(), 0)
        measured = pose()
        for i in range(1, 110):
            target = program.tick(measured, i/30)
            self.assertLessEqual(max(abs(target[k]-measured[k]) for k in JOINTS), 3/30+1e-9)
            measured = target
        self.assertTrue(program.complete)

    def test_slow_speed_is_enforced_on_every_command(self):
        program = MotionProgram([{"t": 0, "pose": pose()}, {"t": .1, "pose": pose(10)}], speed=1)
        program.start(pose(4), 0)  # Initial pose offset must also move slowly.
        target = program.tick(pose(4), .1)
        self.assertLessEqual(max(abs(target[k]-pose(4)[k]) for k in JOINTS), .1+1e-9)
        for speed in (10, 3.01, True, float("nan"), 0, -1):
            with self.assertRaises(ValueError):
                MotionProgram(frames(), speed=speed)
        program = MotionProgram([{"t": 0, "pose": pose()}, {"t": .1, "pose": pose(10)}])
        program.start(pose(), 0)
        target = program.tick(pose(), .4)  # Delayed ticks never create a large catch-up step.
        self.assertLessEqual(max(abs(target[k]-pose()[k]) for k in JOINTS), .15+1e-9)

    def test_reject_invalid_programs(self):
        for value in (float("nan"), float("inf"), 101, -101, True):
            bad = frames()
            bad[1]["pose"]["shoulder_pan.pos"] = value
            with self.assertRaises(ValueError):
                MotionProgram(bad)
        bad = frames()
        bad[1]["t"] = 0
        with self.assertRaises(ValueError):
            MotionProgram(bad)

    def test_start_gap_and_watchdog(self):
        program = MotionProgram(frames())
        with self.assertRaises(ValueError):
            program.start(pose(6), 0)
        program.start(pose(), 0)
        with self.assertRaises(RuntimeError):
            program.tick(pose(), .51)

    def test_partial_read_never_refreshes_timestamp(self):
        self.recorder.robot.bus.partial = True
        before = self.recorder._last_robot_pos_at
        with self.assertRaises(RuntimeError):
            self.recorder._read_robot_observation()
        self.assertEqual(before, self.recorder._last_robot_pos_at)

    def test_teaching_and_autonomy_exclusive(self):
        self.recorder.start_autonomy(frames(), threading.Event())
        self.assertFalse(self.recorder.command("record")["ok"])
        self.assertFalse(self.recorder.command("engage")["ok"])
        history = self.recorder.robot.bus.history
        self.assertEqual(history[0], "disable")
        self.assertIsInstance(history[1], dict)
        self.assertEqual(history[2], "enable")
        self.recorder.emergency_stop("test")
        self.assertFalse(self.recorder.robot.bus.torque)
        self.assertFalse(self.recorder.autonomy_status()["active"])

    def test_camera_loss_stops_motion(self):
        self.recorder.start_autonomy(frames(), threading.Event())
        self.recorder.robot.cameras["wrist"].failed = True
        self.recorder.step()
        self.assertTrue(self.recorder.estop)
        self.assertFalse(self.recorder.robot.bus.torque)

    def test_stale_motors_block_start(self):
        self.recorder._last_robot_pos_at = 0
        with self.assertRaises(RuntimeError):
            self.recorder.start_autonomy(frames(), threading.Event())
        self.assertFalse(self.recorder.robot.bus.torque)

    def test_failed_torque_enable_cannot_leave_autonomy_running(self):
        with patch.object(self.recorder.robot.bus, "enable_torque", side_effect=RuntimeError("Bus error")):
            with self.assertRaises(RuntimeError):
                self.recorder.start_autonomy(frames(), threading.Event())
        self.assertTrue(self.recorder.estop)
        self.assertFalse(self.recorder.autonomy_status()["active"])
        self.assertFalse(self.recorder.robot.bus.torque)

    def test_uncertain_vision_never_passes(self):
        self.assertEqual(outcome({"verdict": "success", "confidence": .89, "evidence": "hidden"}), "uncertain")
        self.assertEqual(outcome({"verdict": "uncertain", "confidence": 1, "evidence": "occluded"}), "uncertain")
        with self.assertRaises(ValueError):
            outcome({"verdict": "success", "confidence": True, "evidence": "x"})

    def test_closed_loop_motor_program_and_verification(self):
        self.add_skill()
        self.loop_start()
        responses = iter([({"match": True, "confidence": .95, "evidence": "Same cell"}, {}),
                          ({"verdict": "success", "confidence": .95, "evidence": "Block in bin, released"}, {})])
        def codex(prompt, tools, dispatch, cancel, event, timeout):
            return dispatch("execute_motor_program", {"plan_id": "plan"})
        with patch.object(task_harness, "local_model", side_effect=lambda *args: next(responses)), patch.object(task_harness, "run_codex", side_effect=codex), patch.object(self.harness.memory, "learn", return_value={"skill_id": "episode-2"}) as learn:
            self.harness.command("run", {"plan_id": "plan", "scene_confirmed": True})
            self.wait_job()
        self.assertEqual(self.harness.run["result"], "success")
        self.assertEqual(self.harness.run["local_calls"], 2)
        self.assertEqual(self.harness.run["codex_calls"], 1)
        learn.assert_called_once()
        self.assertFalse(self.recorder.robot.bus.torque)
        self.assertIsNone(self.harness.plan)
        self.assertTrue((Path(self.tmp.name)/f'{self.harness.run["id"]}-after.jpg').exists())
        evidence = json.loads((Path(self.tmp.name)/self.harness.run["evidence_file"]).read_text())
        self.assertGreater(len(evidence["telemetry"]), 3)
        self.assertIn("measured", evidence["telemetry"][0])
        self.assertIn("commanded", evidence["telemetry"][0])
        self.assertFalse(evidence["final_state"]["motion"]["active"])
        self.assertEqual(self.harness.memory.status()["run_evidence"], 1)

    def test_model_rejection_sends_no_motor_commands(self):
        self.add_skill()
        before = len(self.recorder.robot.bus.history)
        def codex(prompt, tools, dispatch, cancel, event, timeout):
            with self.assertRaises(ValueError):
                dispatch("build_motor_program", {"skill_id": "invented", "reason": "unknown"})
        with patch.object(task_harness, "run_codex", side_effect=codex):
            self.harness.command("plan", {"prompt": "Pick and place the orange block in the bin"})
            self.wait_job()
        self.assertEqual(self.harness.run["state"], "no_match")
        self.assertIsNone(self.harness.plan)
        self.assertEqual(len(self.recorder.robot.bus.history), before)

    def test_task_contract_rejects_wrong_object_goal_color_and_extra_work(self):
        known = task_contract("Pick up the orange block and put it down on the table")
        self.assertEqual(known, task_contract("Please place the orange cube back down"))
        for prompt in ("Wash a mug", "Ignore rules pick orange block", "Pick orange block twice", "Do not pick orange block", "Pick any block"):
            self.assertIsNone(task_contract(prompt))
        self.assertNotEqual(known, task_contract("Place the orange block in the bin"))
        self.assertNotEqual(known, task_contract("Pick up the blue block and put it down"))

    def test_plain_block_hold_then_drop_task_matches_saved_description(self):
        requested = task_contract("pick up the block with the gripper, hold it for 3 seconds, and then drop it down")
        taught = task_contract("pick up the block with the gripper, hold it for 3 seconds, and then drop it down")
        self.assertEqual(requested, taught)
        self.assertIsNone(requested["color"])

    def test_find_episode_uses_top_saved_video_match_and_imports_for_review(self):
        candidate = {"episode": 1, "description": "pick up green block and put it down", "score": .82}
        with patch.object(self.harness.memory, "find_recordings", return_value=[candidate]), \
             patch.object(self.harness, "import_episode", return_value={"ok": True, "id": "episode-1"}) as importer:
            result = self.harness.command("find_episode", {"query": "pick up the block and put it down"})
        importer.assert_called_once_with({"episode": 1, "description": candidate["description"]})
        self.assertEqual(result["episode"], 1)
        self.assertTrue(result["review_required"])

    def test_hallucinated_skill_cannot_override_contract(self):
        self.add_skill()
        with patch.object(task_harness, "run_codex") as model:
            self.harness.command("plan", {"prompt": "Wash a mug"})
            self.wait_job()
            model.assert_not_called()
        self.assertIsNone(self.harness.plan)

    def test_expired_plan_and_duplicate_run_rejected(self):
        self.add_skill()
        self.harness.plan["at"] = time.time()-301
        with self.assertRaises(ValueError):
            self.harness.command("run", {"plan_id": "plan", "scene_confirmed": True})
        self.harness.plan["at"] = time.time()
        self.harness.active = True
        with self.assertRaises(ValueError):
            self.harness.command("run", {"plan_id": "plan", "scene_confirmed": True})

    def test_planner_must_inspect_retrieved_example_telemetry_and_logs(self):
        self.add_skill()
        skill = self.harness.catalog()[0]
        skill["demonstration_evidence"] = {"source": "test parquet", "measured_samples": 2,
                    "samples": [{"measured": pose(.25), "recorded_action": pose()} for _ in frames()]}
        (Path(self.tmp.name)/"skill-2.json").write_text(json.dumps(skill))
        result = {"matches": [{"skill_id": "episode-2", "score": .9}], "history": [], "experiences": []}
        def codex(prompt, tools, dispatch, cancel, event, timeout):
            dispatch("retrieve_skills", {"query": "Place the orange block in the bin"})
            with self.assertRaises(ValueError):
                dispatch("build_motor_program", {"skill_id": "episode-2", "reason": "skip readings"})
            telemetry = dispatch("read_motor_state", {})
            self.assertTrue(telemetry["hardware_available"])
            with self.assertRaises(ValueError):
                dispatch("read_example", {"skill_id": "episode-99"})
            with self.assertRaises(ValueError):
                dispatch("read_example", {"skill_id": "episode-2", "run_id": "other-run"})
            example = dispatch("read_example", {"skill_id": "episode-2"})
            self.assertEqual(example["samples"][0]["measured"], pose(.25))
            self.assertEqual(example["samples"][0]["replay_target"], pose())
            dispatch("read_robot_logs", {})
            dispatch("build_motor_program", {"skill_id": "episode-2", "reason": "Inspected measured demonstration and live state"})
        before = len(self.recorder.robot.bus.history)
        with patch.object(self.harness.memory, "search", return_value=result), patch.object(task_harness, "run_codex", side_effect=codex):
            self.harness.command("plan", {"prompt": "Place the orange block in the bin"})
            self.wait_job()
        self.assertIsNotNone(self.harness.plan)
        self.assertEqual(len(self.recorder.robot.bus.history), before)

    def plan_with_policy(self, policy):
        self.add_skill()
        self.harness.plan = None
        skill = self.harness.catalog()[0]
        skill["demonstration_evidence"] = {"source": "test parquet", "measured_samples": 2,
                    "samples": [{"measured": pose(), "recorded_action": pose()} for _ in frames()]}
        (Path(self.tmp.name)/"skill-2.json").write_text(json.dumps(skill))
        result ={"matches": [{"skill_id": "episode-2", "score": .9}], "history": [], "experiences": []}
        seen = {}
        def codex(prompt, tools, dispatch, cancel, event, timeout):
            dispatch("retrieve_skills", {"query": "Place the orange block in the bin"})
            dispatch("read_motor_state", {})
            dispatch("read_example", {"skill_id": "episode-2"})
            seen["analytics"] = dispatch("read_run_analytics", {"skill_id": "episode-2"})
            dispatch("read_robot_logs", {})
            try:
                dispatch("build_motor_program", {"skill_id": "episode-2", "reason": "History-informed replay"})
            except ValueError as error:
                seen["error"] = str(error)
        with patch.object(self.harness.memory, "search", return_value=result), patch.object(task_harness, "run_codex", side_effect=codex), \
                patch.object(task_harness.run_analytics, "recommend", return_value=policy) as recommend, \
                patch.object(task_harness.run_analytics, "export_run") as export:
            self.harness.command("plan", {"prompt": "Place the orange block in the bin"})
            self.wait_job()
        recommend.assert_called_once_with("episode-2", "test")
        export.assert_called_once()
        return seen

    def test_run_history_slows_planned_replay(self):
        seen = self.plan_with_policy({"speed": 1.5, "policy": "slowed_by_history", "hold": False, "success_rate": .9, "reason": "3 failed fast runs"})
        self.assertEqual(seen["analytics"]["speed_policy"]["speed"], 1.5)
        self.assertFalse(seen["analytics"]["keewano"]["available"])
        self.assertEqual(self.harness.plan["speed"], 1.5)
        self.assertEqual(self.harness.plan["policy"]["policy"], "slowed_by_history")
        self.assertGreater(self.harness.plan["duration"], MotionProgram(frames()).duration)

    def test_run_history_hold_blocks_plan(self):
        seen = self.plan_with_policy({"speed": 1.0, "policy": "hold", "hold": True, "success_rate": None, "reason": "Run history shows this motion failing"})
        self.assertIn("Run history", seen["error"])
        self.assertIsNone(self.harness.plan)

    def test_execution_uses_planned_speed(self):
        self.add_skill()
        self.harness.plan["speed"] = 1.5
        self.loop_start()
        responses = iter([({"match": True, "confidence": .95, "evidence": "Same cell"}, {}),
                          ({"verdict": "success", "confidence": .95, "evidence": "Block in bin, released"}, {})])
        def codex(prompt, tools, dispatch, cancel, event, timeout):
            return dispatch("execute_motor_program", {"plan_id": "plan"})
        with patch.object(task_harness, "local_model", side_effect=lambda *args: next(responses)), patch.object(task_harness, "run_codex", side_effect=codex), \
                patch.object(self.harness.memory, "learn", return_value={"skill_id": "episode-2"}), \
                patch.object(self.recorder, "start_autonomy", wraps=self.recorder.start_autonomy) as start:
            self.harness.command("run", {"plan_id": "plan", "scene_confirmed": True})
            self.wait_job()
        self.assertEqual(start.call_args.args[2], 1.5)
        self.assertEqual(self.harness.run["result"], "success")
        self.assertIsNotNone(self.harness.run.get("max_tracking"))

    def test_demo_missing_or_wrong_joint_order_is_not_faked(self):
        info = {"features": {"observation.state": {"names": list(JOINTS)}}}
        rows = [{"timestamp": 0., "action": [0.]*6, "observation.state": [.2]*6},
                {"timestamp": .1, "action": [1.]*6}]
        evidence = task_harness.episode_readings(rows, info)
        self.assertEqual(evidence["measured_samples"], 1)
        self.assertIsNone(evidence["samples"][1]["measured"])
        info["features"]["observation.state"]["names"].reverse()
        self.assertEqual(task_harness.episode_readings(rows, info)["measured_samples"], 0)

    def test_blocked_execution_persists_failure_evidence_without_learning(self):
        self.add_skill()
        self.recorder._last_robot_pos_at = 0
        def codex(prompt, tools, dispatch, cancel, event, timeout):
            dispatch("execute_motor_program", {"plan_id": "plan"})
        with patch.object(task_harness, "run_codex", side_effect=codex), patch.object(self.harness.memory, "learn") as learn:
            self.harness.command("run", {"plan_id": "plan", "scene_confirmed": True})
            self.wait_job()
        self.assertEqual(self.harness.run["result"], "blocked")
        self.assertEqual(self.harness.memory.status()["run_evidence"], 1)
        self.assertEqual(self.harness.memory.status()["experiences"], 0)
        learn.assert_not_called()
        self.assertFalse(self.recorder.robot.bus.torque)

    def test_embedding_outage_preserves_local_execution_evidence(self):
        self.add_skill()
        self.recorder._last_robot_pos_at = 0
        def codex(prompt, tools, dispatch, cancel, event, timeout):
            dispatch("execute_motor_program", {"plan_id": "plan"})
        with patch.object(task_harness, "run_codex", side_effect=codex), patch.object(self.harness.memory, "record_run", side_effect=RuntimeError("embedding offline")):
            self.harness.command("run", {"plan_id": "plan", "scene_confirmed": True})
            self.wait_job()
        self.assertTrue((Path(self.tmp.name)/self.harness.run["evidence_file"]).is_file())
        self.assertEqual(self.harness.run["events"][-1]["event"], "memory_write_failed")

    def test_example_pages_and_log_reads_are_bounded(self):
        self.add_skill()
        skill = self.harness.catalog()[0]
        skill["frames"] = [{"t": i/30, "pose": pose()} for i in range(50)]
        skill["demonstration_evidence"] = {"source": "unavailable", "measured_samples": 0, "samples": []}
        page = self.harness._example(skill, 24)
        self.assertEqual(len(page["samples"]), 24)
        self.assertEqual(page["next_offset"], 48)
        self.assertEqual(page["trajectory_overview"][-1]["index"], 49)
        with self.assertRaises(ValueError):
            self.harness._example(skill, True)
        logfile = Path(self.tmp.name)/"large.log"
        logfile.write_text("long diagnostic line\n"*1000)
        self.assertEqual(len(task_harness.log_tail(logfile)["lines"]), 20)

    def test_previous_example_returns_actual_run_evidence_and_pages(self):
        self.add_skill()
        skill = self.harness.catalog()[0]
        skill["demonstration_evidence"] = {"source": "unavailable", "measured_samples": 0, "samples": []}
        evidence = {"id": "previous", "result": "uncertain", "events": [{"event": "finished", "verdict": "uncertain"}],
                    "telemetry": [{"elapsed_s": i/30, "measured": pose(.2), "commanded": pose(.3)} for i in range(60)]}
        self.harness.memory.record_run(skill, {"prompt": skill["description"], "sha256": skill["sha256"]}, evidence)
        page = self.harness._example(skill, 0, "previous")
        self.assertEqual(page["previous_run"]["result"], "uncertain")
        self.assertEqual(len(page["previous_run"]["telemetry"]), 24)
        self.assertEqual(page["previous_run"]["telemetry"][0]["measured"], pose(.2))
        self.assertEqual(page["previous_run"]["next_offset"], 24)
        later = self.harness._example(skill, 48, "previous")
        self.assertEqual(later["samples"], [])
        self.assertEqual(len(later["previous_run"]["telemetry"]), 12)
        with self.assertRaises(ValueError):
            self.harness._example({**skill, "sha256": "changed"}, 0, "previous")


    def test_keewano_runs_of_retrieved_video_open_local_telemetry(self):
        self.add_skill()
        self.harness.plan = None
        skill = self.harness.catalog()[0]
        skill["demonstration_evidence"] = {"source": "unavailable", "measured_samples": 0, "samples": []}
        (Path(self.tmp.name)/"skill-2.json").write_text(json.dumps(skill))
        evidence = {"id": "a" * 32, "result": "failure", "events": [{"event": "tool_failed", "error": "tracking diverged"}],
                    "telemetry": [{"elapsed_s": i/30, "measured": pose(.2), "commanded": pose(.3), "tracking_delta": 7.1} for i in range(30)]}
        self.harness.memory.record_run(skill, {"prompt": skill["description"], "sha256": skill["sha256"]}, evidence)
        context = {"available": True, "recent_runs": [
            {"harness_run_id": "a" * 32, "verdict": "failure", "same_video": True},
            {"harness_run_id": "b" * 32, "verdict": "failure", "same_video": True}]}
        result = {"matches": [{"skill_id": "episode-2", "score": .9, "video_sha256": "v" * 64}], "history": [], "experiences": []}
        seen = {}
        def codex(prompt, tools, dispatch, cancel, event, timeout):
            dispatch("retrieve_skills", {"query": "Place the orange block in the bin"})
            with self.assertRaises(ValueError):
                dispatch("read_example", {"skill_id": "episode-2", "run_id": "a" * 32})
            seen["analytics"] = dispatch("read_run_analytics", {"skill_id": "episode-2"})
            seen["past"] = dispatch("read_example", {"skill_id": "episode-2", "run_id": "a" * 32})
            with self.assertRaises(ValueError):  # in Keewano but no local evidence
                dispatch("read_example", {"skill_id": "episode-2", "run_id": "b" * 32})
        with patch.object(self.harness.memory, "search", return_value=result), patch.object(task_harness, "run_codex", side_effect=codex), \
                patch.object(task_harness.run_analytics, "run_context", return_value=context) as run_context, \
                patch.object(task_harness.run_analytics, "export_run"):
            self.harness.command("plan", {"prompt": "Place the orange block in the bin"})
            self.wait_job()
        run_context.assert_called_once_with("episode-2", skill["sha256"], "v" * 64)
        runs = seen["analytics"]["keewano"]["recent_runs"]
        self.assertEqual([r["local_evidence"] for r in runs], [True, False])
        self.assertEqual(seen["past"]["previous_run"]["result"], "failure")
        self.assertEqual(seen["past"]["previous_run"]["telemetry"][0]["tracking_delta"], 7.1)


    def test_insights_answers_from_keewano_without_creating_a_run(self):
        self.add_skill()
        skill = self.harness.catalog()[0]
        ranked = [{"skill_id": "episode-2", "score": .8, "video_sha256": "v" * 64}]
        context = {"available": True, "recent_runs": [], "summary": {}}
        before = (len(self.recorder.robot.bus.history), self.harness.run, self.harness.plan)
        with patch.object(self.harness.memory, "rank_videos", return_value=ranked), \
                patch.object(task_harness.run_analytics, "run_context", return_value=context) as run_context:
            result = self.harness.command("insights", {"query": "how is the orange block task doing?"})
        run_context.assert_called_once_with("episode-2", skill["sha256"], "v" * 64, with_policy=True)
        self.assertTrue(result["query_matched_video"])
        self.assertEqual(result["skills"][0]["keewano"], context)
        self.assertFalse(result["robot_moved"])
        self.assertEqual((len(self.recorder.robot.bus.history), self.harness.run, self.harness.plan), before)
        with patch.object(self.harness.memory, "rank_videos", side_effect=OSError("embedding offline")), \
                patch.object(task_harness.run_analytics, "run_context", return_value=context):
            fallback = self.harness.insights("stats")
        self.assertEqual([s["skill_id"] for s in fallback["skills"]], ["episode-2"])
        self.assertFalse(fallback["query_matched_video"])


if __name__ == "__main__":
    unittest.main()
