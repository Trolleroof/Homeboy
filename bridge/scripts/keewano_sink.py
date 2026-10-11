"""Ship arm-run events to Keewano without ever touching robot control.

One stable Keewano user stands for the arm, so every run lands on one ordered
timeline. Each run is one Keewano data session; each lifecycle event is a custom
event whose string value is the run ID, so rows join back to datasets/harness/
runs.jsonl. Delivery runs on a worker thread. A failure drops to a local spool
file and never raises into the caller.
"""
import json
import os
from pathlib import Path
import queue
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
import zlib

ROOT = Path(__file__).resolve().parents[2]
SPOOL = ROOT / "datasets" / "keewano-failed.jsonl"
BASE = "https://api.keewano.com/event/ingress/v1/json"

# Ids are permanent once registered under a hash. Add new events at the end; the
# hash below is derived from this map, so a change registers under a new hash.
STAGES = {"request_received": 2000, "planning": 2001, "plan_ready": 2002, "no_match": 2003,
          "confirmation_received": 2004, "checking_scene": 2005, "executing": 2006,
          "verifying": 2007, "finished": 2008, "blocked": 2009, "skill_learned": 2010,
          "model_response": 2011, "run_evidence_indexed": 2012}
DETAILS = {"arm_run_skill": (2030, "string"), "arm_run_verdict": (2031, "string"),
           "arm_run_duration_ms": (2032, "uint"), "arm_run_reason": (2033, "string")}
EVENT_TYPES = ([{"eventTypeId": i, "name": "arm_run_" + n, "dataType": "string"} for n, i in STAGES.items()] +
               [{"eventTypeId": i, "name": n, "dataType": t} for n, (i, t) in DETAILS.items()])
EVENTS_HASH = zlib.crc32(json.dumps(EVENT_TYPES, sort_keys=True).encode()) or 1
TYPE_OF = {e["eventTypeId"]: e["dataType"] for e in EVENT_TYPES}


def load_environment():
    """Fill missing KEEWANO_* values from the ignored local env files."""
    for path in (ROOT / ".env", ROOT / "bridge" / ".env.local"):
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and re.fullmatch(r"KEEWANO_[A-Z0-9_]*", key):
                os.environ.setdefault(key, value.strip().strip("\"'"))


def arm_user_id(arm_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "homeboy:arm:" + arm_id))


def session_id(run_id):
    """Run IDs are uuid4 hex; anything else gets a stable derived UUID."""
    try:
        return str(uuid.UUID(run_id))
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, "homeboy:run:" + str(run_id)))


def to_keewano(run_id, event):
    """Map one harness event to Keewano event dicts; unknown names are skipped."""
    name = event.get("event")
    if name not in STAGES:
        return []
    at = int(event.get("at") or time.time())
    out = [{"eventTypeId": STAGES[name], "timestamp": at, "data": str(run_id)[:128], "dataType": "string"}]
    extras = []
    if event.get("skill_id"):
        extras.append(("arm_run_skill", str(event["skill_id"])[:128]))
    if name == "finished" and event.get("verdict"):
        extras.append(("arm_run_verdict", str(event["verdict"])[:128]))
    if name == "executing" and isinstance(event.get("duration_s"), (int, float)) and event["duration_s"] >= 0:
        extras.append(("arm_run_duration_ms", int(event["duration_s"] * 1000)))
    reason = event.get("reason") or event.get("error")
    if reason:
        extras.append(("arm_run_reason", str(reason)[:200]))
    for key, value in extras:
        type_id, data_type = DETAILS[key]
        out.append({"eventTypeId": type_id, "timestamp": at, "data": value, "dataType": data_type})
    return out


class KeewanoSink:
    def __init__(self, api_key=None, arm_id=None, opener=urllib.request.urlopen, spool=SPOOL, retries=3, backoff=2.0, load_env=True):
        if load_env:
            load_environment()
        self.api_key = api_key if api_key is not None else os.environ.get("KEEWANO_API_KEY", "")
        self.user_id = arm_user_id(arm_id or os.environ.get("KEEWANO_ARM_ID", "so101-arm-1"))
        self.enabled = bool(self.api_key)
        self.opener, self.spool, self.retries, self.backoff = opener, spool, retries, backoff
        self.queue = queue.Queue(maxsize=1000)
        self.batches = {}
        self.registered = False
        self.stats = {"sent": 0, "failed": 0, "dropped": 0}
        if self.enabled:
            threading.Thread(target=self._worker, name="keewano-sink", daemon=True).start()

    def emit(self, run_id, event):
        """Queue a harness event. Safe to call from the control path."""
        if not self.enabled:
            return
        try:
            events = to_keewano(run_id, event)
            if events:
                self.queue.put_nowait((str(run_id), events))
        except queue.Full:
            self.stats["dropped"] += 1
        except Exception:
            self.stats["dropped"] += 1

    def flush(self, timeout=10.0):
        deadline = time.monotonic() + timeout
        while self.queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.05)
        return not self.queue.unfinished_tasks

    def _post(self, path, body):
        req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "k-token": self.api_key})
        with self.opener(req, timeout=15) as response:
            return response.status, json.loads(response.read() or b"{}")

    def _register(self):
        status, body = self._post("/custom", {"customEventsHash": EVENTS_HASH, "eventTypes": EVENT_TYPES})
        if status == 200 or (body or {}).get("status") == "already_registered":
            self.registered = True
        return self.registered

    def _send(self, run_id, events):
        if not self.registered and not self._register():
            raise RuntimeError("custom events not registered yet")
        batch = self.batches.get(run_id, 0) + 1
        payload = {"installId": self.user_id, "dataSessionId": session_id(run_id), "batch": batch,
                   "customEventsHash": EVENTS_HASH, "sdkVersion": "homeboy/1.0", "events": events}
        if os.environ.get("KEEWANO_TEST_USER"):
            payload["testUserName"] = os.environ["KEEWANO_TEST_USER"]
        status, body = self._post("/in", payload)
        if status != 202 or body.get("accepted") != len(events):
            raise RuntimeError("Keewano accepted %s of %s events" % (body.get("accepted"), len(events)))
        self.batches[run_id] = batch

    def _worker(self):
        while True:
            run_id, events = self.queue.get()
            try:
                for attempt in range(self.retries):
                    try:
                        self._send(run_id, events)
                        self.stats["sent"] += len(events)
                        break
                    except Exception as error:
                        if attempt + 1 == self.retries:
                            self._spool(run_id, events, error)
                        else:
                            time.sleep(self.backoff * (attempt + 1))
            finally:
                self.queue.task_done()

    def _spool(self, run_id, events, error):
        self.stats["failed"] += len(events)
        try:
            self.spool.parent.mkdir(parents=True, exist_ok=True)
            with self.spool.open("a") as stream:
                stream.write(json.dumps({"run_id": run_id, "events": events, "error": type(error).__name__ + ": " + str(error)[:200]}) + "\n")
        except OSError:
            pass
