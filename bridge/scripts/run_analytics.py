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
from contextlib import contextmanager
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
ENTITY_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
RECENT_RUNS = 8
LOGGED_RUNS = 3
LOG_COLUMNS = ("HarnessEvent", "ToolCalled", "ToolFailed", "ErrorClass", "BlockedReason", "SafetyStop")


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
    if run.get("id"):
        out.append(ev("HarnessRunId", run["id"], first))
    video = (plan.get("retrieval") or {}).get("video_sha256")
    if isinstance(video, str) and video:
        out.append(ev("VideoSampleHash", video[:16], first))
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
    if first is not None and last is not None:
        out.append(ev("RunDurationSec", max(0, round(last-first)), last))
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


_local = threading.local()


def _connect():
    import pg8000.native
    return pg8000.native.Connection(
        user=os.environ["KEEWANO_EMAIL"], password=os.environ["KEEWANO_PAT"],
        host="pgsql.keewano.com", port=27543, database=os.environ["KEEWANO_PROJECT_ID"],
        ssl_context=True, timeout=8)


@contextmanager
def session():
    """Share one connection across the queries of a single context read."""
    connection = _connect()
    _local.connection = connection
    try:
        yield
    finally:
        _local.connection = None
        connection.close()


def _query(sql):
    # The Keewano SQL endpoint allows at most 16 selected columns (~17 event
    # columns across SELECT and WHERE), no SELECT * and no text aggregates. Rows carry each entity's latest value per event.
    shared = getattr(_local, "connection", None)
    if shared is not None:
        return shared.run(sql)
    connection = _connect()
    try:
        return connection.run(sql)
    finally:
        connection.close()


def _scope(skill_id, motion_hash):
    hash16 = motion_hash[:16]
    if not SAFE_ID.fullmatch(skill_id) or not SAFE_ID.fullmatch(hash16):
        raise ValueError("Unsafe identifier for analytics query")
    return f"\"SkillSelected\" = '{skill_id}' AND \"MotionHash\" = '{hash16}'"


def fetch_history(skill_id, motion_hash):
    """Outcomes per replay speed for this exact approved motion, from Keewano."""
    scope = _scope(skill_id, motion_hash)
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


RUN_COLUMNS = ("user_id", "event_datetime", "HarnessRunId", "VideoSampleHash", "RequestSource", "ReplaySpeedTenths",
               "SpeedPolicy", "RetrievalTopScore", "MotionVerdict", "VerdictConfidencePct", "MaxTrackingTenths",
               "MaxMotorTempC", "BlockedReason", "ErrorClass", "ToolFailed", "RunDurationSec")


def _column(name):
    return name if name in ("user_id", "event_datetime") else f'"{name}"'


