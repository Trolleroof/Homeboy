"""Narrow Dial Context MCP bridge to Homeboy: status, planning, and running/stopping phone-planned tasks."""
import json
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import secrets
import urllib.error
import urllib.request
import uuid

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from dial_sms import insights_text

ROOT = Path(__file__).resolve().parents[2]

def load_local():
    path = ROOT / "bridge" / ".env.local"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

load_local()
ALLOWED = {x.strip() for x in os.environ.get("DIAL_VOICE_ALLOWED_CALLERS", "").split(",") if x.strip()}
NUMBER = os.environ.get("DIAL_NUMBER", "+12182315815")
BRIDGE = os.environ.get("WILLOW_BRIDGE_URL", "http://127.0.0.1:8621")
CALLER_RE = re.compile(r"\+[1-9]\d{7,14}\Z")
if not ALLOWED or any(not CALLER_RE.fullmatch(n) for n in ALLOWED):
    raise SystemExit("Set DIAL_VOICE_ALLOWED_CALLERS to your E.164 phone number before starting the voice MCP.")
if not CALLER_RE.fullmatch(NUMBER) or NUMBER in ALLOWED:
    raise SystemExit("DIAL_NUMBER must be Homeboy's E.164 Dial number and must differ from allowed callers.")
# Phone execution is on unless explicitly disabled; the harness still enforces
# approved skills, the vision scene check and bounded motion.
EXECUTION = os.environ.get("DIAL_VOICE_EXECUTION", "on").lower() not in ("0", "off", "false", "no")
MCP_SECRET = os.environ.get("DIAL_MCP_SECRET", "")
if len(MCP_SECRET) < 40:
    raise SystemExit("Set a random DIAL_MCP_SECRET of at least 40 characters.")
if not BRIDGE.startswith(("http://127.0.0.1:", "http://localhost:")):
    raise SystemExit("Homeboy bridge must remain on loopback.")

app = FastMCP("Homeboy robot harness", instructions=(
    "You are Homeboy's phone interface. Only help callers whose live Dial caller-number header is allowlisted. "
    "Call willow_status before describing robot or harness readiness. For block placement requests, "
    "call willow_plan_task, then poll willow_task_progress every few seconds until planning finishes (it usually "
    "takes 20-60 seconds; tell the caller you're working on it). When a plan is ready, read back what the robot will "
    "do and ask the caller to confirm the workcell is set up like the demonstration and the arm's path is clear. "
    "Only after an explicit yes, call willow_run_plan with that plan_id and workcell_clear=true. Then poll "
    "willow_task_progress and report the verified verdict; never claim success before the harness reports it. "
    "If the caller says stop, wait, cancel or anything urgent while the robot moves, call willow_stop immediately. "
    "Explain when no approved skill exists, when the bridge is offline, or when hardware is unavailable. "
    "When the caller asks about past runs, stats, success rate, failures, errors, logs, telemetry or why something "
    "failed, call willow_run_history with their question instead of planning a task; it reads Keewano history only "
    "and never involves the robot. Read the spoken_summary back briefly and offer details from the runs."
), streamable_http_path="/mcp", json_response=True, stateless_http=True, port=8623,
    transport_security=TransportSecuritySettings(
        allowed_hosts=["127.0.0.1:8623", "localhost:8623", "d67f-12-91-134-18.ngrok-free.app"],
        allowed_origins=[]))

def caller(ctx: Context) -> str:
    headers = {k.lower(): v for k, v in (ctx.request_context.request.headers.items() if ctx.request_context.request else [])}
    supplied = headers.get("authorization", "")
    if not supplied.startswith("Bearer ") or not secrets.compare_digest(supplied[7:], MCP_SECRET):
        raise ValueError("Voice tool authorization failed.")
    direction = headers.get("x-dial-direction", "")
    who = headers.get("x-dial-user-number", "")
    agent = headers.get("x-dial-agent-number", "")
    if direction != "inbound" or agent != NUMBER or who not in ALLOWED:
        raise ValueError("This voice tool is available only to the authorized inbound caller.")
    return who

