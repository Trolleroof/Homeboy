"""Narrow Dial Context MCP bridge to Homeboy: call status and task planning only."""
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
MCP_SECRET = os.environ.get("DIAL_MCP_SECRET", "")
if len(MCP_SECRET) < 40:
    raise SystemExit("Set a random DIAL_MCP_SECRET of at least 40 characters.")
if not BRIDGE.startswith(("http://127.0.0.1:", "http://localhost:")):
    raise SystemExit("Homeboy bridge must remain on loopback.")

app = FastMCP("Homeboy robot harness", instructions=(
    "You are Homeboy's phone interface. Only help callers whose live Dial caller-number header is allowlisted. "
    "Call willow_status before describing robot or harness readiness. For orange block placement requests, "
    "use willow_plan_task, which only plans a taught task and never moves the robot. Explain when no approved "
    "skill exists, when the bridge is offline, or when hardware is unavailable. Never claim execution occurred. "
    "A human must approve the exact plan and start any motion in Homeboy's local frontend."
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

def bridge(action, payload=None):
    request = urllib.request.Request(BRIDGE+"/harness/"+action,
        data=json.dumps(payload or {}).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
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
            "execution_requires_local_operator": True}

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
            "robot_moved": False, "next_step": "Tell the caller a plan is being prepared in Homeboy's frontend. A local operator must review the plan, workcell and live robot status before choosing whether to run it."}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app.streamable_http_app(), host="127.0.0.1", port=int(os.environ.get("DIAL_VOICE_MCP_PORT", "8623")), log_level="warning")
