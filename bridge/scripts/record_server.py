#!/usr/bin/env python
"""Recording daemon: owns the arms, the cameras, and the dataset writer, and
exposes recording, an emergency stop, recalibration and live camera streams over
HTTP so the dashboard can drive all of it from buttons.

Why not shell out to `lerobot-record`: that script owns a terminal and takes its
episode boundaries -- and its calibration prompts -- from keyboard listeners,
which a web UI cannot reach. Driving the LeRobot API directly is both simpler and
gives exact control over when an episode is committed.

The interesting part is the commit window. Stopping does *not* immediately write
the episode -- the frames sit in the writer's buffer, and `--commit-seconds`
later the loop saves them. Discarding inside that window is
`clear_episode_buffer()`: instant, and nothing was ever written, so there is no
parquet rewrite and no video re-encode. Walk away and the take is kept, which is
the safe default.

Four things run alongside the recording loop:

*   **E-stop.** `POST /estop` cuts servo torque on both arms from the HTTP
    thread, without waiting for the control loop -- see `_kill_torque`.
*   **Teleop engagement.** The loop starts *observing*: both arms are read and
    the delta is published, but nothing is driven, so bringing the daemon up
    never moves the follower. `POST /engage` hands the follower to the leader
    once the two agree; `POST /disengage` lets go again.
*   **Tracking delta.** The display compares leader and follower positions.
    Teleop forwards the live leader pose. Alignment is informational during
    motion; a tracking gap does not pause teleop or cut torque.
*   **Calibration.** The LeRobot routine blocks on `input()`; this one is a state
    machine stepped by the loop, so the phases are buttons in the browser.

Hardware is touched under `hw_lock`: the loop holds it for a tick, HTTP handlers
take it for the length of one command. The e-stop is the one caller allowed to
give up on the lock and write anyway.

    python scripts/record_server.py --repo-id homeboy/pick_block \
        --robot-port /dev/tty.usbmodemXXXX --teleop-port /dev/tty.usbmodemYYYY \
        --camera wrist=0 --camera overhead=1
"""

from __future__ import annotations

import argparse
import base64
from collections import deque
import copy
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import socket
import struct
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from power_check import aggregate_status, build_arm_power, build_camera_power, motor_probe, motor_temperatures  # noqa: E402

from lerobot.configs.video import RGBEncoderConfig
from lerobot.datasets.lerobot_dataset import LeRobotDataset
from lerobot.motors import MotorCalibration
from lerobot.utils.constants import ACTION, OBS_STR
from lerobot.utils.feature_utils import build_dataset_frame, hw_to_dataset_features

IDLE, RECORDING, PENDING, SAVING = "idle", "recording", "pending", "saving"
CALIBRATING, ESTOPPED = "calibrating", "estopped"


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

# The motor whose range is a full turn and so cannot be swept by hand.
FULL_TURN_MOTOR = "wrist_roll"
FULL_TURN_RANGE = (0, 4095)

BOUNDARY = "homeboy-frame"

# 10 Hz: a legible commit countdown and a live delta, on an already-open socket.
import math

WS_PERIOD = 0.1
DEFAULT_REST_SECONDS = 2.0
DEFAULT_START_SECONDS = 0.8
DEFAULT_SYNC_SECONDS = 0.8
MAX_SYNC_SPEED = 6.0  # Calibrated action units per second; sync takes longer for large gaps.
TRAINING_TASK = "pick up the yellow sponge"
CAMERAS_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "cameras.json"


def load_training_cameras(path: Path = CAMERAS_CONFIG_PATH) -> dict[str, int]:
    """Read the camera contract -- label -> OpenCV index -- from `config/cameras.json`.

    That file is the single source of truth: the dashboard launches this daemon
    straight from it, so a second copy of the mapping here is exactly how the
    labels get crossed. The labels are semantic and must not be renamed to suit
    the wiring -- `wrist` is the close gripper view and `overhead` the wide
    workspace view, which is what `observation.images.*` means in the dataset.
    Move the *indices* when OpenCV enumerates the rig differently.

    A missing or malformed file is fatal rather than defaulted: recording
    against the wrong index fills `observation.images.wrist` with the overhead
    view, and nothing downstream can tell.
    """
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise SystemExit(f"cannot read the camera contract at {path}: {exc}") from exc
    if not isinstance(raw, dict) or not raw:
        raise SystemExit(f"{path} must be a non-empty object of camera label -> OpenCV index")
    cameras: dict[str, int] = {}
    for name, index in raw.items():
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise SystemExit(f"{path}: camera {name!r} needs a non-negative integer index, got {index!r}")
        cameras[str(name)] = index
    if len(set(cameras.values())) != len(cameras):
        raise SystemExit(f"{path}: two cameras share an OpenCV index -- {cameras}")
    return cameras


TRAINING_CAMERAS = load_training_cameras()
# SO-101 follower rest pose, in the project's normalized joint units. Captured
# directly off the physical arm posed by hand -- not a guess -- so change this
# by posing the arm again and re-reading it, not by editing numbers by feel.
REST_POSE = {
    "shoulder_pan": 0.5,
    "shoulder_lift": -101.7,
    "elbow_flex": 95.9,
    "wrist_flex": 50.9,
    "wrist_roll": 1.4,
    # Keep the gripper away from its calibrated hard stop during discard/stop.
    # ponytail: mid-range neutral; tune against the physical jaw clearance.
    "gripper": 40.0,
}
# Pose the follower eases into the instant engage is pressed, before handing
# off to the leader -- same deal as REST_POSE: captured off the physical arm,
# not guessed. Re-pose and re-read to change it.
START_POSE = {
    "shoulder_pan": 0.6,
    "shoulder_lift": -77.0,
    "elbow_flex": 85.3,
    "wrist_flex": 50.9,
    "wrist_roll": 1.3,
    "gripper": 6.4,
}


def _ease_in_out(t: float) -> float:
    """0..1 in, 0..1 out: smooth S-curve acceleration and deceleration."""
    return 0.5 * (1.0 - math.cos(math.pi * t))


def _ease_out_cubic(t: float) -> float:
    """0..1 in, 0..1 out: fast start, soft arrival instead of a dead stop."""
    return 1.0 - (1.0 - t) ** 3


def sync_duration(max_gap: float, minimum_seconds: float, max_speed: float = MAX_SYNC_SPEED) -> float:
    """Duration for cosine easing that caps peak joint speed at max_speed."""
    if not all(math.isfinite(value) for value in (max_gap, minimum_seconds, max_speed)) or max_gap < 0 or minimum_seconds < 0 or max_speed <= 0:
        raise ValueError("Sync gap and minimum duration must be nonnegative; max speed must be positive")
    return max(minimum_seconds, max_gap * math.pi / (2 * max_speed))


def safe_build_dataset_frame(
    ds_features: dict[str, dict],
    values: dict[str, Any],
    prefix: str,
    default_pose: dict[str, float] | None = None,
) -> dict[str, np.ndarray]:
    """Construct a dataset frame that is guaranteed to match the dataset features schema."""
    import cv2

    frame = {}
    default_pose = default_pose or {}
    for key, ft in ds_features.items():
        if key in ("timestamp", "frame_index", "episode_index", "index", "task_index") or not key.startswith(prefix):
            continue
        if ft["dtype"] == "float32" and len(ft["shape"]) == 1:
            joint_values = []
            for name in ft["names"]:
                val = values.get(name)
                if val is None and name.endswith(".pos"):
                    val = values.get(name[:-4])
                elif val is None:
                    val = values.get(f"{name}.pos")
                if val is None or not isinstance(val, (int, float, np.number)):
                    clean_name = name[:-4] if name.endswith(".pos") else name
                    val = default_pose.get(clean_name, 0.0)
                joint_values.append(float(val))
            frame[key] = np.array(joint_values, dtype=np.float32)
        elif ft["dtype"] in ["image", "video"]:
            cam_name = key.removeprefix(f"{prefix}.images.")
            expected_shape = tuple(ft["shape"])
            img = values.get(cam_name)
            if img is None or not isinstance(img, np.ndarray) or img.ndim != 3:
                img = np.zeros(expected_shape, dtype=np.uint8)
            elif img.shape != expected_shape:
                h, w = expected_shape[0], expected_shape[1]
                img = cv2.resize(img, (w, h))
            if img.dtype != np.uint8:
                img = np.clip(img, 0, 255).astype(np.uint8)
            frame[key] = np.ascontiguousarray(img)
    return frame


def recorder_data_issues(
    task: str,
    fps: int,
    dataset_fps: int,
    camera_meta: dict[str, int],
    dataset_cameras: set[str],
    camera_streaming: dict[str, bool],
) -> list[str]:
    issues = []
    if not task.strip():
        issues.append("task description cannot be empty")
    if any(TRAINING_CAMERAS.get(name) != index for name, index in camera_meta.items()):
        expected = " and ".join(f"{name}={index}" for name, index in sorted(TRAINING_CAMERAS.items()))
        issues.append(f"camera labels must be {expected} (from {CAMERAS_CONFIG_PATH.name})")
    if not dataset_cameras.issubset(camera_meta):
        issues.append("a recorded camera is missing from the live configuration")
    if fps != dataset_fps:
        issues.append(f"recorder is {fps} fps but dataset is {dataset_fps} fps")
    missing = [name for name in dataset_cameras if not camera_streaming.get(name, False)]
    if missing:
        issues.append(f"no fresh frames from {', '.join(sorted(missing))}")
    return issues