def bridge(action, payload=None, timeout=8):
    request = urllib.request.Request(BRIDGE+"/harness/"+action,
        data=json.dumps(payload or {}).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try: reason = json.load(exc).get("error", "Homeboy refused the request")
        except Exception: reason = "Homeboy refused the request"
        return {"ok": False, "error": reason}
    except Exception as exc:
        return {"ok": False, "error": "Homeboy bridge unavailable ("+type(exc).__name__+")"}

def audit(name, caller_number, **details):
    path = ROOT/"datasets"/"dial-voice"/"tools.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as out:
        os.chmod(path, 0o600)
        out.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "event": name, "caller_hash": __import__("hashlib").sha256(caller_number.encode()).hexdigest(), **details})+"\n")

@app.tool()
def willow_status(ctx: Context) -> dict:
    """Read Homeboy's current harness, approved taught skills, bridge and motor freshness. Does not move the robot."""
    number = caller(ctx)
    result = bridge("status")
    audit("status_read", number, bridge_online=result.get("ok") is True)
    return {"bridge_online": result.get("ok") is True,
            "robot_motion_active": (result.get("motion") or {}).get("active", False),
            "active_model": result.get("model"), "model_ready": result.get("model_ready"),
            "vision_ready": result.get("vision_ready"), "motors_fresh": (result.get("motors") or {}).get("age_s", 999) < .5,
            "approved_skills": [{"description": s["description"], "approved": s["approved"]} for s in result.get("skills", [])],
            "plan_pending": bool(result.get("plan")),
            "phone_execution_enabled": EXECUTION}

@app.tool()
def willow_plan_task(task: str, ctx: Context) -> dict:
    """Ask Homeboy to plan a taught task, without starting motion. Requires a supported color block placement and an approved skill."""
    number = caller(ctx)
    if not isinstance(task, str) or not 1 <= len(task.strip()) <= 300:
        raise ValueError("Describe one task in 1 to 300 characters.")
    request_id = uuid.uuid4().hex
    result = bridge("plan", {"prompt": task, "source": "dial_voice", "request_id": request_id})
    audit("task_plan_requested", number, request_id=request_id, accepted=result.get("ok") is True)
    if not result.get("ok"):
        return {"accepted": False, "error": result.get("error", "Homeboy refused the plan."),
                "robot_moved": False}
    return {"accepted": True, "request_id": request_id, "planning_run_id": result.get("run_id"),
            "robot_moved": False, "next_step": "Tell the caller Homeboy is planning, then poll willow_task_progress until a plan is ready."}

@app.tool()
def willow_task_progress(ctx: Context) -> dict:
    """Read the current planning or execution job: state, latest events, the ready plan to confirm, and the verified result. Does not move the robot."""
    caller(ctx)
    result = bridge("status")
    if not result.get("ok"):
        return {"bridge_online": False, "error": result.get("error", "Homeboy bridge unavailable")}
    run = result.get("run") or {}
    plan = result.get("plan") or None
    events = [{k: v for k, v in e.items() if k in ("event", "reason", "error", "verdict", "evidence", "duration_s", "speed")}
              for e in run.get("events", []) if e.get("event") != "codex_started"][-5:]
    out = {"bridge_online": True, "job_active": bool(result.get("active")), "job_kind": run.get("kind"),
           "state": run.get("state"), "finished": bool(run) and not result.get("active"), "result": run.get("result"),
           "verification": {k: run["verification"].get(k) for k in ("verdict", "confidence", "evidence")} if run.get("verification") else None,
           "recent_events": events, "robot_motion_active": (result.get("motion") or {}).get("active", False),
           "phone_execution_enabled": EXECUTION}
    if plan:
        out["plan_ready"] = {"plan_id": plan["id"], "task": plan.get("description"), "duration_s": plan.get("duration"),
                             "speed": plan.get("speed"), "reason": plan.get("reason"),
                             "from_this_phone_line": plan.get("source") == "dial_voice",
                             "expires_in_s": max(0, round(300-(datetime.now(timezone.utc).timestamp()-plan.get("at", 0))))}
    return out

