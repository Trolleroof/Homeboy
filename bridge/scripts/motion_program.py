"""Bounded local trajectory interpreter. No model or serial dependencies."""
import bisect
import math

MAX_SPEED = 18.0  # Calibrated normalized units/s, not degrees or Cartesian speed.
MAX_COMMAND_STEP = .90
MEASURED_BOUND_TOLERANCE = 2.0  # Small endpoint overshoot in normalized encoder feedback.
# The resting shoulder is observed at -103.96; imported demonstrations already
# permit four units of endpoint overshoot. Include a small readout margin only
# for this joint. This never widens the recorded-target range.
MEASURED_JOINT_TOLERANCE = {'shoulder_lift.pos': 4.25}

JOINTS = tuple(f"{j}.pos" for j in (
    "shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"))


def validate_pose(pose, *, measured=False):
    if not isinstance(pose, dict) or set(pose) != set(JOINTS):
        raise ValueError("Require exactly the six calibrated SO-101 joints")
    for key, value in pose.items():
        low = 0 if key == "gripper.pos" else -100
        tolerance = MEASURED_JOINT_TOLERANCE.get(key, MEASURED_BOUND_TOLERANCE) if measured else 0
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low-tolerance <= value <= 100+tolerance:
            if measured:
                raise ValueError(f"Measured {key}={value} is outside feedback bounds [{low-tolerance:g}, {100+tolerance:g}]; command targets remain [{low}, 100]")
            raise ValueError(f"{key} is outside calibrated normalized bounds [{low}, 100]")


def approach_demo_start(frames, measured, speed=MAX_SPEED):
    """Prepend a local, speed-limited approach; never alter the taught path."""
    MotionProgram(frames, speed)
    validate_pose(measured, measured=True)
    gap = max(abs(measured[k]-frames[0]['pose'][k]) for k in JOINTS)
    if gap <= 5:
        return frames, 0.0
    if gap > 25:
        raise ValueError(f"Demo start is {gap:.1f} units away; bounded automatic approach limit is 25. Move closer first.")
    anchor = {k: max(0 if k == 'gripper.pos' else -100, min(100, measured[k])) for k in JOINTS}
    duration = max(abs(anchor[k]-frames[0]['pose'][k]) for k in JOINTS)/speed
    prepared = [{'t': 0.0, 'pose': anchor}] + [{'t': f['t']+duration, 'pose': dict(f['pose'])} for f in frames]
    MotionProgram(prepared, speed)
    return prepared, duration


def speed_for_duration(frames, seconds=30.0, measured=None):
    """Fit playback timing only; preserve every recorded motor value."""
    def duration(speed):
        prepared, _ = approach_demo_start(frames, measured, speed) if measured is not None else (frames, 0)
        return MotionProgram(prepared, speed).duration
    # Avoid constructing ultra-slow programs outside the execution budget.
    low, high = 1.0, MAX_SPEED
    if duration(high) >= seconds:
        return high
    try:
        if duration(low) <= seconds:
            return high  # Do not slow an already short clip to fill the budget.
    except ValueError as error:
        if '300-second' not in str(error):
            raise
    for _ in range(32):
        mid = (low+high)/2
        try:
            too_slow = duration(mid) > seconds
        except ValueError as error:
            if '300-second' not in str(error):
                raise
            too_slow = True
        if too_slow:
            low = mid
        else:
            high = mid
    return high


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
        self.horizon = None

    def elapsed(self, now):
        elapsed = max(0, now-self.started) if self.started is not None else 0
        return min(elapsed, self.horizon) if self.horizon is not None else elapsed

    def advance_turn(self, measured, now, seconds=2):
        validate_pose(measured, measured=True)
        if self.horizon is None or self.elapsed(now) < self.horizon or self.complete:
            return False
        if max(abs(measured[k]-self.last_target[k]) for k in JOINTS) > 2:
            return False
        index = min(len(self.frames)-2, max(0, bisect.bisect_right(self.times, self.horizon)-1))
        a, b = self.frames[index:index+2]
        fraction = (self.horizon-self.times[index])/(self.times[index+1]-self.times[index])
        expected = {k: a['pose'][k]+fraction*(b['pose'][k]-a['pose'][k]) for k in JOINTS}
        if max(abs(measured[k]-expected[k]) for k in JOINTS) > 2:
            return False
        # Time spent holding a checkpoint is not trajectory time. Resume from
        # this point, rather than jumping ahead to catch up with wall time.
        self.started += max(0, now-self.started-self.horizon)
        self.horizon = min(self.duration, self.horizon+seconds)
        return True

    def start(self, measured, now):
        validate_pose(measured, measured=True)
        gap = max(abs(measured[k] - self.frames[0]["pose"][k]) for k in JOINTS)
        if gap > 5:
            joint = max(JOINTS, key=lambda k: abs(measured[k]-self.frames[0]["pose"][k]))
            raise ValueError(f"Align follower to skill start pose: {joint} is {measured[joint]:.1f}, needs {self.frames[0]['pose'][joint]:.1f}; difference {gap:.1f} (limit 5)")
        self.started = now
        self.last_tick = now
        self.last_target = dict(measured)

    def tick(self, measured, now):
        validate_pose(measured, measured=True)
        if self.started is None:
            raise ValueError("Program has not started")
        elapsed = self.elapsed(now)
        dt = now - self.last_tick
        if dt < 0 or dt > .5:
            raise RuntimeError("Control loop missed its 500 ms deadline")
        if now-self.started > self.duration + 90:
            raise RuntimeError("Verification hold lease expired")
        if max(abs(measured[k] - self.last_target[k]) for k in JOINTS) > 8:
            raise RuntimeError("Follower is not tracking the commanded pose")
        if self.horizon is not None and now-self.started > self.horizon+5 and not self.complete:
            raise RuntimeError("Historical trajectory checkpoint did not settle within five seconds")
        t = min(self.times[-1], elapsed)
        index = min(len(self.frames)-2, max(0, bisect.bisect_right(self.times, t)-1))
        a, b = self.frames[index:index+2]
        fraction = (t-self.times[index]) / (self.times[index+1]-self.times[index])
        desired = {k: a["pose"][k] + fraction*(b["pose"][k]-a["pose"][k]) for k in JOINTS}
        # Bound each physical command even after a delayed tick or start offset.
        limit = min(MAX_COMMAND_STEP, self.speed * dt)
        target = {k: self.last_target[k] + max(-limit, min(limit, desired[k]-self.last_target[k])) for k in JOINTS}
        # A tolerated initial endpoint overshoot may only return inward, with the
        # same per-command cap. Never clamp feedback and create a sudden jump.
        self.complete = elapsed >= self.duration and max(abs(measured[k]-b["pose"][k]) for k in JOINTS) <= 2
        if elapsed > self.duration + 5 and not self.complete:
            raise RuntimeError("Final pose did not settle within five seconds")
        self.last_tick, self.last_target = now, target
        return target
