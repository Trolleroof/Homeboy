"""Keewano run history -> harness execution policy.

Each harness request is one Keewano entity. Lifecycle, speed, verdict and
tracking events are exported after motor shutdown. Before planning, the
harness reads back past outcomes for the same approved motion and picks the
replay speed. History can only slow a skill down or hold it for re-teaching;
it never exceeds MAX_SPEED, invents angles or bypasses any existing gate.
"""
import os
import re
import sys
import threading
from pathlib import Path

from motion_program import MAX_SPEED

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "keewano"))
import keewano  # noqa: E402

# Fastest first. Every rung is at or below the controller's own limit.
SPEED_LADDER = tuple(s for s in (3.0, 2.0, 1.5, 1.0) if s <= MAX_SPEED)
MIN_RUNS = 3
PROVEN_RATE = .8
BAD_RATE = .6
TRACKING_WARN = 6.0  # controller stops at 8 units; slow down before reaching it
SKIP_EVENTS = {"protocol_event", "model_response", "codex_usage", "tool_returned"}
SAFE_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def blocked_reason(text):
    text = (text or "").lower()
    for label, needles in (("scene_mismatch", ("scene",)), ("camera", ("camera", "frame")),
                           ("stale_motors", ("stale", "fresh six-motor", "no fresh")),
                           ("temperature", ("temperature", "overheat")), ("tracking", ("tracking", "diverg")),
                           ("deadline", ("deadline", "timed out", "timeout")), ("operator_stop", ("stopped", "cancel", "stop")),
                           ("expired", ("expired",)), ("estop", ("emergency",)), ("policy_hold", ("history",))):
        if any(n in text for n in needles):
            return label
    return "other"


def trace_events(run, plan=None):
    """Pure mapping of one harness run to ordered Keewano events."""
    plan = plan or run.get("execution_plan") or {}
    ev = keewano.event
    first = run["events"][0]["at"] if run.get("events") else None
    out = [ev("RunStarted", run.get("kind", "harness"), first), ev("AgentKind", "robot_harness", first),
           ev("RequestSource", plan.get("source") or ("dial_sms" if run.get("request_id") else "dashboard"), first)]
    if plan.get("skill_id"):
        out += [ev("SkillSelected", plan["skill_id"], first), ev("MotionHash", plan["sha256"][:16], first)]
    if plan.get("speed"):
        out.append(ev("ReplaySpeedTenths", round(plan["speed"]*10), first))
    if plan.get("policy"):
        out.append(ev("SpeedPolicy", plan["policy"]["policy"], first))
        if plan["policy"].get("success_rate") is not None:
            out.append(ev("HistorySuccessPct", round(plan["policy"]["success_rate"]*100), first))
    for e in run.get("events", []):
        name = e["event"]
        if name in SKIP_EVENTS:
            continue
        at = e["at"]
        if name == "tool_called":
            out.append(ev("ToolCalled", e.get("tool"), at))
        elif name == "tool_failed":
            out += [ev("ErrorClass", blocked_reason(e.get("error")), at), ev("ToolFailed", e.get("tool"), at)]
        elif name == "plan_ready":
            score = (e.get("retrieval") or {}).get("score")
            if isinstance(score, (int, float)):
                out.append(ev("RetrievalTopScore", max(0, round(score*100)), at))
            out.append(ev("HarnessEvent", name, at))
        elif name == "blocked":
            out += [ev("BlockedReason", blocked_reason(e.get("error") or e.get("reason")), at), ev("HarnessEvent", name, at)]
        else:
            out.append(ev("HarnessEvent", name, at))
    last = run["events"][-1]["at"] if run.get("events") else None
    if run.get("max_tracking") is not None:
        out.append(ev("MaxTrackingTenths", round(run["max_tracking"]*10), last))
    temps = [t for t in ((run.get("initial_state") or {}).get("temperatures") or {}).values() if isinstance(t, (int, float))]
    if temps:
        out.append(ev("MaxMotorTempC", round(max(temps)), last))
    confidence = (run.get("verification") or {}).get("confidence")
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        out.append(ev("VerdictConfidencePct", round(confidence*100), last))
    if run.get("kind") == "execution" and run.get("result"):
        out.append(ev("MotionVerdict", run["result"], last))
    if run.get("kind") == "execution":
        out.append(ev("RunCompleted", run.get("result") == "success", last))
    return out


def export_run(run, entity, plan=None):
    """Fire-and-forget after motor shutdown; never raises into the harness."""
    if not keewano.api_key():
        return None
    try:
        events = trace_events(run, plan)
    except Exception:
        return None
    thread = threading.Thread(target=keewano.send, args=(entity, events), daemon=True)
    thread.start()
    return thread