@app.tool()
def willow_run_plan(plan_id: str, workcell_clear: bool, ctx: Context) -> dict:
    """Run the ready phone-planned task on the real robot. Only call after the caller explicitly confirmed the plan and
    that the workcell matches the demonstration with a clear arm path. The harness still checks the scene by camera,
    replays only the approved bounded trajectory, and verifies the outcome visually."""
    number = caller(ctx)
    if not EXECUTION:
        return {"started": False, "robot_moved": False, "error": "Phone execution is disabled on this Homeboy; run the plan from the local frontend."}
    if workcell_clear is not True:
        return {"started": False, "robot_moved": False, "error": "Ask the caller to confirm the workcell is set up and the path is clear first."}
    if not isinstance(plan_id, str) or not re.fullmatch(r"[a-f0-9]{32}", plan_id):
        raise ValueError("Use the plan_id from willow_task_progress.")
    plan = bridge("status").get("plan") or {}
    if plan.get("id") != plan_id or plan.get("source") != "dial_voice":
        audit("task_run_requested", number, plan_id=plan_id, accepted=False)
        return {"started": False, "robot_moved": False, "error": "That plan is not the current phone plan; it may have expired or been replaced. Plan again."}
    result = bridge("run", {"plan_id": plan_id, "scene_confirmed": True})
    audit("task_run_requested", number, plan_id=plan_id, request_id=plan.get("request_id"), accepted=result.get("ok") is True)
    if not result.get("ok"):
        return {"started": False, "robot_moved": False, "error": result.get("error", "Homeboy refused to run the plan.")}
    return {"started": True, "execution_run_id": result.get("run_id"),
            "next_step": "Tell the caller Homeboy is checking the scene and will move shortly. Poll willow_task_progress until finished and report the verified verdict. Call willow_stop if the caller asks."}

@app.tool()
def willow_stop(ctx: Context) -> dict:
    """Emergency stop: cancel any planning or execution job and halt the robot immediately."""
    number = caller(ctx)
    result = bridge("stop", {})
    audit("stop_requested", number, ok=result.get("ok", True) is not False)
    return {"stop_sent": result.get("ok", True) is not False, "error": result.get("error")}

@app.tool()
def willow_run_history(question: str, ctx: Context) -> dict:
    """Answer questions about Homeboy's past runs from Keewano: recent outcomes, success rate, replay speed,
    tracking error, motor temperature, failure reasons and lifecycle logs, for the taught skill whose
    demonstration video best matches the question. Read-only; never plans or moves the robot."""
    number = caller(ctx)
    if not isinstance(question, str) or len(question) > 300:
        raise ValueError("Ask one question of at most 300 characters.")
    result = bridge("insights", {"query": question.strip()}, timeout=30)
    audit("run_history_read", number, ok=result.get("ok") is True)
    if not result.get("ok"):
        return {"available": False, "error": result.get("error", "Run history unavailable."), "robot_moved": False}
    # Drop raw entity ids; the caller needs outcomes, telemetry and logs.
    skills = [{"description": s["description"], "video_score": s.get("video_score"),
               **{k: v for k, v in (s.get("keewano") or {}).items() if k in ("available", "reason", "summary", "speed_policy", "failure_modes")},
               "recent_runs": [{k: v for k, v in r.items() if k not in ("entity", "harness_run_id")} for r in (s.get("keewano") or {}).get("recent_runs", [])]}
              for s in result.get("skills", [])]
    return {"available": True, "spoken_summary": insights_text(result, limit=900), "skills": skills,
            "project": result.get("project"), "query_matched_video": result.get("query_matched_video"), "robot_moved": False}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app.streamable_http_app(), host="127.0.0.1", port=int(os.environ.get("DIAL_VOICE_MCP_PORT", "8623")), log_level="warning")