def extend_video_camera_schema(dataset_root: Path, configured_features: dict[str, dict]) -> list[str]:
    """Add newly configured video cameras to an existing dataset without rewriting old takes.

    LeRobot stores one video feature per camera. Older episode rows simply have
    no path for a camera added later, so they remain wrist-only while new rows
    get their own synchronized video stream.
    """
    info_path = dataset_root / "meta" / "info.json"
    if not info_path.is_file():
        return []
    info = json.loads(info_path.read_text())
    current = info.get("features", {})
    missing = {
        key: feature for key, feature in configured_features.items()
        if key.startswith(f"{OBS_STR}.images.") and feature.get("dtype") == "video" and key not in current
    }
    if not missing:
        return []
    if int(info.get("fps", 0)) <= 0:
        raise RuntimeError(f"Cannot safely extend camera schema: invalid FPS in {info_path}")
    for key, feature in missing.items():
        if not isinstance(feature.get("shape"), (tuple, list)) or len(feature["shape"]) != 3:
            raise RuntimeError(f"Cannot safely extend camera schema: invalid feature shape for {key}")
        current[key] = feature
    info["features"] = current
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=info_path.parent, delete=False) as temp:
            json.dump(info, temp, indent=4)
            temp.write("\n")
            temp_name = temp.name
        os.replace(temp_name, info_path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
    return sorted(missing)


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("recorder")


class Calibration:
    """Non-interactive rewrite of `SOFollower.calibrate` / `SOLeader.calibrate`.

    LeRobot's version is a straight line through two `input()` calls; the phases
    here are the same two pauses, but they end when a button is pressed in the
    browser instead. Between them the control loop keeps sampling raw encoder
    counts, which is exactly what `record_ranges_of_motion` does while it waits
    on the terminal.
    """

    HOME, RANGE = "home", "range"

    def __init__(self, arm, role: str):
        self.arm = arm
        self.role = role
        self.phase = self.HOME
        self.homing_offsets: dict[str, int] = {}
        self.mins: dict[str, int] = {}
        self.maxes: dict[str, int] = {}
        self.positions: dict[str, int] = {}
        # Restored if the operator backs out, so a cancelled calibration leaves
        # the arm exactly as usable as it was before.
        self.previous = dict(getattr(arm, "calibration", {}) or {})

    @property
    def bus(self):
        return self.arm.bus

    @property
    def sweep_motors(self) -> list[str]:
        return [motor for motor in self.bus.motors if motor != FULL_TURN_MOTOR]

    def begin(self) -> None:
        """Free the joints so they can be moved by hand."""
        from lerobot.motors.feetech import OperatingMode

        self.bus.disable_torque()
        for motor in self.bus.motors:
            self.bus.write("Operating_Mode", motor, OperatingMode.POSITION.value)

    def set_home(self) -> None:
        """Centre every joint's range on where it is standing right now."""
        self.homing_offsets = self.bus.set_half_turn_homings()
        self.positions = self.bus.sync_read("Present_Position", normalize=False, num_retry=3)
        self.mins = dict(self.positions)
        self.maxes = dict(self.positions)
        self.phase = self.RANGE

    def sample(self) -> None:
        """One tick of the range sweep."""
        if self.phase != self.RANGE:
            return
        positions = self.bus.sync_read("Present_Position", self.sweep_motors, normalize=False, num_retry=3)
        self.positions = positions
        for motor, value in positions.items():
            self.mins[motor] = min(self.mins.get(motor, value), value)
            self.maxes[motor] = max(self.maxes.get(motor, value), value)

    def unswept(self) -> list[str]:
        """Joints that have not actually been moved yet.

        LeRobot raises on a zero-width range at the end of the sweep. Reporting
        it up front instead lets the dashboard grey out the finish button and
        name the joints still to move.
        """
        return [m for m in self.sweep_motors if self.maxes.get(m, 0) - self.mins.get(m, 0) < 2]

    def finish(self) -> dict[str, MotorCalibration]:
        mins = dict(self.mins)
        maxes = dict(self.maxes)
        mins[FULL_TURN_MOTOR], maxes[FULL_TURN_MOTOR] = FULL_TURN_RANGE

        calibration = {
            motor: MotorCalibration(
                id=m.id,
                drive_mode=0,
                homing_offset=self.homing_offsets[motor],
                range_min=mins[motor],
                range_max=maxes[motor],
            )
            for motor, m in self.bus.motors.items()
        }
        self.arm.calibration = calibration
        self.bus.write_calibration(calibration)
        self.arm._save_calibration()
        self.arm.configure()
        return calibration

    def cancel(self) -> None:
        if self.previous:
            self.arm.calibration = dict(self.previous)
            self.bus.write_calibration(self.previous)
        self.arm.configure()

    def status(self) -> dict[str, Any]:
        joints = {
            motor: {
                "pos": int(self.positions.get(motor, 0)),
                "min": int(self.mins.get(motor, 0)),
                "max": int(self.maxes.get(motor, 0)),
                "swept": self.maxes.get(motor, 0) - self.mins.get(motor, 0) >= 2,
            }
            for motor in self.sweep_motors
        }
        return {
            "arm": self.role,
            "phase": self.phase,
            "joints": joints,
            "unswept": self.unswept(),
            "can_finish": self.phase == self.RANGE and not self.unswept(),
        }


class Recorder:
    """State machine driven by HTTP, stepped by the control loop."""

    def __init__(
        self,
        robot,
        teleop,
        dataset: LeRobotDataset,
        fps: int,
        task: str,
        commit_seconds: float,
        flush_every: int = 1,
        *,
        camera_meta: dict[str, int] | None = None,
        delta_limit: float = 25.0,
        engage_on_start: bool = False,
        rest_seconds: float = DEFAULT_REST_SECONDS,
        start_seconds: float = DEFAULT_START_SECONDS,
        sync_seconds: float = DEFAULT_SYNC_SECONDS,
        stream_fps: float = 10.0,
        stream_quality: int = 70,
    ):
        self.robot = robot
        self.teleop = teleop
        self.dataset = dataset
        self.fps = fps
        self.task = task
        self.commit_seconds = commit_seconds
        self.flush_every = max(1, flush_every)
        self.camera_meta = camera_meta or {}
        self._since_flush = 0

        self.obs_features = hw_to_dataset_features(robot.observation_features, OBS_STR, use_video=True)
        self.action_features = hw_to_dataset_features(robot.action_features, ACTION, use_video=True)

        self.lock = threading.RLock()
        # Serial buses are not reentrant and not thread-safe. Everything that
        # talks to a bus takes this; the loop holds it for one tick at a time.
        self.hw_lock = threading.RLock()
        self.state = IDLE
        self.frames = 0
        self.started_at = 0.0
        self.started_at_utc: str | None = None
        self.stopped_at_utc: str | None = None
        self.saved_at_utc: str | None = None
        self.commit_at = 0.0
        self.last_message = "ready"
        self._command: str | None = None
        self._payload: dict[str, Any] = {}
        self._command_done: threading.Event | None = None
        self._command_result: dict[str, Any] = {}

        self.estop = False
        self.estop_reason = ""
        self.estop_at = 0.0
        self._estop_reasserted_at = 0.0

        # Observing until someone asks for otherwise: on the first tick the
        # leader is wherever it was left, and sending that straight to a cold
        # follower is a full-speed snap across the arm's range.
        self.engaged = False
        # Engagement completes its physical ramp synchronously, but recording
        # waits for one normal leader->follower control tick after that ramp.
        self._control_ready = False
        # Engaging needs a measured delta, and there is none until the loop has
        # read both arms once -- so this is a request, honoured on the first tick
        # that has numbers to check it against.
        self._engage_pending = engage_on_start

        self.delta_limit = delta_limit
        self.rest_seconds = max(0.0, rest_seconds)
        self.start_seconds = max(0.0, start_seconds)
        self.sync_seconds = max(0.0, sync_seconds)
        self._delta: dict[str, Any] = {"joints": {}, "max": 0.0, "max_joint": None, "over": False}

        self.calib: Calibration | None = None

        self._stream_period = 1.0 / max(1.0, stream_fps)
        self._stream_quality = stream_quality
        self._stream_at = 0.0
        self._jpegs: dict[str, bytes] = {}
        self._stream_cv = threading.Condition()
        self._stream_seq = 0

        self._last_camera_frames: dict[str, np.ndarray] = {}
        self._camera_misses: dict[str, int] = {}
        self._camera_failed_at: dict[str, float] = {}
        self._last_robot_pos: dict[str, float] = {}
        self._last_robot_pos_at: float = 0.0
        self._last_teleop_action: dict[str, float] = {}
        self._last_teleop_action_at: float = 0.0

        self._motors_probed_at = 0.0
        self._teleop_motors: dict[str, bool] = {}
        self._follower_motors: dict[str, bool] = {}
        self._teleop_temps: dict[str, int] = {}
        self._follower_temps: dict[str, int] = {}
        self._motor_faults: dict[str, dict[str, str]] = {}
        self._hardware: dict[str, Any] = {}
        self._quality: dict[str, Any] = {}
        self._camera_fresh_at: dict[str, float] = {}
        self._autonomy = None
        self._autonomy_cancel = None
        self._autonomy_error = ""
        self._autonomy_trace = deque(maxlen=10000)
        self.offline = False
        from keewano_sink import KeewanoSink
        from task_harness import TaskHarness
        self.harness = TaskHarness(self, KeewanoSink())

    # -- called from the HTTP thread ------------------------------------

    def autonomy_status(self):
        with self.lock:
            program = self._autonomy
            return {"active": program is not None, "complete": bool(program and program.complete),
                    "error": self._autonomy_error,
                    "target": dict(program.last_target) if program and program.last_target else None,
                    "tracking_error": round(max(abs(self._last_robot_pos[k]-program.last_target[k]) for k in program.last_target), 2)
                    if program and program.last_target and all(k in self._last_robot_pos for k in program.last_target) else None,
                    "duration_s": round(program.duration, 2) if program else None,
                    "turn_end_s": program.horizon if program else None,
                    "elapsed_s": round(program.elapsed(time.monotonic()), 2) if program else 0}

    def advance_autonomy_turn(self):
        """Only extend the recorded path after fresh, settled live feedback."""
        with self.lock:
            program = self._autonomy
            if program is None or self.estop or self._autonomy_cancel.is_set():
                raise RuntimeError("Autonomous turn stopped")
            if time.time()-self._last_robot_pos_at > .5 or self.fresh_jpeg("wrist") is None:
                raise RuntimeError("Fresh motor and camera readings are required for the next turn")
            return program.advance_turn(self._last_robot_pos, time.monotonic())

    def motion_bounds(self):
        """Use the bus normalization mode, rather than assuming all joints are ±100."""
        from motion_program import DEFAULT_BOUNDS
        bounds = dict(DEFAULT_BOUNDS)
        bus = getattr(self.robot, "bus", None)
        for name, motor in (getattr(bus, "motors", {}) or {}).items():
            if getattr(getattr(motor, "norm_mode", None), "name", "") == "DEGREES":
                calibration = bus.calibration[name]
                resolution = bus.model_resolution_table[motor.model] - 1
                half_range = (calibration.range_max-calibration.range_min)*180/resolution
                bounds[f"{name}.pos"] = (-half_range, half_range)
        return bounds

    def motion_units(self):
        bus = getattr(self.robot, "bus", None)
        return {f"{name}.pos": ("degrees" if getattr(getattr(motor, "norm_mode", None), "name", "") == "DEGREES" else "normalized")
                for name, motor in (getattr(bus, "motors", {}) or {}).items()}

    def motor_telemetry(self):
        """Read cached observations only; the control loop retains bus ownership."""
        with self.lock:
            now = time.time()
            positions = dict(self._last_robot_pos)
            age = now-self._last_robot_pos_at if self._last_robot_pos_at else None
            radians = {}
            calibration = getattr(getattr(self.robot, "bus", None), "calibration", {}) or {}
            for name in ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll"):
                motor = calibration.get(name)
                value = positions.get(f"{name}.pos")
                if motor is not None and isinstance(value, (float, int)) and np.isfinite(value):
                    # Convert according to the actual bus mode; degree readings are already angles.
                    span = motor.range_max - motor.range_min
                    if span > 0:
                        radians[name] = (float(value)*np.pi/180 if self.motion_units().get(f"{name}.pos") == "degrees"
                                         else float(value)/100*span*np.pi/4095)
            return {"source": "recorder_cache", "units": "native LeRobot motor units", "joint_units": self.motion_units(), "bounds": self.motion_bounds(),
                    "positions": positions, "age_s": age, "radians": radians, "gripper": positions.get("gripper.pos"),
                    "hardware_available": not self.offline and age is not None and 0 <= age < .5 and len(positions) == 6,
                    "temperatures": dict(self._follower_temps),
                    "temperature_age_s": now-self._motors_probed_at if self._motors_probed_at else None,
                    "camera_age_s": {k: time.perf_counter()-v for k, v in self._camera_fresh_at.items()},
                    "hardware": copy.deepcopy(self._hardware), "motion": self.autonomy_status(),
                    "estop": self.estop, "estop_reason": self.estop_reason,
                    "offline": self.offline, "message": self.last_message}

    def reset_motor_trace(self):
        with self.lock:
            self._autonomy_trace.clear()

    def motor_trace(self):
        with self.lock:
            return copy.deepcopy(list(self._autonomy_trace))

    def fresh_jpeg(self, name):
        if time.perf_counter()-self._camera_fresh_at.get(name, 0) > .5 or self._camera_misses.get(name, 1):
            return None
        return self.jpeg(name)

    def autonomy_preflight(self):
        with self.lock:
            if self.estop or self.engaged or self.calib is not None or self.state != IDLE or self._command is not None or self._autonomy is not None:
                raise RuntimeError("Follower must be idle, disengaged, and outside calibration/e-stop")
            if time.time()-self._last_robot_pos_at > .5:
                raise RuntimeError("No fresh six-motor follower reading; reconnect and power the arm")
            if self.fresh_jpeg("wrist") is None:
                raise RuntimeError("A fresh wrist-camera frame is required")
            if self._hardware.get("follower", {}).get("temperature_status") == "fail":
                raise RuntimeError("Follower temperature is too high")

    def start_autonomy(self, frames, cancel, speed=None, *, short_turns=False):
        from motion_program import MotionProgram, JOINTS, MAX_SPEED
        program = MotionProgram(frames, MAX_SPEED if speed is None else speed, bounds=self.motion_bounds())
        if short_turns:
            program.horizon = min(program.duration, 2.0)
        with self.hw_lock:
            self.autonomy_preflight()
            observation = self._read_robot_observation()
            measured = {k: observation[k] for k in JOINTS}
            program.start(measured, time.monotonic())
            with self.lock:
                if self.estop or cancel.is_set() or self.engaged or self.state != IDLE or self._command is not None:
                    raise RuntimeError("Controller ownership changed during preparation")
                # Seed goal registers at the measured pose BEFORE energizing.
                self._autonomy_error = ""
                self._autonomy_cancel = cancel
                self._autonomy = program
                try:
                    self.robot.bus.disable_torque()
                    self.robot.send_action(measured)
                    self.robot.bus.enable_torque()
                    program.started = program.last_tick = time.monotonic()
                except Exception as error:
                    self.emergency_stop(f"Could not initialize autonomous control: {error}")
                    raise

    def _autonomy_tick(self):
        from motion_program import JOINTS
        try:
            with self.hw_lock:
                observation = self._read_robot_observation()
                with self.lock:
                    program = self._autonomy
                    if program is None or self.estop:
                        return observation
                    if self._autonomy_cancel.is_set():
                        raise RuntimeError("Autonomous skill cancelled")
                    if self.fresh_jpeg("wrist") is None:
                        raise RuntimeError("Wrist camera lost during motion")
                    if self._hardware.get("follower", {}).get("temperature_status") == "fail":
                        raise RuntimeError("Follower overheated during motion")
                    measured = {k: observation[k] for k in JOINTS}
                    tracking = max(abs(measured[k]-program.last_target[k]) for k in JOINTS)
                    target = program.tick(measured, time.monotonic())
                    self.robot.send_action(target)
                    # Bounded memory append only: no embedding, model or file I/O per tick.
                    self._autonomy_trace.append({"at": time.time(), "elapsed_s": program.elapsed(time.monotonic()),
                                                 "measured": measured, "commanded": dict(target),
                                                 "tracking_delta": tracking, "complete": program.complete})
            return observation
        except Exception as error:
            self.emergency_stop(str(error))
            return None

    def finish_autonomy(self):
        with self.hw_lock:
            with self.lock:
                if self.estop or self._autonomy is None or not self._autonomy.complete:
                    raise RuntimeError("Autonomous execution stopped before verification finished")
                self._autonomy = None
                self._autonomy_cancel = None
            self._kill_torque()
            self.last_message = "Autonomous skill finished; torque released"

    def command(self, name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Queue a command and block until the loop has applied it.

        Blocking matters: the UI fires stop-then-discard within milliseconds, and
        at 30 Hz that is well inside one tick. If the calls returned early, the
        second would be validated against the pre-stop state and rejected.
        """
        payload = payload or {}
        if self.offline and name not in ("estop", "task"):
            return {"ok": False, "error": "Bridge is in offline planning mode; reconnect hardware first"}

        # The kill switch does not queue. Everything else does.
        if name == "estop":
            return self.emergency_stop(payload.get("reason") or "operator")
        if name == "rearm":
            return self.rearm(force=bool(payload.get("force")))
        # Engagement is a torque write like re-arming is, and it has the same
        # reason not to queue: the operator is standing over the arms with a
        # hand on them, and a button that waits on a tick reads as a dead button.
        if name in ("engage", "disengage"):
            return self._set_engaged(name == "engage")
        if name == "sync":
            if payload.get("confirmed") is not True:
                return {"ok": False, "error": "operator confirmation is required before the follower can move to the leader"}
            with self.lock:
                if self.state != IDLE:
                    return {"ok": False, "error": f"sync arms is only available while idle, not {self.state}"}
                if self.estop or self.calib is not None or self._autonomy is not None or self._command is not None:
                    return {"ok": False, "error": "sync arms is unavailable during e-stop, calibration, or another motion"}
                for role in ("teleop", "follower"):
                    hardware = self._hardware.get(role, {})
                    if hardware.get("motors_ok") != 6:
                        return {"ok": False, "error": f"{role} must report all six healthy motors before syncing ({hardware.get('motors_ok', 0)}/6 responding)"}
                joint_readings = self._delta.get("joints") or {}
                required_joints = {"shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"}
                if (time.time() - min(self._last_robot_pos_at, self._last_teleop_action_at) > 0.5
                        or not required_joints.issubset(joint_readings)):
                    return {"ok": False, "error": "fresh six-joint readings from both arms are required to sync"}
            # This is the explicit, operator-confirmed path for closing an
            # out-of-range pose gap. The follower is ramped to the live leader
            # pose before tracking begins; normal engage remains delta-gated.
            return self._set_engaged(True, force=True)

        with self.lock:
            if (name == "stop" and self.state in (IDLE, PENDING, SAVING, ESTOPPED)
                    and not self.engaged and self._command is None):
                return {"ok": True, "already": True}

        done = threading.Event()
        with self.lock:
            if self._autonomy is not None:
                return {"ok": False, "error": "Autonomous skill owns the follower; stop it before teaching"}
            if self._command is not None:
                return {"ok": False, "error": "a command is already in flight"}
            if self.estop and name != "task":
                return {"ok": False, "error": "arms are e-stopped — re-arm first"}
            if name == "task":
                self.task = payload.get("task") or self.task
            elif name == "record":
                if getattr(self, "_writer_error", None):
                    return {"ok": False, "error": f"Previous save failed; restart the bridge before recording again: {self._writer_error}"}
                if self.state not in (IDLE, PENDING):
                    return {"ok": False, "error": f"cannot record while {self.state}"}
                if not self.engaged or not self._control_ready:
                    return {"ok": False, "error": "follower control is not settled yet"}
                self._command = "record"
            elif name == "stop" and self.state == RECORDING:
                self._command = "stop"
            elif name == "discard" and self.state in (RECORDING, PENDING):
                self._command = "discard"
            elif name == "save" and self.state == PENDING:
                self._command = "save"
            elif name == "calibrate_start" and self.state == IDLE:
                self._command = "calibrate_start"
            elif name in ("calibrate_home", "calibrate_finish", "calibrate_cancel") and self.state == CALIBRATING:
                self._command = name
            else:
                return {"ok": False, "error": f"cannot {name} while {self.state}"}
            queued = self._command is not None
            if queued:
                self._payload = payload
                self._command_done = done
                self._command_result = {}

        if not queued:
            return {"ok": True}

        # `save`, a `record` that commits a pending take, and finishing a
        # calibration all run seconds of work inside the loop, so the wait has to
        # tolerate seconds, not ticks.
        if not done.wait(timeout=30.0):
            return {"ok": False, "error": f"{name} timed out"}
        with self.lock:
            return self._command_result or {"ok": True}

    def emergency_stop(self, reason: str = "operator") -> dict[str, Any]:
        """Cut torque on both arms now, from whatever thread asked.

        Deliberately not routed through the command queue: a queued kill is only
        as fast as the loop, and the loop can be several seconds deep in a video
        encode. An in-flight take is dropped -- a take that ended in a kill is a
        bad take by definition, and its frames were never written.
        """
        with self.lock:
            already = self.estop
            prior = self.state
            self.estop = True
            self.estop_reason = reason
            self.estop_at = time.time()
            self.state = ESTOPPED
            # Torque is about to go; the gate follows it, so re-arming lands
            # back in observing rather than resuming a drive nobody asked for.
            self.engaged = False
            if hasattr(self, "harness"):
                self.harness.cancel.set()
            if self._autonomy is not None:
                self._autonomy_error = reason
                self._autonomy = None
            if self._autonomy_cancel is not None:
                self._autonomy_cancel.set()

        killed = self._kill_torque()
        self._estop_reasserted_at = time.time()

        if prior in (RECORDING, PENDING):
            try:
                self.dataset.clear_episode_buffer()
            except Exception as err:  # noqa: BLE001
                log.warning("could not clear the episode buffer: %s", err)
        if prior == CALIBRATING and self.calib is not None:
            self.calib = None

        with self.lock:
            self.frames = 0
            self.last_message = f"E-STOP · {reason} · torque off: {', '.join(killed) or 'nothing answered'}"
        if not already:
            log.warning("E-STOP (%s); torque cut on %s", reason, killed or "nothing")
        return {"ok": True, "killed": killed, "already": already}

    def rearm(self, force: bool = False) -> dict[str, Any]:
        """Put torque back on the follower after an e-stop.

        Refused while the arms disagree: re-energising a follower that is far
        from the leader makes it snap to the leader's pose at full speed, which
        is how you break a gripper (or a finger). Move them together first, or
        pass force.
        """
        with self.lock:
            if not self.estop:
                return {"ok": False, "error": "not stopped"}
            follower_motors = self._hardware.get("follower", {}).get("motors_ok", 0)
            teleop_motors = self._hardware.get("teleop", {}).get("motors_ok", 0)
            if follower_motors != 6 or teleop_motors != 6:
                return {"ok": False, "error": f"cannot re-arm: follower {follower_motors}/6 motors and leader {teleop_motors}/6 motors responding; inspect the reported motor fault first"}

        try:
            with self.hw_lock:
                configure_at_measured_pose(self.robot)
                self.robot.bus.disable_torque()
        except Exception as err:  # noqa: BLE001
            return {"ok": False, "error": f"could not re-arm: {err}"}

        with self.lock:
            self.estop = False
            self.estop_reason = ""
            self.state = IDLE
            self.last_message = "re-armed"
        log.info("re-armed")
        return {"ok": True}

    def _set_engaged(self, engaged: bool, force: bool = False) -> dict[str, Any]:
        """Hand the follower to the leader, or take it back."""
        verb = "engage" if engaged else "disengage"
        with self.lock:
            if self._autonomy is not None:
                return {"ok": False, "error": "Autonomous skill owns the follower"}
            if self.estop:
                return {"ok": False, "error": f"cannot {verb} while e-stopped — re-arm first"}
            if self.state == CALIBRATING:
                return {"ok": False, "error": f"cannot {verb} while calibrating"}
            if engaged != self.engaged and not engaged and self.state == RECORDING:
                return {"ok": False, "error": "stop the take before disengaging"}
            if engaged and (time.time() - min(self._last_robot_pos_at, self._last_teleop_action_at) > 0.5 or not self._delta.get("joints")):
                return {"ok": False, "error": "fresh readings from both arms are required before engaging teleop"}
            if engaged and self._delta.get("max", 999) > self.delta_limit and not force:
                return {"ok": False, "error": "Align the leader and follower before engaging teleop"}
            resync = engaged and force and self.engaged
            if engaged == self.engaged and not resync:
                return {"ok": True, "already": True}

        try:
            with self.hw_lock:
                if engaged:
                    configure_at_measured_pose(self.robot)
                    if force:
                        self._sync_to_leader()
                    else:
                        self._engage_ramp()
                else:
                    self._rest_and_release()
        except Exception as err:  # noqa: BLE001
            if force:
                # A partial sync must never leave the follower energized in an
                # intermediate pose; a failed ramp cuts torque immediately.
                self.emergency_stop(f"sync failed: {err}")
            return {"ok": False, "error": f"could not {verb}: {err}"}

        with self.lock:
            self.engaged = engaged
            self._control_ready = False
            self.last_message = (
                "teleop engaged — the follower is tracking the leader"
                if engaged
                else "teleop observing — follower torque off"
            )
        log.info("teleop %sd", verb)
        return {"ok": True}

    def _kill_torque(self) -> list[str]:
        # Torque off outranks a clean serial transaction: if the loop will not
        # hand over the bus quickly, write anyway and accept a garbled packet
        # (the retries below cover it).
        acquired = self.hw_lock.acquire(timeout=0.5)
        if not acquired:
            log.warning("e-stop could not take the hardware lock in 500ms; cutting torque regardless")
        try:
            killed = []
            for role, arm in (("follower", self.robot), ("teleop", self.teleop)):
                bus = getattr(arm, "bus", None)
                if bus is None:
                    continue
                try:
                    release_all_torque(bus)
                    killed.append(role)
                except Exception as err:  # noqa: BLE001
                    log.error("could not cut torque on %s: %s", role, err)
            return killed
        finally:
            if acquired:
                self.hw_lock.release()

    def _ramp_to(self, pose: dict[str, float], seconds: float) -> None:
        """Ease the follower from wherever it reads now onto `pose`.

        Uses a smooth S-curve easing so the arm starts from 0 velocity, accelerates
        gently, glides smoothly, and decelerates softly into target pose.
        Caller holds `hw_lock`.
        """
        # Joints only. `robot.get_observation()` also calls `cam.read_latest()`,
        # and a dead wrist thread (OpenCVCamera(1)) would abort the whole engage.
        observation = self._read_robot_observation()
        current = {
            key: float(value)
            for key, value in observation.items()
            if key.endswith(".pos") and isinstance(value, (int, float, np.number))
        }
        if not current:
            return
        target = {key: pose.get(key.removesuffix(".pos"), value) for key, value in current.items()}
        steps = max(1, round(self.fps * seconds))
        for step in range(1, steps + 1):
            fraction = _ease_in_out(step / steps)
            self.robot.send_action({
                key: value + fraction * (target[key] - value)
                for key, value in current.items()
            })
            if step < steps:
                time.sleep(1.0 / self.fps)
        time.sleep(0.2)

    def _rest_and_release(self) -> None:
        """Ease to the calibrated relaxed pose, then release torque.

        This is for orderly stop/disengage/discard only. E-stop and daemon
        teardown deliberately call `_kill_torque()` directly so they never wait
        for a commanded movement.
        """
        try:
            with self.hw_lock:
                self.robot.configure()
                self._ramp_to(REST_POSE, self.rest_seconds)
        except Exception as err:  # noqa: BLE001
            log.warning("could not move the follower to the relaxed pose: %s", err)
        finally:
            with self.lock:
                self.engaged = False
                self._control_ready = False
            self._kill_torque()

    def _leader_pose(self) -> dict[str, float]:
        leader_action = self.teleop.get_action()
        return {
            key.removesuffix(".pos"): float(value)
            for key, value in leader_action.items()
            if key.endswith(".pos") and isinstance(value, (int, float, np.number))
        }

    def _sync_to_leader(self) -> None:
        """Ease directly to the live leader pose, with a distance-based speed cap."""
        leader_pose = self._leader_pose()
        observation = self._read_robot_observation()
        follower_pose = {key.removesuffix(".pos"): float(value)
                         for key, value in observation.items()
                         if key.endswith(".pos") and isinstance(value, (int, float, np.number))}
        joints = set(leader_pose) & set(follower_pose)
        if not joints or len(joints) != 6:
            raise RuntimeError("fresh readings for all six leader and follower joints are required")
        max_gap = max(abs(leader_pose[joint] - follower_pose[joint]) for joint in joints)
        # Cosine easing peaks at pi/2 times its average speed. Extend the ramp
        # so even the largest joint gap stays under MAX_SYNC_SPEED.
        seconds = sync_duration(max_gap, self.sync_seconds)
        log.info("syncing follower to leader over %.1fs (largest joint gap %.1f)", seconds, max_gap)
        self._ramp_to(leader_pose, seconds)

    def _engage_ramp(self) -> None:
        """Ease through the ready pose, then synchronize to the leader."""
        self._ramp_to(START_POSE, self.start_seconds)
        leader_pose = self._leader_pose()
        if leader_pose:
            self._ramp_to(leader_pose, self.sync_seconds)

    def status(self) -> dict[str, Any]:
        with self.lock:
            now = time.time()
            dataset_cameras = {
                key.removeprefix(f"{OBS_STR}.images.")
                for key, feature in self.dataset.features.items()
                if key.startswith(f"{OBS_STR}.images.") and feature.get("dtype") in ("image", "video")
            }
            camera_streaming = {
                name: name in self._last_camera_frames and self._camera_misses.get(name, 0) == 0
                for name in self.camera_meta
            }
            # Every schema camera is a required recording input; refuse readiness
            # if either synchronized view is missing or stale.
            dataset_camera_meta = {name: index for name, index in self.camera_meta.items() if name in dataset_cameras}
            dataset_camera_streaming = {name: camera_streaming.get(name, False) for name in dataset_camera_meta}
            issues = recorder_data_issues(
                self.task, self.fps, self.dataset.fps, dataset_camera_meta, dataset_cameras, dataset_camera_streaming
            )
            return {
                "state": self.state,
                "offline_bridge": self.offline,
                "repo_id": self.dataset.repo_id,
                "fps": self.fps,
                "task": self.task,
                "frames": self.frames,
                "elapsed_s": (now - self.started_at) if self.state == RECORDING else 0.0,
                "started_at": self.started_at_utc,
                "stopped_at": self.stopped_at_utc,
                "saved_at": self.saved_at_utc,
                "commit_in_s": max(0.0, self.commit_at - now) if self.state == PENDING else 0.0,
                "commit_seconds": self.commit_seconds,
                "saved_episodes": self.dataset.meta.total_episodes,
                "message": self.last_message,
                "hardware": dict(self._hardware),
                "cameras": list(self.camera_meta),
                "data_quality": {
                    "ready": not issues,
                    "issues": issues,
                    "camera_config": dict(self.camera_meta),
                    "dataset_cameras": sorted(dataset_cameras),
                    "dataset_fps": self.dataset.fps,
                    "camera_streaming": camera_streaming,
                },
                "estop": {
                    "engaged": self.estop,
                    "reason": self.estop_reason,
                    "since_s": (now - self.estop_at) if self.estop else 0.0,
                },
                "delta": dict(self._delta),
                "teleop": {
                    "engaged": self.engaged,
                    # Same number the delta panel already shows, restated as the
                    # yes/no the Engage button needs. A delta with no joints in
                    # it is not "aligned", it is "not measured yet".
                    "ready": not self.estop and now - min(self._last_robot_pos_at, self._last_teleop_action_at) < 0.5 and bool(self._delta.get("joints")) and self._delta.get("max", 0.0) <= self.delta_limit,
                    "record_ready": self.engaged and self._control_ready,
                    "worst": self._delta.get("max", 0.0),
                    "worst_joint": self._delta.get("max_joint"),
                },
                "calibration": self._calibration_status,
                "autonomy": self.autonomy_status(),
                "motors": {
                    "positions": dict(self._last_robot_pos),
                    "age_s": round(now - self._last_robot_pos_at, 3) if self._last_robot_pos_at else None,
                },
            }

    @property
    def _calibration_status(self) -> dict[str, Any] | None:
        calib = self.calib
        return calib.status() if calib is not None else None

    def jpeg(self, name: str) -> bytes | None:
        with self._stream_cv:
            return self._jpegs.get(name)

    def wait_for_jpeg(self, name: str, seq: int, timeout: float = 5.0) -> tuple[bytes | None, int]:
        """Block until a frame newer than `seq` exists, for the MJPEG stream."""
        with self._stream_cv:
            if self._stream_seq <= seq:
                self._stream_cv.wait(timeout)
            return self._jpegs.get(name), self._stream_seq

    # -- called from the control loop thread ----------------------------

    def _get_camera_frame(self, name: str, cam: Any) -> np.ndarray:
        frame: np.ndarray | None = None
        now = time.time()

        try:
            frame = cam.read_latest(max_age_ms=500)
            self._camera_fresh_at[name] = getattr(cam, "latest_timestamp", 0) or 0
        except Exception:
            pass  # Cached camera pixels are display-only, never fresh control evidence.

        is_alive = bool(getattr(cam, "is_connected", False) and getattr(cam, "thread", None) and cam.thread.is_alive())
        if not is_alive or frame is None:
            self._camera_misses[name] = self._camera_misses.get(name, 0) + 1
            if self.state == RECORDING:
                self._quality.setdefault("camera_misses", {}).setdefault(name, 0)
                self._quality["camera_misses"][name] += 1
            if self._camera_misses[name] >= 5 and (now - self._camera_failed_at.get(name, 0.0) >= 3.0):
                self._camera_failed_at[name] = now
                log.warning("camera %s connection/thread lost, reconnecting in background...", name)
                threading.Thread(target=self._reconnect_camera, args=(name, cam), daemon=True).start()

        if is_alive and frame is not None:
            self._last_camera_frames[name] = frame
            self._camera_misses[name] = 0
            return frame

        if name in self._last_camera_frames:
            return self._last_camera_frames[name]

        width = getattr(cam, "width", None) or getattr(getattr(cam, "config", None), "width", 640) or 640
        height = getattr(cam, "height", None) or getattr(getattr(cam, "config", None), "height", 480) or 480
        dummy = np.zeros((height, width, 3), dtype=np.uint8)
        return dummy

    def _reconnect_camera(self, name: str, cam: Any) -> None:
        try:
            cam.disconnect()
        except Exception:
            pass
        try:
            cam.connect(warmup=False)
            log.info("camera %s reconnected successfully", name)
        except Exception as err:
            log.warning("camera %s reconnect attempt failed: %s", name, err)

    def _read_robot_observation(self) -> dict[str, Any]:
        obs_dict: dict[str, Any] = {}
        now = time.time()
        try:
            bus = getattr(self.robot, "bus", None)
            if bus is not None:
                raw_pos = bus.sync_read("Present_Position", num_retry=3)
                expected = {"shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"}
                if not isinstance(raw_pos, dict) or set(raw_pos) != expected or not all(isinstance(v, (int, float, np.number)) and np.isfinite(v) for v in raw_pos.values()):
                    raise RuntimeError("Require fresh readings from all six follower motors")
                if raw_pos and isinstance(raw_pos, dict):
                    for motor, val in raw_pos.items():
                        if isinstance(val, (int, float, np.number)):
                            obs_dict[f"{motor}.pos"] = float(val)
                    self._last_robot_pos = {k: v for k, v in obs_dict.items() if k.endswith(".pos")}
                    self._last_robot_pos_at = now
        except Exception as err:
            log.debug("read robot position exception: %s", err)
            raise RuntimeError("Follower position read failed") from err
            if self.state == RECORDING:
                self._quality["bus_errors"] = self._quality.get("bus_errors", 0) + 1

        for m in ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"):
            key = f"{m}.pos"
            if key not in obs_dict:
                obs_dict[key] = float(self._last_robot_pos.get(key, REST_POSE.get(m, 0.0)))

        expected_cams = {
            k.removeprefix(f"{OBS_STR}.images.")
            for k, ft in getattr(getattr(self, "dataset", None), "features", {}).items()
            if k.startswith(f"{OBS_STR}.images.") and ft.get("dtype") in ("image", "video")
        }
        all_cam_keys = set(self.camera_meta.keys()) | expected_cams
        for cam_key in all_cam_keys:
            cam = (getattr(self.robot, "cameras", {}) or {}).get(cam_key)
            obs_dict[cam_key] = self._get_camera_frame(cam_key, cam)

        return obs_dict

    def _read_teleop_action(self) -> dict[str, Any]:
        # Never substitute cached/default angles for a missing leader joint:
        # those values would become real follower commands and training labels.
        try:
            bus = getattr(self.teleop, "bus", None)
            if bus is None:
                raise RuntimeError("Leader bus is unavailable")
            raw_pos = bus.sync_read("Present_Position", num_retry=3)
            expected = {"shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"}
            if not isinstance(raw_pos, dict) or set(raw_pos) != expected or not all(
                isinstance(v, (int, float, np.number)) and np.isfinite(v) for v in raw_pos.values()
            ):
                raise RuntimeError("Require fresh readings from all six leader motors")
            action = {f"{motor}.pos": float(value) for motor, value in raw_pos.items()}
            self._last_teleop_action = dict(action)
            self._last_teleop_action_at = time.time()
            return action
        except Exception as err:
            if self.state == RECORDING:
                self._quality["bus_errors"] = self._quality.get("bus_errors", 0) + 1
            raise RuntimeError("Leader position read failed") from err

    def step(self) -> None:
        if self.offline:
            self._dispatch()
            return
        observation: dict[str, Any] | None = None
        sent: dict[str, Any] | None = None

        if self.estop:
            observation = self._estop_hold()
        elif self._autonomy is not None:
            observation = self._autonomy_tick()
        elif self.calib is not None:
            observation = self._calibration_sample()
        else:
            observation, sent = self._teleop()
            if self._engage_pending:
                self._engage_on_start()
        self._dispatch()

        with self.lock:
            state = self.state

        if state == RECORDING and (observation is None or sent is None):
            return
        if state == RECORDING:
            if observation is None:
                observation = self._read_robot_observation()
            if sent is None:
                sent = self._read_teleop_action()
            try:
                obs_frame = safe_build_dataset_frame(self.dataset.features, observation, prefix=OBS_STR, default_pose=REST_POSE)
                act_frame = safe_build_dataset_frame(self.dataset.features, sent, prefix=ACTION, default_pose=START_POSE)
                task_str = str(self.task or "default")
                frame = {**obs_frame, **act_frame, "task": task_str}
                self.dataset.add_frame(frame)
                with self.lock:
                    self.frames += 1
            except Exception as err:  # noqa: BLE001
                log.error("could not record frame to dataset: %s", err)
                with self.lock:
                    self._quality["frame_errors"] = self._quality.get("frame_errors", 0) + 1
                    self.last_message = f"frame error: {err}"

        with self.lock:
            expired = self.state == PENDING and time.time() >= self.commit_at
        if expired:
            self._save()

        self._update_hardware(observation)
        self._publish_frames(observation)

    def _teleop(self) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Read both arms every tick; drive the follower only once engaged.

        Observing costs exactly the same two bus transactions, which is the
        point: the delta the operator is lining the arms up against is measured
        by the same code that will drive them a moment later.

        A dropped USB packet here must not take the daemon down with it -- the
        control loop's own `while True` only catches Ctrl-C, so anything else
        that escapes `step()` kills the process and both arms disconnect as a
        side effect, which reads exactly like a stop that nobody pressed. Log
        it and pick the read back up next tick instead.
        """
        try:
            with self.hw_lock:
                observation = self._read_robot_observation()
                action = self._read_teleop_action()
                self._update_delta(action, observation)
                sent = None
                if self.engaged and not self.estop and action:
                    sent = self.robot.send_action(action)
        except Exception as err:  # noqa: BLE001
            log.warning("teleop read/write failed, skipping this tick: %s", err)
            if self.state == RECORDING:
                self._quality["skipped_ticks"] = self._quality.get("skipped_ticks", 0) + 1
            self._control_ready = False
            if self.engaged and time.time() - min(self._last_robot_pos_at, self._last_teleop_action_at) > 0.5:
                self.emergency_stop("Joint readings are stale")
            return None, None
        with self.lock:
            self._control_ready = bool(sent) and not self.estop and bool(self._delta.get("joints"))
        return observation, (sent if sent is not None else action)

    def _engage_on_start(self) -> None:
        """Apply --engage-on-start, once there is a delta to gate it on.

        Still gated: starting the daemon must not make a follower that is
        nowhere near its leader snap across its range, and the whole reason
        engagement is a separate step is that it is the moment that can happen.
        A refusal leaves teleop observing and says so, rather than failing
        silently and looking like a dead flag.
        """
        with self.lock:
            measured = bool(self._delta.get("joints"))
        if not measured:
            return
        self._engage_pending = False

        result = self._set_engaged(True)
        if not result.get("ok"):
            with self.lock:
                self.last_message = f"stayed observing — {result.get('error')}"
            log.warning("--engage-on-start refused: %s", result.get("error"))

    def _estop_hold(self) -> dict[str, Any] | None:
        """Keep reading -- and keep re-cutting torque -- while stopped.

        Reads carry on because the delta readout is what tells the operator when
        it is safe to re-arm, and because the camera streams should not go black
        at the moment something has just gone wrong.
        """
        try:
            with self.hw_lock:
                observation = self._read_robot_observation()
                action = self._read_teleop_action()
            self._update_delta(action, observation)
        except Exception as err:  # noqa: BLE001
            log.debug("read failed while e-stopped: %s", err)
            observation = None

        now = time.time()
        if now - self._estop_reasserted_at >= 2.0:
            self._estop_reasserted_at = now
            self._kill_torque()
        return observation

    def _calibration_sample(self) -> dict[str, Any] | None:
        calib = self.calib
        if calib is None:
            return None
        try:
            with self.hw_lock:
                calib.sample()
        except Exception as err:  # noqa: BLE001
            log.debug("calibration read failed: %s", err)
        # Normalised joint readings are meaningless mid-calibration, but the
        # cameras still are not, so the operator can see the arm they are moving.
        return self._camera_only_observation()

    def _camera_only_observation(self) -> dict[str, Any]:
        images: dict[str, Any] = {}
        cameras = getattr(self.robot, "cameras", {}) or {}
        for name, cam in cameras.items():
            images[name] = self._get_camera_frame(name, cam)
        return images

    def _dispatch(self) -> None:
        with self.lock:
            command, self._command = self._command, None
            payload, self._payload = self._payload, {}
            done, self._command_done = self._command_done, None

        if command is None:
            if done is not None:
                done.set()
            return

        result: dict[str, Any] = {"ok": True}
        if command == "record":
            # Readiness can disappear between HTTP validation and this tick.
            with self.lock:
                ready = self.engaged and self._control_ready and not self.estop
                pending = self.state == PENDING
            if not ready:
                result = {"ok": False, "error": "follower control is not settled yet"}
            else:
                if pending:
                    result = self._save()
                if result.get("ok"):
                    self._begin()
        elif command == "stop":
            with self.lock:
                self.stopped_at_utc = utc_timestamp()
            if self.estop:
                self._kill_torque()
            else:
                self._rest_and_release()
            if self.commit_seconds > 0:
                self._pend()
            else:
                result = self._save()
        elif command == "discard":
            self._discard()
        elif command == "save":
            result = self._save()
        elif command == "calibrate_start":
            result = self._calibrate_start(payload.get("arm") or "follower")
        elif command == "calibrate_home":
            result = self._calibrate_home()
        elif command == "calibrate_finish":
            result = self._calibrate_finish()
        elif command == "calibrate_cancel":
            result = self._calibrate_cancel()

        with self.lock:
            self._command_result = result
        if done is not None:
            done.set()

    def _begin(self) -> None:
        with self.lock:
            self.state = RECORDING
            self.frames = 0
            self._quality = {"bus_errors": 0, "frame_errors": 0, "skipped_ticks": 0, "camera_misses": {}, "max_tracking_delta": 0.0}
            self.started_at = time.time()
            self.started_at_utc = utc_timestamp()
            self.stopped_at_utc = None
            self.saved_at_utc = None
            self.last_message = "recording"
        log.info("recording started")

    def _pend(self) -> None:
        with self.lock:
            if self.stopped_at_utc is None:
                self.stopped_at_utc = utc_timestamp()
            self.state = PENDING
            self.commit_at = time.time() + self.commit_seconds
            self.last_message = f"{self.frames} frames — saving in {self.commit_seconds:.0f}s"
        log.info("stopped with %d frames; commit window open", self.frames)

    def _discard(self) -> None:
        frames = self.frames
        try:
            self.dataset.clear_episode_buffer()
        except Exception as err:
            log.warning("could not clear episode buffer: %s", err)
        self._rest_and_release()
        with self.lock:
            self.state = IDLE
            self.frames = 0
            self.last_message = f"discarded {frames} frames"
        log.info("discarded %d frames", frames)

    def _save(self) -> dict[str, Any]:
        with self.lock:
            if self.state not in (RECORDING, PENDING, SAVING):
                return {"ok": False, "error": f"cannot save while {self.state}"}
            self.state = SAVING
            frames = self.frames
            if frames == 0 and not self.dataset.has_pending_frames():
                self.state = IDLE
                self.last_message = "nothing to save — no frames were recorded"
                return {"ok": False, "error": self.last_message}
            self.last_message = "saving…"

        try:
            save_started = time.monotonic()
            save_recording_episode(self.dataset)
            encoded_at = time.monotonic()
            new_ds = reopen(self.dataset)
            with self.lock:
                self.dataset = new_ds
            log.info("EPISODE_SAVE_TIMING %s", json.dumps({"save_s": round(encoded_at-save_started, 3),
                     "reopen_s": round(time.monotonic()-encoded_at, 3), "frames": frames}))
        except Exception as err:  # noqa: BLE001
            with self.lock:
                self.state = IDLE
                self._writer_error = str(err)
                self.last_message = f"save failed: {err}"
            log.exception("save failed")
            return {"ok": False, "error": self.last_message}
        self._since_flush = 0
        with self.lock:
            self.saved_at_utc = utc_timestamp()
            episode = self.dataset.meta.total_episodes - 1
            quality = {
                "episode": episode,
                "task": self.task,
                "started_at": self.started_at_utc,
                "stopped_at": self.stopped_at_utc,
                "saved_at": self.saved_at_utc,
                "fps": self.fps,
                "frames": frames,
                "duration_s": round(frames / self.fps, 3),
                "camera_config": dict(self.camera_meta),
                **self._quality,
            }
            quality["quality_ok"] = (
                quality["bus_errors"] == 0
                and quality["frame_errors"] == 0
                and quality["skipped_ticks"] == 0
                and not any(quality["camera_misses"].values())
                and quality["max_tracking_delta"] <= self.delta_limit
            )
            self.state = IDLE
            self.frames = 0
            self.last_message = f"saved episode {episode} ({frames} frames)"
        log.info("TRAINING_EPISODE %s", json.dumps(quality, sort_keys=True))
        return {"ok": True, "saved": True, "episode": episode, "frames": frames}

    # -- calibration ----------------------------------------------------

    def _arm(self, role: str):
        return self.teleop if role == "teleop" else self.robot

    def _calibrate_start(self, role: str) -> dict[str, Any]:
        if role not in ("teleop", "follower"):
            return {"ok": False, "error": f"unknown arm {role!r}"}
        arm = self._arm(role)
        if getattr(arm, "bus", None) is None:
            return {"ok": False, "error": f"{role} has no motor bus"}
        calib = Calibration(arm, role)
        try:
            with self.hw_lock:
                calib.begin()
        except Exception as err:  # noqa: BLE001
            return {"ok": False, "error": f"could not free the joints: {err}"}
        self.calib = calib
        with self.lock:
            self.state = CALIBRATING
            self.last_message = f"calibrating {role} — move it to the middle of its range"
        log.info("calibration started on %s", role)
        return {"ok": True}

    def _calibrate_home(self) -> dict[str, Any]:
        calib = self.calib
        if calib is None:
            return {"ok": False, "error": "not calibrating"}
        try:
            with self.hw_lock:
                calib.set_home()
        except Exception as err:  # noqa: BLE001
            return {"ok": False, "error": f"could not set home: {err}"}
        with self.lock:
            self.last_message = f"calibrating {calib.role} — sweep every joint through its range"
        return {"ok": True}

    def _calibrate_finish(self) -> dict[str, Any]:
        calib = self.calib
        if calib is None:
            return {"ok": False, "error": "not calibrating"}
        if calib.phase != Calibration.RANGE:
            return {"ok": False, "error": "set the home position first"}
        unswept = calib.unswept()
        if unswept:
            return {"ok": False, "error": f"not moved yet: {', '.join(unswept)}"}
        try:
            with self.hw_lock:
                calib.finish()
        except Exception as err:  # noqa: BLE001
            return {"ok": False, "error": f"could not write calibration: {err}"}
        self.calib = None
        with self.lock:
            self.state = IDLE
            self.last_message = f"calibrated {calib.role} → {getattr(calib.arm, 'calibration_fpath', 'motors')}"
        log.info("calibration written for %s", calib.role)
        return {"ok": True}

    def _calibrate_cancel(self) -> dict[str, Any]:
        calib = self.calib
        if calib is None:
            return {"ok": False, "error": "not calibrating"}
        try:
            with self.hw_lock:
                calib.cancel()
        except Exception as err:  # noqa: BLE001
            log.warning("could not restore the previous calibration: %s", err)
        self.calib = None
        with self.lock:
            self.state = IDLE
            self.last_message = f"calibration of {calib.role} cancelled"
        return {"ok": True}

    # -- tracking delta -------------------------------------------------

    def _update_delta(self, action: dict[str, Any] | None, observation: dict[str, Any] | None) -> None:
        """Display alignment in calibrated action units without gating live teleop."""
        if not action or not observation:
            return
        joints: dict[str, Any] = {}
        worst, worst_joint = 0.0, None
        for key, commanded in action.items():
            if not key.endswith(".pos"):
                continue
            measured = observation.get(key)
            if not isinstance(measured, (int, float)) or not isinstance(commanded, (int, float)):
                continue
            delta = abs(float(commanded) - float(measured))
            joints[key[: -len(".pos")]] = {
                "leader": round(float(commanded), 2),
                "follower": round(float(measured), 2),
                "delta": round(delta, 2),
            }
            if delta > worst:
                worst, worst_joint = delta, key[: -len(".pos")]

        over = worst > self.delta_limit
        with self.lock:
            if self.state == RECORDING:
                self._quality["max_tracking_delta"] = max(self._quality.get("max_tracking_delta", 0.0), round(worst, 2))
            self._delta = {
                "joints": joints,
                "max": round(worst, 2),
                "max_joint": worst_joint,
                "limit": self.delta_limit,
                "over": over,
            }

    # -- camera streams -------------------------------------------------

    def _publish_frames(self, observation: dict[str, Any] | None) -> None:
        """Re-encode the frames the loop already grabbed, for the MJPEG streams.

        Throttled to `--stream-fps`: the cameras are read at the control rate
        either way, and JPEG-encoding two 640×480 frames 30 times a second buys
        nothing a browser can show.
        """
        now = time.time()
        if now - self._stream_at < self._stream_period:
            return
        self._stream_at = now

        # A tick that produced no observation -- a dropped USB packet, or the
        # bot unplugged entirely -- still has cameras worth watching: they run
        # their own capture threads and know nothing about the motor bus. Fall
        # back to their frames rather than publishing none, which would stall
        # every open MJPEG stream on an arm fault and black the panel out.
        if not observation:
            observation = self._camera_only_observation()

        encoded: dict[str, bytes] = {}
        for name in self.camera_meta:
            frame = observation.get(name)
            if isinstance(frame, np.ndarray) and frame.ndim == 3:
                jpeg = encode_jpeg(frame, self._stream_quality)
                if jpeg:
                    encoded[name] = jpeg
        if not encoded:
            return
        with self._stream_cv:
            self._jpegs.update(encoded)
            self._stream_seq += 1
            self._stream_cv.notify_all()

    # -- power panel ----------------------------------------------------

    def _update_hardware(self, observation: dict[str, Any] | None) -> None:
        try:
            now = time.time()
            is_recording = (self.state == RECORDING or self._autonomy is not None)

            if now - self._motors_probed_at >= 5.0 and self.state not in (CALIBRATING,):
                self._motors_probed_at = now
                for attr_probe, attr_temp, arm in (
                    ("_teleop_motors", "_teleop_temps", self.teleop),
                    ("_follower_motors", "_follower_temps", self.robot),
                ):
                    bus = getattr(arm, "bus", None)
                    if bus is None:
                        continue
                    try:
                        with self.hw_lock:
                            if not is_recording:
                                setattr(self, attr_probe, motor_probe(bus))
                            else:
                                # During recording, positions are actively read every tick, so motors are alive
                                setattr(self, attr_probe, {name: True for name in getattr(bus, "motors", {})})
                            setattr(self, attr_temp, motor_temperatures(bus))
                            if not self.engaged and self.calib is None:
                                role = "follower" if arm is self.robot else "teleop"
                                self._motor_faults[role] = read_motor_faults(bus)
                    except Exception:  # noqa: BLE001
                        pass

            teleop_port = getattr(self.teleop, "bus", None) and self.teleop.bus.port
            follower_port = getattr(self.robot, "bus", None) and self.robot.bus.port
            teleop_ok = sum(self._teleop_motors.values()) if self._teleop_motors else 0
            follower_ok = sum(self._follower_motors.values()) if self._follower_motors else 0

            teleop = build_arm_power(
                role="teleop",
                port=teleop_port,
                usb=getattr(self.teleop, "is_connected", False),
                powered=getattr(self.teleop, "is_connected", False) and teleop_ok > 0,
                motors_ok=teleop_ok,
                motors_total=len(self._teleop_motors) or 6,
                temperatures=self._teleop_temps,
                message="torque cut by the e-stop" if self.estop else None,
            )
            follower = build_arm_power(
                role="follower",
                port=follower_port,
                usb=getattr(self.robot, "is_connected", False),
                powered=getattr(self.robot, "is_connected", False) and follower_ok > 0,
                motors_ok=follower_ok,
                motors_total=len(self._follower_motors) or 6,
                temperatures=self._follower_temps,
                message="torque cut by the e-stop" if self.estop else None,
            )
            for role, health in (("teleop", teleop), ("follower", follower)):
                faults = self._motor_faults.get(role, {})
                if faults:
                    health["faults"] = dict(faults)
                    health["status"] = "fail"
                    health["message"] = "; ".join(f"{joint}: {reason}" for joint, reason in faults.items())
                    health["motors_ok"] = max(0, health["motors_ok"] - len(faults))

            cameras: dict[str, Any] = {}
            for name, index in self.camera_meta.items():
                frame = (observation or {}).get(name)
                streaming = isinstance(frame, np.ndarray) and frame.ndim == 3
                cameras[name] = build_camera_power(
                    name=name,
                    index=index,
                    usb=getattr(self.robot, "is_connected", False),
                    streaming=streaming,
                    message=None if streaming else "no frame from camera",
                )

            overall = aggregate_status([teleop["status"], follower["status"], *(c["status"] for c in cameras.values())])

            with self.lock:
                self._hardware = {
                    "source": "recorder",
                    "status": "fail" if self.estop else overall,
                    "teleop": teleop,
                    "follower": follower,
                    "cameras": cameras,
                }
        except Exception as err:
            log.debug("error updating hardware: %s", err)


def encode_jpeg(frame: np.ndarray, quality: int = 70) -> bytes | None:
    """RGB ndarray → JPEG bytes. Cameras hand back RGB; cv2 encodes BGR."""
    try:
        import cv2
    except ImportError:  # pragma: no cover - cv2 ships with the camera extra
        return None
    ok, buffer = cv2.imencode(".jpg", frame[:, :, ::-1], [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return buffer.tobytes() if ok else None


# -- websocket (RFC 6455, server side only) -------------------------------
#
# A dependency-free implementation, because the venv has no websockets library
# and the daemon only needs the narrow half of the protocol: accept one upgrade,
# push text frames, notice when the client leaves. Reading is limited to
# recognising close and ping, since nothing the browser sends here matters.

WS_GUID = "258EAFA5-E914-47DA-95CA-5AB0DC85B11F"


def ws_accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def ws_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """One unfragmented, unmasked frame. Servers never mask."""
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header += struct.pack("!H", length)
    else:
        header.append(127)
        header += struct.pack("!Q", length)
    return bytes(header) + payload


def ws_read_opcode(sock: socket.socket) -> tuple[int | None, bytes]:
    """Opcode and payload of one waiting client frame, or (None, b"") if nothing is pending.

    Client frames are always masked; close (0x8) and ping (0x9) are handled.
    """
    try:
        first = sock.recv(2)
    except (BlockingIOError, TimeoutError):
        return None, b""
    except OSError as e:
        if getattr(e, "errno", None) in (11, 35):
            return None, b""
        return 0x8, b""
    if len(first) == 0:
        return 0x8, b""
    if len(first) < 2:
        return None, b""

    opcode = first[0] & 0x0F
    masked = bool(first[1] & 0x80)
    length = first[1] & 0x7F
    payload = b""
    try:
        if length == 126:
            length = struct.unpack("!H", recv_exactly(sock, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", recv_exactly(sock, 8))[0]
        mask_key = recv_exactly(sock, 4) if masked else b""
        if length:
            raw = recv_exactly(sock, length)
            if masked:
                payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(raw))
            else:
                payload = raw
    except OSError:
        return 0x8, b""
    return opcode, payload


def recv_exactly(sock: socket.socket, count: int) -> bytes:
    chunks = []
    while count:
        try:
            chunk = sock.recv(count)
        except (BlockingIOError, TimeoutError):
            continue
        except OSError as e:
            if getattr(e, "errno", None) in (11, 35):
                continue
            raise
        if not chunk:
            raise OSError("connection closed")
        chunks.append(chunk)
        count -= len(chunk)
    return b"".join(chunks)


def save_recording_episode(dataset: LeRobotDataset) -> None:
    """Save cameras introduced after older episodes without fabricating old video paths.

    The installed LeRobot writer assumes the last old episode contains every
    camera when choosing a new video shard. A newly added camera has no such
    history. Hide that history only during its first video write; restore the
    complete episode metadata immediately afterward.
    """
    writer = getattr(dataset, "writer", None)
    if writer is None:
        dataset.save_episode()
        return
    original = writer._save_episode_video

    def save_camera(video_key, *args, **kwargs):
        meta = dataset.meta
        episodes = meta.episodes
        field = f"videos/{video_key}/chunk_index"
        new_camera = (episodes is not None and len(episodes) > 0
                      and episodes[-1].get(field) is None and field not in (meta.latest_episode or {}))
        if not new_camera:
            return original(video_key, *args, **kwargs)
        first_video = dataset.root / meta.video_path.format(video_key=video_key, chunk_index=0, file_index=0)
        if first_video.exists():
            raise RuntimeError(f"Cannot initialize new camera over an existing unreferenced video: {first_video}")
        try:
            meta.episodes = None
            return original(video_key, *args, **kwargs)
        finally:
            meta.episodes = episodes

    writer._save_episode_video = save_camera
    try:
        dataset.save_episode()
    finally:
        writer._save_episode_video = original


def load_recording_episodes(root):
    """Read local episode metadata directly, including missing older camera columns.

    Avoid the HF cache/build pass on every save. Arrow unions the camera schemas
    and uses nulls for cameras not present in older episodes.
    """
    import datasets
    import pyarrow as pa
    import pyarrow.parquet as pq
    paths = sorted((Path(root)/"meta"/"episodes").rglob("*.parquet"))
    if not paths:
        raise RuntimeError("Saved episode metadata is missing")
    combined = pa.unify_schemas([pq.read_schema(path) for path in paths])
    schema = pa.schema([field for field in combined if not field.name.startswith("stats/")])
    table = pa.concat_tables([pq.read_table(path, schema=schema) for path in paths])
    return datasets.Dataset(table.replace_schema_metadata(None))


def resume_recording(**kwargs):
    # The installed resume API has no metadata-loader argument. Scope this
    # local-reader adapter to resume and restore the library immediately.
    import lerobot.datasets.dataset_metadata as metadata_module
    original = metadata_module.load_episodes
    metadata_module.load_episodes = load_recording_episodes
    try:
        return LeRobotDataset.resume(**kwargs)
    finally:
        metadata_module.load_episodes = original


def reopen(dataset: LeRobotDataset) -> LeRobotDataset:
    """Close the writer (flushing metadata) and reopen for appending.

    ``root`` has to be passed explicitly -- resume() refuses to create a writer
    over the Hub snapshot cache, and we always want the local directory anyway.
    """
    repo_id, root = dataset.repo_id, dataset.root
    dataset.finalize()
    ds = resume_recording(
        repo_id=repo_id,
        root=root,
        rgb_encoder=RGBEncoderConfig(vcodec="h264", preset="ultrafast"),
        streaming_encoding=True,
    )
    ds.meta._metadata_buffer_size = 1
    return ds


def make_handler(recorder: Recorder):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):  # noqa: D102 - the loop already logs what matters
            pass

        def _reply(self, body: dict, status: int = 200) -> None:
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            path = url.path.rstrip("/")
            query = parse_qs(url.query)
            name = (query.get("camera") or [""])[0]

            if path in ("", "/status"):
                self._reply(recorder.status())
            elif path == "/motors":
                self._reply(recorder.motor_telemetry())
            elif path == "/harness/status":
                self._reply(recorder.harness.status())
            elif path == "/harness/evidence":
                data = recorder.harness.evidence((query.get("run_id") or [""])[0], (query.get("frame") or [""])[0])
                if data is None:
                    self._reply({"error": "Evidence not found"}, 404)
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
            elif path == "/ws":
                self._websocket()
            elif path == "/snapshot":
                self._snapshot(name)
            elif path == "/stream":
                self._stream(name)
            else:
                self._reply({"error": "not found"}, 404)

        def _websocket(self) -> None:
            """Push status to the dashboard until the client goes away.

            Replaces a 4 Hz poll per panel. The saving is not really the request
            count -- it is that a poll against a daemon that is not running is a
            failed request every 250ms forever, whereas a socket that will not
            open is one failed connect and a backoff.
            """
            key = self.headers.get("Sec-WebSocket-Key")
            if not key or "websocket" not in (self.headers.get("Upgrade") or "").lower():
                self._reply({"error": "expected a websocket upgrade"}, 400)
                return

            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", ws_accept_key(key))
            self.end_headers()
            self.close_connection = True

            sock = self.connection
            # Short timeout rather than non-blocking: recv is only used to notice
            # the client leaving, and it must not stall the push cadence.
            sock.settimeout(0.01)
            try:
                while True:
                    try:
                        payload = json.dumps(recorder.status()).encode()
                        sock.settimeout(None)
                        sock.sendall(ws_frame(payload))
                        sock.settimeout(0.01)
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    except Exception as err:
                        log.debug("websocket status push error: %s", err)

                    opcode, client_payload = ws_read_opcode(sock)
                    if opcode == 0x8:
                        break
                    if opcode == 0x9:
                        try:
                            sock.settimeout(None)
                            sock.sendall(ws_frame(client_payload, opcode=0xA))
                            sock.settimeout(0.01)
                        except Exception:
                            break
                    time.sleep(WS_PERIOD)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                try:
                    sock.settimeout(None)
                except OSError:
                    pass

        def _snapshot(self, name: str) -> None:
            jpeg = recorder.jpeg(name)
            if jpeg is None:
                self._reply({"error": f"no frames from camera {name!r}"}, 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(jpeg)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(jpeg)

        def _stream(self, name: str) -> None:
            """MJPEG: one multipart part per frame, held open until the client
            goes away. An <img src> renders this with no JavaScript at all."""
            if name not in recorder.camera_meta:
                self._reply({"error": f"unknown camera {name!r}"}, 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
            self.send_header("Cache-Control", "no-store, no-cache, private")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            seq = -1
            try:
                while True:
                    jpeg, seq = recorder.wait_for_jpeg(name, seq)
                    if jpeg is None:
                        continue
                    self.wfile.write(
                        b"--" + BOUNDARY.encode() + b"\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                    )
                    self.wfile.write(jpeg)
                    self.wfile.write(b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self) -> None:  # noqa: N802
            origin = self.headers.get("Origin")
            parsed_origin = urlparse(origin) if origin else None
            frontend_origin = bool(parsed_origin and parsed_origin.scheme == "http" and parsed_origin.hostname in ("127.0.0.1", "localhost") and parsed_origin.port in (3000, 3217))
            if origin and urlparse(origin).netloc != self.headers.get("Host") and not frontend_origin:
                self._reply({"ok": False, "error": "Cross-origin robot commands disabled"}, 403)
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > 65536:
                self._reply({"ok": False, "error": "Request too large"}, 413)
                return
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                payload = {}
            name = self.path.strip("/").split("/")[-1]
            if self.path.startswith("/harness/"):
                try:
                    if not isinstance(payload, dict):
                        raise ValueError("Request must be a JSON object")
                    self._reply(recorder.harness.command(name, payload))
                except Exception as error:
                    self._reply({"ok": False, "error": str(error)}, 409)
                return
            if name not in COMMANDS:
                self._reply({"error": "not found"}, 404)
                return
            result = recorder.command(name, payload)
            self._reply({**result, **recorder.status()}, 200 if result.get("ok") else 409)

    return Handler


COMMANDS = {
    "record",
    "stop",
    "discard",
    "save",
    "task",
    "estop",
    "rearm",
    "engage",
    "disengage",
    "sync",
    "calibrate_start",
    "calibrate_home",
    "calibrate_finish",
    "calibrate_cancel",
}


def read_motor_faults(bus) -> dict[str, str]:
    """Read status flags without energizing motors or treating a reply as health."""
    from lerobot.motors.motors_bus import get_address

    faults = {}
    for name, motor in bus.motors.items():
        try:
            address, length = get_address(bus.model_ctrl_table, motor.model, "Torque_Enable")
            _, comm, error = bus._read(address, length, motor.id, num_retry=0, raise_on_error=False)
            if not bus._is_comm_success(comm):
                faults[name] = "no valid response; inspect power and motor cable"
            elif error:
                label = "overload" if error & 0x20 else "motor fault"
                faults[name] = f"{label} ({error:#04x}); support the arm, remove load, and power-cycle its motor supply"
        except Exception as exc:
            faults[name] = f"fault check failed: {exc}"
    return faults


def read_torque_enable(bus, name):
    """Raw Torque_Enable that tolerates a latched fault bit in the status byte.

    lerobot raises on any nonzero status byte even when the packet arrived
    intact. A motor with a latched fault (e.g. 0x10 angle limit) still applies
    and reports Torque_Enable correctly, so only a comm failure is fatal here.
    """
    from lerobot.motors.motors_bus import get_address

    motor = bus.motors[name]
    addr, length = get_address(bus.model_ctrl_table, motor.model, "Torque_Enable")
    value, comm, error = bus._read(addr, length, motor.id, num_retry=3, raise_on_error=False)
    if not bus._is_comm_success(comm):
        raise ConnectionError(f"Torque_Enable read on {name}: {bus.packet_handler.getTxRxResult(comm)}")
    if bus._is_error(error):
        log.warning("%s reports status fault %#04x while reading Torque_Enable", name, error)
    return value


def release_all_torque(bus):
    """Try every joint; confirm a lost write acknowledgement with readback."""
    failures = []
    for name in bus.motors:
        try:
            bus.disable_torque(name, num_retry=3)
        except Exception as error:
            try:
                # A motor can apply the write even when its acknowledgement is
                # lost or carries a fault flag. Require two fresh off readings
                # before accepting it.
                for _ in range(2):
                    value = read_torque_enable(bus, name)
                    if value != 0:
                        raise RuntimeError(f"Torque_Enable readback is {value!r}, expected 0")
            except Exception as read_error:
                failures.append(f"{name}: {error}; readback failed: {read_error}")
            else:
                log.warning("Torque-off acknowledgement failed on %s; two readbacks confirmed torque off: %s", name, error)
    if failures:
        raise RuntimeError("Could not confirm torque off on " + "; ".join(failures))


def configure_at_measured_pose(robot):
    """Native configure enables torque; overwrite stale goals before it does."""
    release_all_torque(robot.bus)
    faults = read_motor_faults(robot.bus)
    if faults:
        raise RuntimeError("Follower cannot engage: " + "; ".join(f"{name}: {reason}" for name, reason in faults.items()))
    present = robot.bus.sync_read("Present_Position", normalize=False, num_retry=3)
    if not isinstance(present, dict) or set(present) != set(robot.bus.motors) or not all(np.isfinite(v) for v in present.values()):
        raise RuntimeError("Not all follower motors responded during configuration")
    # A joint resting a few ticks past its calibrated limit (shoulder_lift
    # folded onto its rest) would get an out-of-range goal, which latches the
    # STS angle fault (0x10) and makes every later reply look like a failure.
    goal = {}
    for name, value in present.items():
        cal = robot.bus.calibration[name]
        goal[name] = int(min(max(value, cal.range_min), cal.range_max))
        if goal[name] != value:
            log.info("%s rests at %d, outside calibrated %d..%d; seeding goal %d", name, value, cal.range_min, cal.range_max, goal[name])
    robot.bus.sync_write("Goal_Position", goal, normalize=False)
    try:
        robot.configure()
    except Exception:
        release_all_torque(robot.bus)
        raise


def build_hardware(args):
    camera_meta = {spec.split("=", 1)[0]: int(spec.split("=", 1)[1]) for spec in args.camera}

    # Refuse before anything is opened. A crossed index still streams, still
    # fills both observation keys, and only shows up as a policy that reaches
    # for nothing -- so this has to be an exit, not a warning.
    if camera_meta != TRAINING_CAMERAS:
        want = " ".join(f"--camera {name}={index}" for name, index in sorted(TRAINING_CAMERAS.items()))
        got = " ".join(f"--camera {name}={index}" for name, index in sorted(camera_meta.items())) or "(none)"
        raise SystemExit(
            f"camera mapping does not match {CAMERAS_CONFIG_PATH}: expected {want}, got {got}. "
            "The labels are semantic -- wrist is the close gripper view, overhead the wide workspace "
            "view -- so fix the indices, not the names."
        )

    from lerobot.cameras.opencv import OpenCVCameraConfig
    from lerobot.robots.so_follower import SO101Follower, SO101FollowerConfig
    from lerobot.teleoperators.so_leader import SO101Leader, SO101LeaderConfig

    cameras = {
        name: OpenCVCameraConfig(index_or_path=index, width=args.width, height=args.height, fps=args.fps)
        for name, index in camera_meta.items()
    }

    robot = SO101Follower(SO101FollowerConfig(port=args.robot_port, id=args.robot_id, cameras=cameras))
    teleop = SO101Leader(SO101LeaderConfig(port=args.teleop_port, id=args.teleop_id))
    return robot, teleop, camera_meta


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo-id", required=True)
    parser.add_argument("--task", default=TRAINING_TASK)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument(
        "--commit-seconds",
        type=float,
        default=6.0,
        help="How long after stopping you can still discard the take for free.",
    )
    parser.add_argument(
        "--flush-every",
        type=int,
        default=1,
        help="Reopen the dataset every N saved episodes. 1 (the default) makes every take "
        "immediately reviewable, at the cost of one data/video file per episode. Raise it "
        "to let LeRobot pack episodes into larger files, at the cost of liveness.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8611)
    parser.add_argument("--robot-port")
    parser.add_argument("--robot-id", default="follower")
    parser.add_argument("--teleop-port")
    parser.add_argument("--teleop-id", default="leader")
    parser.add_argument(
        "--camera",
        action="append",
        default=[],
        metavar="NAME=INDEX",
        help="Repeatable; must match config/cameras.json, currently "
        + " ".join(f"--camera {name}={index}" for name, index in sorted(TRAINING_CAMERAS.items())),
    )
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument(
        "--delta-limit",
        type=float,
        default=25.0,
        help="Initial alignment limit in calibrated action units; live teleop is not stopped by this gap.",
    )
    parser.add_argument(
        "--engage-on-start",
        action="store_true",
        help="Hand the follower to the leader as soon as the arms are read, instead of starting in "
        "observing mode. Still refused if the two are further apart than --delta-limit.",
    )
    parser.add_argument(
        "--rest-seconds",
        type=float,
        default=DEFAULT_REST_SECONDS,
        help="Seconds used to ease the follower to the relaxed pose before torque is released.",
    )
    parser.add_argument(
        "--start-seconds",
        type=float,
        default=DEFAULT_START_SECONDS,
        help="Seconds used to raise the follower to START_POSE at the start of an engage.",
    )
    parser.add_argument(
        "--sync-seconds",
        type=float,
        default=DEFAULT_SYNC_SECONDS,
        help="Seconds used to ease from START_POSE onto the leader's live pose before tracking begins.",
    )
    parser.add_argument("--stream-fps", type=float, default=10.0, help="Rate of the browser camera streams.")
    parser.add_argument("--stream-quality", type=int, default=70, help="JPEG quality of the camera streams.")
    parser.add_argument("--offline", action="store_true", help="Serve prompt planning and skill import without opening hardware; physical commands stay blocked.")
    args = parser.parse_args()

    if not (args.robot_port and args.teleop_port):
        parser.error("--robot-port and --teleop-port are required")

    robot, teleop, camera_meta = build_hardware(args)

    # Connecting the arms, opening the cameras and creating or resuming the
    # dataset can take several seconds on real hardware -- long enough that a
    # Stop pressed during this window is not a hypothetical. Nothing has
    # recorded a frame yet at this point, so there is nothing to save; the
    # only job here is to say so plainly and leave the ports closed, instead
    # of an interrupt landing mid-connect with no handler for it and exiting
    # in whatever state that leaves things, unexplained in the log.
    try:
        if not args.offline:
            # Native connect/configure re-enables torque. Seed existing calibrated
            # goal registers from live measurements first so reconnect cannot snap
            # toward a previous controller's goal.
            robot.bus.connect()
            release_all_torque(robot.bus)
            if not robot.is_calibrated:
                raise RuntimeError("Follower calibration mismatch; calibrate the arm before connecting Homeboy")
            try:
                configure_at_measured_pose(robot)
            finally:
                release_all_torque(robot.bus)
            for camera in robot.cameras.values():
                camera.connect()
            teleop.connect(calibrate=False)

        features = {
            **hw_to_dataset_features(robot.observation_features, OBS_STR, use_video=True),
            **hw_to_dataset_features(robot.action_features, ACTION, use_video=True),
        }

        from lerobot.utils.constants import HF_LEROBOT_HOME

        dataset_root = HF_LEROBOT_HOME / args.repo_id
        added_camera_features = extend_video_camera_schema(dataset_root, features)
        if added_camera_features:
            log.info("extended existing dataset camera schema with %s; older episodes keep their original camera views",
                     ", ".join(added_camera_features))

        if dataset_root.exists():
            # `root` is required: without it resume() would build a writer over the
            # revision-safe Hub snapshot cache, which it refuses to do. Same reason
            # as in reopen() -- and we always want the local directory anyway.
            dataset = resume_recording(
                repo_id=args.repo_id,
                root=dataset_root,
                rgb_encoder=RGBEncoderConfig(vcodec="h264", preset="ultrafast"),
                streaming_encoding=True,
            )
            dataset.meta._metadata_buffer_size = 1
            log.info("resuming %s at %d episodes", args.repo_id, dataset.meta.total_episodes)
            # Newly configured cameras were added to the video schema above;
            # the existing episode rows retain only the cameras they contain.
            existing = {key.split(".")[-1] for key in dataset.meta.features if key.startswith(f"{OBS_STR}.images.")}
            if not existing.issubset(camera_meta):
                parser.error(
                    f"{args.repo_id} was recorded with cameras {sorted(existing)}, which are not all present in "
                    f"the configured cameras {sorted(camera_meta)}. Restore the missing camera or record into a new --repo-id."
                )
        else:
            dataset = LeRobotDataset.create(
                repo_id=args.repo_id,
                fps=args.fps,
                features=features,
                robot_type=getattr(robot, "name", "so101_follower"),
                root=dataset_root,
                use_videos=True,
                metadata_buffer_size=1,
                streaming_encoding=True,
                # AV1 (the default) only decodes in Safari on M3 and newer; the
                # dashboard has to play these back.
                rgb_encoder=RGBEncoderConfig(vcodec="h264", preset="ultrafast"),
            )
            dataset.meta._metadata_buffer_size = 1
            log.info("created %s", args.repo_id)

        recorder = Recorder(
            robot,
            teleop,
            dataset,
            args.fps,
            args.task,
            args.commit_seconds,
            args.flush_every,
            camera_meta=camera_meta,
            delta_limit=args.delta_limit,
            engage_on_start=args.engage_on_start,
            rest_seconds=args.rest_seconds,
            start_seconds=args.start_seconds,
            sync_seconds=args.sync_seconds,
            stream_fps=args.stream_fps,
            stream_quality=args.stream_quality,
        )
        recorder.offline = args.offline
    except KeyboardInterrupt:
        log.info("interrupted while connecting — nothing was recorded")
        if robot.is_connected:
            robot.disconnect()
        if teleop.is_connected:
            teleop.disconnect()
        return 0
    except Exception:
        # Startup can fail after opening only one bus or camera.
        for arm in (robot, teleop):
            try:
                if arm.bus.is_connected:
                    try:
                        release_all_torque(arm.bus)
                    finally:
                        arm.bus.disconnect(disable_torque=False)
            except Exception as error:
                log.warning("startup cleanup: %s", error)
        for camera in robot.cameras.values():
            try:
                camera.disconnect()
            except Exception:
                pass
        raise

    server = ThreadingHTTPServer((args.host, args.port), make_handler(recorder))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("control API on http://%s:%d — ready", args.host, args.port)
    for name, index in camera_meta.items():
        log.info("camera %s (index %d) at http://%s:%d/stream?camera=%s", name, index, args.host, args.port, name)

    period = 1 / args.fps
    try:
        while True:
            start = time.perf_counter()
            try:
                recorder.step()
            except Exception as err:  # noqa: BLE001
                log.warning("error in control loop step: %s", err)
            time.sleep(max(0.0, period - (time.perf_counter() - start)))
    except KeyboardInterrupt:
        log.info("shutting down")
        # An in-flight take is worth more than a clean exit; commit it.
        if recorder.state in (RECORDING, PENDING):
            recorder._save()
        recorder.dataset.finalize()
    finally:
        server.shutdown()
        recorder._kill_torque()
        if not args.offline:
            # Torque-off has already been attempted per joint and verified.
            # Close both ports even if one native disconnect would raise on a
            # fault-flagged torque acknowledgement.
            try:
                robot.bus.disconnect(disable_torque=False)
            finally:
                teleop.bus.disconnect(disable_torque=False)
                for camera in robot.cameras.values():
                    camera.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