def _when(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def fetch_recent_runs(skill_id, motion_hash, limit=RECENT_RUNS):
    """Latest finished executions of this motion, one row per run with its telemetry."""
    scope = _scope(skill_id, motion_hash)
    rows = _query(f"SELECT {', '.join(map(_column, RUN_COLUMNS))} FROM events WHERE {scope} "
                  f"AND \"MotionVerdict\" IS NOT NULL "
                  f"ORDER BY event_datetime DESC LIMIT {int(limit)*4}")
    runs, seen = [], set()
    for row in rows:
        r = dict(zip(RUN_COLUMNS, row))
        key = r["HarnessRunId"] or r["user_id"]
        if key in seen:
            continue  # several final rows share a timestamp per run
        seen.add(key)
        tenths = lambda v: None if v is None else round(v/10, 1)
        pct = lambda v: None if v is None else round(v/100, 2)
        runs.append({"entity": r["user_id"], "harness_run_id": r["HarnessRunId"], "at": _when(r["event_datetime"]),
                     "video_sha16": r["VideoSampleHash"], "source": r["RequestSource"], "verdict": r["MotionVerdict"],
                     "replay_speed": tenths(r["ReplaySpeedTenths"]), "speed_policy": r["SpeedPolicy"],
                     "retrieval_score": pct(r["RetrievalTopScore"]),
                     "verdict_confidence": pct(r["VerdictConfidencePct"]),
                     "max_tracking": tenths(r["MaxTrackingTenths"]), "max_motor_temp_c": r["MaxMotorTempC"],
                     "duration_s": r["RunDurationSec"], "blocked_reason": r["BlockedReason"],
                     "last_error_class": r["ErrorClass"], "last_failed_tool": r["ToolFailed"]})
        if len(runs) >= limit:
            break
    return runs


def fetch_failure_modes(skill_id, motion_hash):
    """How many distinct runs of this motion hit each blocked reason / error class."""
    scope = _scope(skill_id, motion_hash)
    modes = {}
    for column in ("BlockedReason", "ErrorClass"):
        rows = _query(f"SELECT \"{column}\" AS value, count(DISTINCT user_id) AS runs FROM events "
                      f"WHERE {scope} AND \"{column}\" IS NOT NULL GROUP BY value")
        modes[column] = dict(sorted(((v, n) for v, n in rows if v), key=lambda x: -x[1]))
    return {"blocked_reasons": modes["BlockedReason"], "error_classes": modes["ErrorClass"]}


def fetch_run_logs(entities):
    """Ordered lifecycle log per run. Each distinct value is logged once, at the
    first row it appears in, so carried-forward columns don't repeat."""
    entities = [e for e in entities if ENTITY_ID.fullmatch(e or "")]
    if not entities:
        return {}
    ids = ", ".join(f"'{e}'" for e in entities)
    rows = _query(f"SELECT user_id, event_datetime, {', '.join(map(_column, LOG_COLUMNS))} FROM events "
                  f"WHERE user_id IN ({ids}) ORDER BY event_datetime LIMIT 600")
    logs, seen = {e: [] for e in entities}, set()
    for user, at, *values in rows:
        for column, value in zip(LOG_COLUMNS, values):
            if value is None or (user, column, value) in seen:
                continue
            seen.add((user, column, value))
            logs[user].append({"at": _when(at), "event": column, "value": value})
    return {e: lines[-40:] for e, lines in logs.items()}


def summarize(runs, video_sha256=None):
    """Recent-window performance; separates runs of the currently indexed video."""
    video16 = (video_sha256 or "")[:16] or None
    decided = [r for r in runs if r["verdict"] in ("success", "failure")]
    tracking = [r["max_tracking"] for r in runs if r["max_tracking"] is not None]
    temps = [r["max_motor_temp_c"] for r in runs if r["max_motor_temp_c"] is not None]
    same = [r for r in decided if video16 and r["video_sha16"] == video16]
    return {"runs": len(runs), "decided_runs": len(decided),
            "success_rate": round(sum(r["verdict"] == "success" for r in decided)/len(decided), 2) if decided else None,
            "same_video_decided_runs": len(same),
            "same_video_success_rate": round(sum(r["verdict"] == "success" for r in same)/len(same), 2) if same else None,
            "avg_max_tracking": round(sum(tracking)/len(tracking), 2) if tracking else None,
            "peak_max_tracking": max(tracking) if tracking else None, "tracking_stop_at": 8.0, "tracking_warn_at": TRACKING_WARN,
            "peak_motor_temp_c": max(temps) if temps else None,
            "last_verdicts": [r["verdict"] for r in runs]}


def run_context(skill_id, motion_hash, video_sha256=None, with_policy=False):
    """Recent Keewano runs, telemetry, failure modes and lifecycle logs for one
    retrieved demonstration video's exact motion. Read-only and never raises."""
    base = {"skill_id": skill_id, "motion_sha16": motion_hash[:16],
            "video_sha16": (video_sha256 or "")[:16] or None}
    if not configured():
        return {**base, "available": False, "reason": "Keewano query credentials are not set."}
    try:
        with session():
            runs = fetch_recent_runs(skill_id, motion_hash)
            modes = fetch_failure_modes(skill_id, motion_hash)
            # Logs for the newest non-successful runs first; they explain what to watch for.
            focus = [r for r in runs if r["verdict"] != "success"][:LOGGED_RUNS] or runs[:1]
            logs = fetch_run_logs([r["entity"] for r in focus])
            policy = speed_policy(fetch_history(skill_id, motion_hash)) if with_policy else None
    except Exception as error:
        return {**base, "available": False, "reason": f"Keewano history unavailable ({type(error).__name__})."}
    video16 = base["video_sha16"]
    for r in runs:
        r["same_video"] = None if not (video16 and r["video_sha16"]) else r["video_sha16"] == video16
        if r["entity"] in logs:
            r["log"] = logs[r["entity"]]
    result = {**base, "available": True, "summary": summarize(runs, video_sha256), "recent_runs": runs, "failure_modes": modes}
    if policy:
        result["speed_policy"] = {k: policy[k] for k in ("speed", "policy", "hold", "success_rate", "reason")}
    return result
