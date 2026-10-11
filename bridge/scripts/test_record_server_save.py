"""Exercise actual writer durability and adding a camera between recordings."""
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import numpy as np
import pyarrow.parquet as pq

from lerobot.configs.video import RGBEncoderConfig
from lerobot.datasets.lerobot_dataset import LeRobotDataset
from record_server import Recorder, IDLE, PENDING, ESTOPPED, extend_video_camera_schema, reopen, resume_recording, save_recording_episode


class EpisodeSaveTests(unittest.TestCase):
    def test_degree_mode_bounds_come_from_encoder_calibration(self):
        from lerobot.motors import Motor, MotorNormMode, MotorCalibration
        recorder = Recorder.__new__(Recorder)
        recorder.robot = SimpleNamespace(bus=SimpleNamespace(
            motors={'shoulder_lift': Motor(2, 'sts3215', MotorNormMode.DEGREES),
                    'gripper': Motor(6, 'sts3215', MotorNormMode.RANGE_0_100)},
            calibration={'shoulder_lift': MotorCalibration(2, 0, 0, 866, 3247)},
            model_resolution_table={'sts3215': 4096}))
        self.assertAlmostEqual(recorder.motion_bounds()['shoulder_lift.pos'][1], 104.659340659)
        self.assertEqual(recorder.motion_bounds()['gripper.pos'], (0, 100))
        self.assertEqual(recorder.motion_units()['shoulder_lift.pos'], 'degrees')

    def recorder(self, frames, error=None):
        recorder = Recorder.__new__(Recorder)
        recorder.lock = threading.RLock()
        recorder.state = PENDING
        recorder.frames = frames
        recorder.saved_at_utc = None
        recorder.dataset = SimpleNamespace(has_pending_frames=lambda: bool(frames), save_episode=Mock(side_effect=error))
        recorder._command = "save"
        recorder._payload = {}
        recorder._command_done = threading.Event()
        return recorder

    def test_empty_save_returns_failure_to_http_command(self):
        recorder = self.recorder(0)
        done = recorder._command_done
        recorder._dispatch()
        self.assertTrue(done.is_set())
        self.assertFalse(recorder._command_result["ok"])
        self.assertIn("no frames", recorder._command_result["error"])
        self.assertEqual(recorder.state, IDLE)
        self.assertIsNone(recorder.saved_at_utc)

    def test_repeated_stop_and_stop_after_estop_do_not_require_teleop_readiness(self):
        recorder = self.recorder(0)
        recorder.offline = False
        recorder.engaged = False
        recorder._command = None
        for state in (IDLE, PENDING, ESTOPPED):
            recorder.state = state
            self.assertTrue(recorder.command("stop")["ok"])

    def test_failed_save_returns_error_and_prevents_reusing_poisoned_writer(self):
        recorder = self.recorder(20, RuntimeError("camera metadata missing"))
        recorder._dispatch()
        self.assertFalse(recorder._command_result["ok"])
        self.assertIn("camera metadata missing", recorder._command_result["error"])
        recorder.offline = False
        recorder._autonomy = None
        recorder.estop = False
        self.assertIn("restart the bridge", recorder.command("record")["error"])

    def test_second_camera_can_be_added_without_rewriting_old_episodes(self):
        camera = {"dtype": "video", "shape": (64, 64, 3), "names": ["height", "width", "channels"]}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/"dataset"
            ds = LeRobotDataset.create(repo_id="test/save", root=root, fps=30, use_videos=True,
                metadata_buffer_size=1, streaming_encoding=True,
                rgb_encoder=RGBEncoderConfig(vcodec="h264", preset="ultrafast"),
                features={"action": {"dtype": "float32", "shape": (1,), "names": ["joint"]},
                          "observation.images.wrist": camera})
            try:
                old_files = None
                for episode in range(3):
                    for index in range(6):
                        frame = {"action": np.array([episode], dtype=np.float32), "task": "Save test",
                                 "observation.images.wrist": np.full((64,64,3), index*30, dtype=np.uint8)}
                        if episode:
                            frame["observation.images.overhead"] = frame["observation.images.wrist"]
                        ds.add_frame(frame)
                    save_recording_episode(ds)
                    ds = reopen(ds)
                    self.assertEqual(ds.meta.total_episodes, episode+1)
                    self.assertEqual(ds.writer.episode_buffer["size"], 0)
                    self.assertEqual(sum(pq.read_metadata(p).num_rows for p in (root/"data").rglob("*.parquet")), 6*(episode+1))
                    if old_files:
                        for path, content in old_files.items():
                            self.assertEqual(path.read_bytes(), content)
                    if not episode:
                        old_files = {path: path.read_bytes() for folder in ("data", "videos") for path in (root/folder).rglob("*") if path.is_file()}
                        ds.finalize()
                        extend_video_camera_schema(root, {"observation.images.overhead": camera})
                        ds = resume_recording(repo_id="test/save", root=root, streaming_encoding=True,
                            rgb_encoder=RGBEncoderConfig(vcodec="h264", preset="ultrafast"))
                        ds.meta._metadata_buffer_size = 1
                self.assertIsNone(ds.meta.episodes[0].get("videos/observation.images.overhead/chunk_index"))
                self.assertIn("videos/observation.images.overhead/chunk_index", ds.meta.episodes[-1])
            finally:
                ds.finalize()
