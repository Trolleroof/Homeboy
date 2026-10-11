"""Minimal Keewano REST client for Homeboy agent traces (stdlib only).

Entity model: one entity per agent run (a robot task run or a Claude Code
session). Every event is Name(value); context events (RunTrigger, Skill...)
go first so later events carry them without joins.
"""

import hashlib
import json
import os
import time
import uuid
import zlib
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API = "https://api.keewano.com/event/ingress/v1/json"
ROOT = Path(__file__).resolve().parents[2]

# One schema for Python (REST) and Node (SDK): keewano.events.json at the repo
# root. Ids are 2500 + position, as @keewano/codegen assigns them, so append
# only and never reorder. Regenerate the Node helpers after editing:
#   cd frontend && npx keewano-codegen --target node --input ../keewano.events.json --code lib --json
DEFINITIONS = ROOT / "keewano.events.json"
GENERATED = ROOT / "frontend" / "lib" / "keewano-events.json"
CUSTOM_EVENTS = [{"eventTypeId": 2500 + i, "name": e["eventName"], "dataType": e["eventValueType"]}
                 for i, e in enumerate(json.loads(DEFINITIONS.read_text())["events"])]
EVENTS_BY_NAME = {e["name"]: e for e in CUSTOM_EVENTS}


def _events_hash():
    """Use the codegen schema version so both SDKs report under one hash."""
    try:
        generated = json.loads(GENERATED.read_text())
        if generated.get("eventCount") == len(CUSTOM_EVENTS):
            return generated["version"]
    except (OSError, ValueError, KeyError):
        pass
    return zlib.crc32(json.dumps(CUSTOM_EVENTS, sort_keys=True).encode()) or 1


EVENTS_HASH = _events_hash()


def _load_env_local():
    env_file = ROOT / "bridge" / ".env.local"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_local()


def api_key():
    return os.environ.get("KEEWANO_API_KEY", "")


def entity_id(name):
    """Stable UUID for any string id (uuid5); passes real UUIDs through."""
    try:
        return str(uuid.UUID(str(name)))
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"homeboy:{name}"))


def _post(path, body, timeout=5):
    req = Request(f"{API}/{path}", data=json.dumps(body).encode(),
                  headers={"k-token": api_key(), "Content-Type": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def register(force=False):
    """Register the custom event list once per hash (repeats are a server no-op)."""
    # Per schema AND per key: switching to a new project must register again.
    key_tag = hashlib.sha256(api_key().encode()).hexdigest()[:12]
    marker = Path.home() / ".cache" / "homeboy-keewano" / f"registered-{EVENTS_HASH}-{key_tag}"
    if not force and marker.exists():
        return None
    result = _post("custom", {"customEventsHash": EVENTS_HASH, "eventTypes": CUSTOM_EVENTS})
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    return result


def event(name, value=None, timestamp=None):
    spec = EVENTS_BY_NAME[name]
    if spec["dataType"] == "string" and value is not None:
        value = str(value)[:256]
    return {"eventTypeId": spec["eventTypeId"], "dataType": spec["dataType"],
            "timestamp": int(timestamp if timestamp is not None else time.time()),
            "data": value}


def send(entity, events, strict=False, session_id=None):
    """Send events (in time order) for one entity. Returns the response or None.

    Non-strict mode never raises, so tracing can't break the robot or the agent.
    """
    if not api_key() or not events:
        return None
    try:
        register()
        return _post("in", {
            "dataSessionId": session_id or str(uuid.uuid4()),  # reuse to make a retry idempotent
            "batch": 1,
            "customEventsHash": EVENTS_HASH,
            "installId": entity_id(entity),
            "events": events,
        })
    except (HTTPError, URLError, OSError, ValueError):
        if strict:
            raise
        return None


class Run:
    """Collects one agent run's trace and flushes it to Keewano.

        with Run("robot_harness", trigger="sms", intent="fetch_object") as run:
            run.add("SkillSelected", "pick_cup")
            ...
    """

    def __init__(self, agent_kind, trigger, intent=None, run_id=None):
        self.id = run_id or str(uuid.uuid4())
        self.started = time.time()
        self.events = [event("RunStarted", trigger), event("AgentKind", agent_kind)]
        if intent:
            self.events.append(event("PromptIntent", intent))

    def add(self, name, value=None):
        self.events.append(event(name, value))
        return self

    def tool(self, name, ok, latency_ms=None, error_class=None):
        self.add("ToolCalled", name)
        if latency_ms is not None:
            self.add("StepLatencyMs", int(latency_ms))
        if ok:
            self.add("ToolSucceeded", name)
        else:
            if error_class:
                self.add("ErrorClass", error_class)
            self.add("ToolFailed", name)
        return self

    def finish(self, ok):
        self.add("RunDurationSec", int(time.time() - self.started))
        self.add("RunCompleted", bool(ok))
        result = send(self.id, self.events)
        self.events = []
        return result

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.add("ErrorClass", exc_type.__name__)
        self.finish(exc_type is None)
        return False