def _query(sql):
    import pg8000.native
    connection = pg8000.native.Connection(
        user=os.environ["KEEWANO_EMAIL"], password=os.environ["KEEWANO_PAT"],
        host="pgsql.keewano.com", port=27543, database=os.environ["KEEWANO_PROJECT_ID"],
        ssl_context=True, timeout=8)
    try:
        return connection.run(sql)
    finally:
        connection.close()


def fetch_history(skill_id, motion_hash):
    """Outcomes per replay speed for this exact approved motion, from Keewano."""
    hash16 = motion_hash[:16]
    if not SAFE_ID.fullmatch(skill_id) or not SAFE_ID.fullmatch(hash16):
        raise ValueError("Unsafe identifier for analytics query")
    scope = f"\"SkillSelected\" = '{skill_id}' AND \"MotionHash\" = '{hash16}'"
    stats = {}
    for speed, verdict, runs in _query(
            f"SELECT \"ReplaySpeedTenths\" AS speed, \"MotionVerdict\" AS verdict, count(DISTINCT user_id) AS runs "
            f"FROM events WHERE {scope} AND \"MotionVerdict\" IS NOT NULL GROUP BY speed, verdict"):
        if speed is None:
            continue
        stats.setdefault(speed/10, {"success": 0, "failure": 0, "uncertain": 0, "blocked": 0})[verdict] = runs
    for speed, tracking in _query(
            f"SELECT \"ReplaySpeedTenths\" AS speed, avg(\"MaxTrackingTenths\") AS tracking "
            f"FROM events WHERE {scope} AND \"MaxTrackingTenths\" IS NOT NULL GROUP BY speed"):
        if speed is not None and speed/10 in stats and tracking is not None:
            stats[speed/10]["avg_max_tracking"] = round(float(tracking)/10, 2)
    return stats


def speed_policy(stats):
    """Pick the fastest ladder speed whose history is not bad; hold if all are bad.

    Only success/failure verdicts count toward the rate: uncertain camera checks
    and blocked preflights say nothing about how well the motion performs.
    """
    summary = {}
    for speed in SPEED_LADDER:
        s = stats.get(speed, {})
        decided = s.get("success", 0) + s.get("failure", 0)
        rate = s.get("success", 0)/decided if decided else None
        status = "unknown"
        if decided >= MIN_RUNS:
            status = "proven" if rate >= PROVEN_RATE else "bad" if rate < BAD_RATE else "mixed"
        if status in ("proven", "mixed") and s.get("avg_max_tracking", 0) >= TRACKING_WARN:
            status = "bad"  # succeeding, but too close to the controller's tracking stop
        summary[speed] = {"decided_runs": decided, "success_rate": rate, "status": status, **s}
    if not any(v["decided_runs"] for v in summary.values()):
        return {"speed": SPEED_LADDER[0], "policy": "no_history", "hold": False, "success_rate": None, "ladder": summary,
                "reason": "No recorded outcomes for this motion yet; using the default speed."}
    for speed in SPEED_LADDER:
        row = summary[speed]
        if row["status"] == "bad":
            continue
        policy = "default" if speed == SPEED_LADDER[0] else "slowed_by_history"
        reason = (f"{speed:g} units/s: {row['decided_runs']} decided runs, "
                  + (f"{row['success_rate']:.0%} success." if row["success_rate"] is not None else "untested at this speed."))
        slower = [f"{s:g} ({summary[s]['decided_runs']} runs, {summary[s]['success_rate']:.0%})" for s in SPEED_LADDER
                  if s > speed and summary[s]["status"] == "bad"]
        if slower:
            reason += " Faster speeds performed poorly: " + ", ".join(slower) + "."
        return {"speed": speed, "policy": policy, "hold": False, "success_rate": row["success_rate"], "ladder": summary, "reason": reason}
    return {"speed": SPEED_LADDER[-1], "policy": "hold", "hold": True, "success_rate": None, "ladder": summary,
            "reason": "Run history shows this motion failing at every allowed speed. Re-teach or inspect the workcell before replaying it."}


def configured():
    return all(os.environ.get(k) for k in ("KEEWANO_PROJECT_ID", "KEEWANO_EMAIL", "KEEWANO_PAT"))


def recommend(skill_id, motion_hash):
    """Policy for planning. An unavailable Keewano keeps today's behaviour."""
    if not configured():
        return {"speed": SPEED_LADDER[0], "policy": "keewano_unconfigured", "hold": False, "success_rate": None,
                "reason": "Keewano query credentials are not set; using the default speed."}
    try:
        return speed_policy(fetch_history(skill_id, motion_hash))
    except Exception as error:
        return {"speed": SPEED_LADDER[0], "policy": "keewano_unavailable", "hold": False, "success_rate": None,
                "reason": f"Keewano history unavailable ({type(error).__name__}); using the default speed."}
