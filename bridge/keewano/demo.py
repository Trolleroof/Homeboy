#!/usr/bin/env python3
"""Show, live from Keewano, that run history steers the robot harness.

    python3 bridge/keewano/demo.py               # project rollup + speed policy per skill
    python3 bridge/keewano/demo.py --ask "which skill fails the most?"   # what the phone would reply

Every number is queried from Keewano's SQL endpoint at run time; nothing is cached
or hard-coded. Simulator backfill (MotionHash "sim-*") is labelled as such. Needs
KEEWANO_PROJECT_ID, KEEWANO_EMAIL and KEEWANO_PAT in bridge/.env.local and pg8000.
"""
import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def load_env():
    env = ROOT / ".env.local"
    if env.exists():
        for line in env.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() and not key.startswith("#"):
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def ask(question, bridge):
    """Same read-only call SMS and voice make; prints the text the caller gets back."""
    from dial_sms import insights_text
    request = urllib.request.Request(bridge + "/harness/insights", data=json.dumps({"query": question}).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=40) as response:
        print(insights_text(json.load(response)))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ask", help="question to send to the bridge's /harness/insights, as SMS/voice would")
    parser.add_argument("--bridge", default=os.environ.get("HOMEBOY_BRIDGE", "http://127.0.0.1:8621"))
    args = parser.parse_args()
    load_env()
    if args.ask:
        return ask(args.ask, args.bridge)

    import run_analytics
    if not run_analytics.configured():
        sys.exit("Set KEEWANO_PROJECT_ID, KEEWANO_EMAIL and KEEWANO_PAT in bridge/.env.local")
    o = run_analytics.project_overview()
    if not o["available"]:
        sys.exit(o["reason"])
    real = o["total_runs"] - o["simulated_runs"]
    print(f"Keewano project {os.environ['KEEWANO_PROJECT_ID'][:8]}…: {o['total_runs']} traced runs "
          f"({real} real, {o['simulated_runs']} simulator backfill)")
    print("  by agent: " + ", ".join(f"{a} {n}" for a, n in o["runs_by_agent"].items()))
    print("  top block reasons: " + ", ".join(f"{r} {n}" for r, n in list(o["blocked_reasons"].items())[:4]))
    if o["claude_code_failed_tools"]:
        print("  Claude Code tool failures: " + ", ".join(f"{t} {n}" for t, n in o["claude_code_failed_tools"].items()))

    # Speed policy per motion, read back exactly as the planner does before every plan.
    motions = run_analytics._query('SELECT "SkillSelected" AS skill, "MotionHash" AS motion, count(DISTINCT user_id) AS runs '
                                   'FROM events WHERE "MotionVerdict" IS NOT NULL AND "SkillSelected" IS NOT NULL '
                                   'GROUP BY skill, motion')
    print("\nSpeed policy the planner would apply now (ladder " + " > ".join(f"{s:g}" for s in run_analytics.SPEED_LADDER) + " u/s):")
    for skill, motion, runs in sorted(motions, key=lambda r: (str(r[1]).startswith("sim-"), r[0])):
        if not run_analytics.SAFE_ID.fullmatch(skill or "") or not run_analytics.SAFE_ID.fullmatch((motion or "")[:16]):
            continue
        p = run_analytics.recommend(skill, motion)
        tag = "sim " if motion.startswith("sim-") else "real"
        print(f"  [{tag}] {skill:<12} motion {motion[:16]:<16} {runs:>3} runs -> {p['speed']:g} u/s "
              f"{p['policy']}{' HOLD' if p['hold'] else ''}: {p['reason']}")


if __name__ == "__main__":
    main()
