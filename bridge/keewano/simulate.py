#!/usr/bin/env python3
"""Backfill two weeks of robot-harness run traces, then optionally stream live runs.

    python3 bridge/keewano/simulate.py --runs 300 --days 14
    python3 bridge/keewano/simulate.py --live --interval 20

Planted patterns (so questions have real answers):
  * pick_cup fails ~45% when RetrievalTopScore < 65, ~8% otherwise.
  * sms-triggered runs get ApprovalGranted(false) 3x more often than dashboard runs.
  * The motion_program tool times out often until 4 days ago (the "fix"), then rarely.
  * pick_cup ran at 3.0 units/s until 4 days ago with high tracking error and more misses;
    the speed policy then slowed it to 2.0 and its success rate rose.
Skill names are fictional ("sim" motion hash), so this never steers the real harness.
"""

import argparse
import random
import time
import uuid

import keewano

SKILLS = ["pick_cup", "place_cup", "wave", "open_drawer", "stack_block"]
INTENTS = {"pick_cup": "fetch_object", "place_cup": "tidy", "wave": "greet",
           "open_drawer": "fetch_object", "stack_block": "tidy"}
TRIGGERS = ["dashboard"] * 6 + ["sms"] * 3 + ["claude_code"]


def simulate_run(start_ts, fix_ts, rng):
    t = start_ts
    ev = []

    def add(name, value=None, dt=1):
        nonlocal t
        t += dt
        ev.append(keewano.event(name, value, timestamp=t))

    skill = rng.choice(SKILLS)
    trigger = rng.choice(TRIGGERS)
    learned = start_ts >= fix_ts and skill == "pick_cup"
    speed = 2.0 if learned else 3.0
    add("RunStarted", "execution", 0)
    add("AgentKind", "robot_harness", 0)
    add("RequestSource", trigger, 0)
    add("ReplaySpeedTenths", int(speed * 10), 0)
    add("SpeedPolicy", "slowed_by_history" if learned else "default", 0)
    add("PromptIntent", INTENTS[skill])

    # Retrieval of the taught demonstration
    score = int(max(30, min(95, rng.gauss(72, 12))))
    add("RetrievalQueried", skill)
    add("ToolCalled", "video_search")
    add("StepLatencyMs", rng.randint(180, 900))
    add("ToolSucceeded", "video_search")
    add("RetrievalTopScore", score)
    matched = score >= 60
    add("RetrievalMatched", matched)
    if not matched:
        add("SafetyStop", "no_confident_match")
        add("BlockedReason", "no_confident_match")
        add("MotionVerdict", "blocked")
        add("RunDurationSec", int(t - start_ts))
        add("RunCompleted", False)
        return ev

    add("SkillSelected", skill)
    add("MotionHash", "sim-" + skill)
    add("LlmCost", rng.randint(1, 6))
    add("TokensUsed", rng.randint(800, 4000))

    # Operator approval
    add("ApprovalRequested", skill)
    deny_p = 0.18 if trigger == "sms" else 0.06
    granted = rng.random() > deny_p
    add("ApprovalGranted", granted, rng.randint(5, 90))
    if not granted:
        add("BlockedReason", "approval_denied")
        add("MotionVerdict", "blocked")
        add("RunDurationSec", int(t - start_ts))
        add("RunCompleted", False)
        return ev

    # Motion program
    timeout_p = 0.22 if start_ts < fix_ts else 0.03
    add("ToolCalled", "motion_program")
    if rng.random() < timeout_p:
        add("StepLatencyMs", 30000, 30)
        add("ErrorClass", "timeout")
        add("ToolFailed", "motion_program")
        add("BlockedReason", "deadline")
        add("MotionVerdict", "blocked")
        add("RunDurationSec", int(t - start_ts))
        add("RunCompleted", False)
        return ev
    add("StepLatencyMs", rng.randint(2500, 9000), 6)
    add("ToolSucceeded", "motion_program")

    fail_p = 0.08
    if skill == "pick_cup" and score < 65:
        fail_p = 0.45
    if skill == "pick_cup" and speed == 3.0:
        fail_p += 0.2
    tracking = rng.gauss(6.6 if speed == 3.0 and skill == "pick_cup" else 4.0, 0.8)
    add("MaxTrackingTenths", max(5, int(tracking * 10)))
    add("MaxMotorTempC", rng.randint(36, 52))
    ok = rng.random() > fail_p
    add("MotionExecuted", skill)
    add("MotionSucceeded", ok, 2)
    add("VerdictConfidencePct", rng.randint(90, 99))
    add("MotionVerdict", "success" if ok else "failure")
    if not ok:
        add("SafetyStop", "grasp_miss" if "pick" in skill else "collision_guard")
    add("RunDurationSec", int(t - start_ts))
    add("RunCompleted", ok)
    return ev


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--runs", type=int, default=300)
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--live", action="store_true", help="stream a new run every --interval seconds")
    p.add_argument("--interval", type=float, default=20)
    p.add_argument("--seed", type=int, default=11)
    args = p.parse_args()

    if not keewano.api_key():
        raise SystemExit("Set KEEWANO_API_KEY (bridge/.env.local) first.")
    print("register:", keewano.register(force=True))

    rng = random.Random(args.seed)
    now = int(time.time())
    fix_ts = now - 4 * 86400

    if not args.live:
        starts = sorted(now - rng.randint(600, args.days * 86400) for _ in range(args.runs))
        accepted = 0
        for i, start in enumerate(starts, 1):
            # Fixed ids per (seed, run): a rerun or retry is deduplicated server-side.
            run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"homeboy-sim:{args.seed}:{i}"))
            events = simulate_run(start, fix_ts, rng)
            for attempt in range(4):
                try:
                    res = keewano.send(run_id, events, strict=True, session_id=run_id)
                    break
                except OSError:
                    if attempt == 3:
                        raise
                    time.sleep(2 * (attempt + 1))
            accepted += (res or {}).get("accepted", 0)
            if i % 50 == 0:
                print(f"{i}/{len(starts)} runs, {accepted} events accepted")
        print(f"done: {len(starts)} runs, {accepted} events accepted. Queryable in ~5 min.")
        return

    while True:
        run_id = str(uuid.uuid4())
        events = simulate_run(int(time.time()) - 120, fix_ts, rng)  # keep timestamps in the past
        res = keewano.send(run_id, events, strict=True)
        print(time.strftime("%H:%M:%S"), run_id[:8], f"{len(events)} events ->", res)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
