"""Bounded local trajectory interpreter. No model or serial dependencies."""
import bisect
import math

MAX_SPEED = 3.0  # Calibrated normalized units/s, not degrees or Cartesian speed.
MAX_COMMAND_STEP = .15

JOINTS = tuple(f"{j}.pos" for j in (
    "shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"))


def validate_pose(pose):
    if not isinstance(pose, dict) or set(pose) != set(JOINTS):
        raise ValueError("Require exactly the six calibrated SO-101 joints")
    for key, value in pose.items():
        low = 0 if key == "gripper.pos" else -100
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= 100:
            raise ValueError(f"{key} is outside calibrated normalized bounds [{low}, 100]")


class MotionProgram:
    """Time interpolation, speed limit, tracking checks and finite hold lease.

    Targets use LeRobot RANGE_M100_100, gripper RANGE_0_100, never radians.
    Retime each segment to preserve the joint path without slowing idle periods.
    """
    def __init__(self, frames, speed=MAX_SPEED):
        if not isinstance(frames, list) or not 2 <= len(frames) <= 18000:
            raise ValueError("A skill needs 2–18000 timestamped frames")
        if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not math.isfinite(speed) or not 0 < speed <= MAX_SPEED:
            raise ValueError(f"Speed must be positive and at most {MAX_SPEED:g} normalized units per second")
        self.speed = speed
        previous = -1.0
        times = [0.0]
        for i, frame in enumerate(frames):
            validate_pose(frame.get("pose"))
            t = frame.get("t")
            if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) or t < 0 or t <= previous:
                raise ValueError("Frame timestamps must be finite and strictly increasing")
            if i:
                distance = max(abs(frame["pose"][k] - frames[i-1]["pose"][k]) for k in JOINTS)
                times.append(times[-1] + max(t-previous, distance/speed))
            previous = t
        if frames[0]["t"] != 0:
            raise ValueError("First timestamp must be zero")
        self.frames = frames
        self.times = times
        self.duration = self.times[-1]
        if self.duration > 300:
            raise ValueError("Skill exceeds the 300-second execution budget after speed limiting")
        self.started = None
        self.last_tick = None
        self.last_target = None
        self.complete = False

    def start(self, measured, now):
        validate_pose(measured)
        gap = max(abs(measured[k] - self.frames[0]["pose"][k]) for k in JOINTS)
        if gap > 5:
            raise ValueError(f"Align follower to skill start pose; largest difference is {gap:.1f} units (limit 5)")
        self.started = now
        self.last_tick = now
        self.last_target = dict(measured)

    def tick(self, measured, now):
        validate_pose(measured)
        if self.started is None:
            raise ValueError("Program has not started")
        elapsed = now - self.started
        dt = now - self.last_tick
        if dt < 0 or dt > .5:
            raise RuntimeError("Control loop missed its 500 ms deadline")
        if elapsed > self.duration + 90:
            raise RuntimeError("Verification hold lease expired")
        if max(abs(measured[k] - self.last_target[k]) for k in JOINTS) > 8:
            raise RuntimeError("Follower is not tracking the commanded pose")
        t = min(self.times[-1], elapsed)
        index = min(len(self.frames)-2, max(0, bisect.bisect_right(self.times, t)-1))
        a, b = self.frames[index:index+2]
        fraction = (t-self.times[index]) / (self.times[index+1]-self.times[index])
        desired = {k: a["pose"][k] + fraction*(b["pose"][k]-a["pose"][k]) for k in JOINTS}
        # Bound each physical command even after a delayed tick or start offset.
        limit = min(MAX_COMMAND_STEP, self.speed * dt)
        target = {k: self.last_target[k] + max(-limit, min(limit, desired[k]-self.last_target[k])) for k in JOINTS}
        self.complete = elapsed >= self.duration and max(abs(measured[k]-b["pose"][k]) for k in JOINTS) <= 2
        if elapsed > self.duration + 5 and not self.complete:
            raise RuntimeError("Final pose did not settle within five seconds")
        self.last_tick, self.last_target = now, target
        return target
