"""Dial SMS -> existing Homeboy harness, with local operator execution.

Run with .sms-venv/bin/python bridge/scripts/dial_sms.py. The event stream is
presence-based; this sidecar is for the local hackathon demo, not a durable
public webhook. Tokens and event IDs persist across listener restarts.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[2]
ACTIVE = ("planning", "awaiting_confirmation", "confirmed", "executing")
PHONE = re.compile(r"\+[1-9]\d{7,14}\Z")
SEND_TIMEOUT = 15
INSIGHTS_TIMEOUT = 30
# Questions about past runs. They are answered from Keewano history and never
# reach the planner, so the robot is never asked to do anything to answer them.
INSIGHT_WORDS = re.compile(
    r"\b(stats?|statistics|history|logs?|telemetry|keewano|analytics|metrics|performance|perform(?:ing|ed)?|"
    r"success(?:es| rate)?|fail(?:s|ed|ing|ures?)?|errors?|issues?|problems?|recent runs?|last runs?|report|"
    r"how (?:is|are|did|has|have|was|were|does|do|often|many|well)|how's|why (?:did|does|is|was)|what happened)\b", re.I)


def wants_insights(body):
    return bool(INSIGHT_WORDS.search(body or ""))


def insights_text(result, limit=1400):
    """Plain-text Keewano summary for SMS or speech."""
    lines = ["Homeboy run history from Keewano. Read-only; no motion requested."]
    skills = result.get("skills") or []
    if not skills:
        lines.append("No approved taught skills yet.")
    for item in skills:
        k = item.get("keewano") or {}
        score = item.get("video_score")
        lines.append(f"{item.get('description') or item.get('skill_id')}" + (f" (video match {score:.2f})" if isinstance(score, (int, float)) else "") + ":")
        if not k.get("available"):
            lines.append("  History unavailable: " + str(k.get("reason", "Keewano not reachable")))
            continue
        summary, runs = k.get("summary") or {}, k.get("recent_runs") or []
        if not runs:
            lines.append("  No finished runs recorded yet.")
        else:
            counts = {}
            for verdict in summary.get("last_verdicts", []):
                counts[verdict] = counts.get(verdict, 0) + 1
            rate = summary.get("success_rate")
            lines.append(f"  Last {len(runs)} runs: " + ", ".join(f"{n} {v}" for v, n in counts.items())
                         + (f" ({rate:.0%} success)" if rate is not None else "") + ".")
            if summary.get("same_video_decided_runs"):
                lines.append(f"  This video: {summary['same_video_success_rate']:.0%} of {summary['same_video_decided_runs']} decided runs.")
            if summary.get("avg_max_tracking") is not None:
                lines.append(f"  Tracking error avg {summary['avg_max_tracking']}, peak {summary['peak_max_tracking']} (stop at {summary.get('tracking_stop_at', 8)}).")
            if summary.get("peak_motor_temp_c") is not None:
                lines.append(f"  Peak motor temp {summary['peak_motor_temp_c']}C.")
        policy = k.get("speed_policy")
        if policy:
            lines.append(f"  Next replay speed {policy['speed']:g} ({policy['policy']}).")
        modes = k.get("failure_modes") or {}
        top = list((modes.get("blocked_reasons") or {}).items())[:2] + list((modes.get("error_classes") or {}).items())[:2]
        if top:
            lines.append("  Top issues: " + ", ".join(f"{name} x{n}" for name, n in top) + ".")
        failed = next((r for r in runs if r.get("verdict") != "success" and r.get("log")), None)
        if failed:
            trail = " > ".join(str(l["value"]) for l in failed["log"][-5:])
            lines.append(f"  Last {failed['verdict']} run ({str(failed.get('at', ''))[:16]}): {trail}.")
    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit-3] + "..."


def validate_configuration(number, allowed, result_url=""):
    if not PHONE.fullmatch(number) or not allowed or any(not PHONE.fullmatch(x) for x in allowed):
        raise ValueError("Configure the Dial number and allowed senders in E.164 format")
    if number in allowed:
        raise ValueError("The receiving Dial number cannot also be an allowed sender")
    if result_url:
        u = urllib.parse.urlsplit(result_url)
        if (u.scheme != "https" or not u.hostname or u.hostname in ("localhost", "127.0.0.1", "::1")
                or u.username or u.password or u.query or u.fragment):
            raise ValueError("Result URL must be a verified HTTPS page, without credentials or a query")


def load_environment(path):
    """Read a private KEY=value file without shell interpolation."""
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                raise ValueError("Invalid environment file entry")
            os.environ.setdefault(key, value.strip().strip("\"'"))


def api_key():
    if os.environ.get("DIAL_API_KEY"):
        return os.environ["DIAL_API_KEY"]
    directory = Path(os.environ.get("XDG_DATA_HOME", str(Path.home()/".local/share")))/"dial"
    for name in ("auth.v1.json", "auth.json"):
        auth = directory/name
        if auth.exists():
            return json.loads(auth.read_text()).get("apiKey", "")
    return ""


class Bridge:
    def __init__(self, url):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost")
                or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("Homeboy bridge must use a loopback HTTP origin")
        self.url = url.rstrip("/")

    async def call(self, action="status", **payload):
        return await asyncio.to_thread(self._call, action, payload)

    def _call(self, action, payload):
        if action not in ("status", "plan", "confirm_sms", "insights"):
            raise ValueError("SMS adapter cannot execute robot motion")
        request = urllib.request.Request(self.url+"/harness/"+action,
                    data=None if action == "status" else json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"})
        try:
            # Insights query Keewano for up to three skills; still bounded, never motion.
            with urllib.request.urlopen(request, timeout=INSIGHTS_TIMEOUT if action == "insights" else 5) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            try:
                message = json.load(error).get("error", "Homeboy refused the request")
            except (ValueError, AttributeError):
                message = "Homeboy refused the request"
            raise ValueError(message) from None


class State:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = (directory/"listener.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise
        self.db = sqlite3.connect(directory/"sms.sqlite3")
        self.db.row_factory = sqlite3.Row
        os.chmod(directory/"sms.sqlite3", 0o600)
        self.trace = directory/"events.jsonl"
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS received(event_id TEXT PRIMARY KEY, message_id TEXT UNIQUE);
            CREATE TABLE IF NOT EXISTS requests(
                id TEXT PRIMARY KEY, sender TEXT NOT NULL, status TEXT NOT NULL,
                created REAL NOT NULL, token TEXT UNIQUE, expires REAL,
                plan_id TEXT, motion_hash TEXT, planning_run_id TEXT, execution_run_id TEXT);
            CREATE TABLE IF NOT EXISTS replies(id TEXT PRIMARY KEY, status TEXT NOT NULL);
        """)
        self.db.commit()

    def close(self):
        self.db.close()
        self.lock.close()

    def claim(self, event_id, message_id):
        try:
            with self.db:
                self.db.execute("INSERT INTO received VALUES (?,?)", (event_id, message_id))
            return True
        except sqlite3.IntegrityError:
            return False

    def update(self, request_id, **fields):
        allowed = {"status", "token", "expires", "plan_id", "motion_hash", "planning_run_id", "execution_run_id"}
        if not fields or not set(fields) <= allowed:
            raise ValueError("Invalid request fields")
        with self.db:
            self.db.execute("UPDATE requests SET "+", ".join(k+"=?" for k in fields)+" WHERE id=?",
                            (*fields.values(), request_id))

    def active(self):
        return self.db.execute("SELECT * FROM requests WHERE status IN (?,?,?,?)", ACTIVE).fetchall()

    def event(self, request_id, name, **details):
        # Raw phone numbers and message bodies are never written to this trace.
        with self.trace.open("a") as stream:
            os.chmod(self.trace, 0o600)
            stream.write(json.dumps({"request_id": request_id, "event": name,
                                    "at": datetime.now(timezone.utc).isoformat(), "details": details})+"\n")


class Adapter:
    def __init__(self, state, bridge, send, number, allowed, result_url="", clock=time.time):
        validate_configuration(number, allowed, result_url)
        self.state, self.bridge, self.send = state, bridge, send
        self.number, self.allowed, self.result_url, self.clock = number, set(allowed), result_url, clock
        self.lock = asyncio.Lock()

    async def reply(self, reply_id, sender, body, request_id=None):
        # Reserve before network I/O. A timeout may still have sent the message:
        # never retry it automatically, including after a process restart.
        try:
            with self.state.db:
                self.state.db.execute("INSERT INTO replies VALUES (?,?)", (reply_id, "attempting"))
        except sqlite3.IntegrityError:
            return
        try:
            await asyncio.wait_for(self.send(sender, body[:1500]), timeout=SEND_TIMEOUT)
            status = "submitted"
        except Exception as error:
            status = "unknown"
            self.state.event(request_id, "sms_reply_unknown", error_class=type(error).__name__)
        with self.state.db:
            self.state.db.execute("UPDATE replies SET status=? WHERE id=?", (status, reply_id))
        self.state.event(request_id, "sms_reply_"+status, reply_id=reply_id)

    async def handle(self, event):
        async with self.lock:
            if not isinstance(event, dict) or event.get("type") != "message.received":
                return
            data = event.get("data")
            if not isinstance(data, dict):
                return
            sender, body = data.get("from"), data.get("body")
            event_id, message_id = event.get("id"), data.get("messageId")
            if (sender not in self.allowed or data.get("to") != self.number or data.get("channel") != "sms"
                    or data.get("source") not in ("external", "internal") or not isinstance(body, str)
                    or not isinstance(event_id, str) or not event_id or not isinstance(message_id, str) or not message_id):
                return
            if not self.state.claim(event_id, message_id):
                return
            try:
                created = datetime.fromisoformat(event["createdAt"].replace("Z", "+00:00"))
                if created.tzinfo is None or not -30 <= self.clock()-created.timestamp() <= 300:
                    raise ValueError("Stale event")
            except (KeyError, ValueError, TypeError, AttributeError):
                self.state.event(None, "sms_rejected", reason="invalid_or_stale_timestamp", dial_event_id=event_id)
                return
            body = body.strip()
            if body.upper().startswith("RUN"):
                await self.confirm(event_id, sender, body)
                return
            if body.upper() == "HELP":
                await self.reply(event_id, sender, "Text a taught task, e.g. 'Put the orange block in the bin'. Homeboy replies with a RUN token. A local operator must check the workcell before motion. "
                                 "Ask 'how is the orange block task doing?' or 'show logs' for run history and stats.")
                return
            if not 1 <= len(body) <= 500:
                await self.reply(event_id, sender, "Please send one taught task in 1–500 characters.")
                return
            if wants_insights(body):
                await self.insights(event_id, sender, body)
                return
            if self.state.active():
                await self.reply(event_id, sender, "Homeboy already has a pending request. Confirm it or wait for it to finish/expire.")
                return
            request_id = uuid.uuid4().hex
            with self.state.db:
                self.state.db.execute("INSERT INTO requests(id,sender,status,created) VALUES (?,?,?,?)",
                                      (request_id, sender, "planning", self.clock()))
            self.state.event(request_id, "request_received", dial_event_id=event_id, message_id=message_id,
                             sender_hash=hashlib.sha256(sender.encode()).hexdigest(), source=data["source"])
            try:
                result = await self.bridge.call("plan", prompt=body, source="dial_sms", request_id=request_id)
                if not result.get("ok") or not result.get("run_id"):
                    raise ValueError("Planner did not accept the request")
                self.state.update(request_id, planning_run_id=result["run_id"])
                self.state.event(request_id, "planning_started", run_id=result["run_id"])
            except ValueError as error:
                self.state.update(request_id, status="done")
                self.state.event(request_id, "request_refused", reason=str(error)[:256])
                await self.reply(request_id+":refused", sender, "Homeboy cannot plan this request: "+str(error), request_id)
            except Exception as error:
                # Ambiguous HTTP failures remain pending and are reconciled by
                # request_id on poll. Never repeat a planning command.
                self.state.event(request_id, "planning_acceptance_unknown", error_class=type(error).__name__)
                await self.reply(request_id+":unknown", sender, "Homeboy's planner connection is unavailable. No motion was requested. Checking the request; please wait.", request_id)

    async def insights(self, event_id, sender, body):
        """Answer a stats/logs question from Keewano. Allowed while a request is
        pending: it creates no request, plan or run."""
        self.state.event(None, "insights_requested", dial_event_id=event_id)
        try:
            result = await asyncio.wait_for(self.bridge.call("insights", query=body), timeout=INSIGHTS_TIMEOUT+5)
            text = insights_text(result)
        except Exception as error:
            self.state.event(None, "insights_unavailable", dial_event_id=event_id, error_class=type(error).__name__)
            text = "Homeboy's run history is unavailable right now. No motion was requested."
        await self.reply(event_id+":insights", sender, text)

    async def confirm(self, event_id, sender, body):
        match = re.fullmatch(r"RUN\s+([A-F0-9]{8})", body.upper())
        row = self.state.db.execute("SELECT * FROM requests WHERE token=? AND sender=?",
                                    (match[1] if match else "", sender)).fetchone()
        if (not row or row["status"] != "awaiting_confirmation" or row["expires"] <= self.clock()):
            await self.reply(event_id, sender, "That RUN token is invalid, expired or already used. Text the task again after the pending request ends.")
            return
        try:
            result = await self.bridge.call("confirm_sms", plan_id=row["plan_id"], request_id=row["id"])
            if not result.get("ok"):
                raise ValueError("Confirmation refused")
        except Exception as error:
            self.state.event(row["id"], "confirmation_unknown", error_class=type(error).__name__)
            await self.reply(event_id, sender, "Could not confirm this plan. No motion was requested. Check Homeboy locally.", row["id"])
            return
        self.state.update(row["id"], status="confirmed")
        self.state.event(row["id"], "confirmation_received", plan_id=row["plan_id"], dial_event_id=event_id)
        await self.reply(row["id"]+":confirmed", sender,
                         "Confirmed. Waiting for the local operator to check the original workcell and press Run on robot. This text does not start motion.", row["id"])

    async def finish(self, row, message, event="request_finished", **details):
        self.state.update(row["id"], status="done")
        self.state.event(row["id"], event, **details)
        await self.reply(row["id"]+":finished", row["sender"], message, row["id"])

    async def poll(self):
        async with self.lock:
            rows = self.state.active()
            if not rows:
                return
            try:
                status = await self.bridge.call()
            except Exception:
                # Still expire pending consent during a bridge outage.
                for row in rows:
                    if row["status"] != "executing" and self.clock() > (row["expires"] or row["created"]+150):
                        await self.finish(row, "Homeboy's request expired while the bridge was unavailable. Check Homeboy locally before sending another request.", "request_expired")
                return
            plan, run = status.get("plan") or {}, status.get("run") or {}
            for row in rows:
                same_run = run.get("request_id") == row["id"]
                if same_run and run.get("kind") == "execution":
                    if row["status"] != "executing":
                        self.state.update(row["id"], status="executing", execution_run_id=run["id"])
                        self.state.event(row["id"], "execution_started", run_id=run["id"], plan_id=run.get("plan_id"))
                    if not status.get("active"):
                        verdict = run.get("result") or "unknown"
                        message = {"success": "Camera reports task success; operator verification is still required.",
                                   "failure": "Camera reports task failure. Homeboy will not retry automatically.",
                                   "uncertain": "Task outcome is uncertain. Ask the operator to inspect the block.",
                                   "blocked": "Homeboy stopped or refused execution. Ask the operator to inspect the evidence."}.get(verdict, "Execution outcome is unavailable. Check Homeboy locally; do not assume completion.")
                        message += " Run: "+run["id"]+"."
                        if self.result_url:
                            message += " "+self.result_url+"?"+urllib.parse.urlencode({"run_id": run["id"], "request_id": row["id"]})
                        await self.finish(row, message, "execution_finished", run_id=run["id"], result=verdict,
                                          evidence_file=run.get("evidence_file"), operator_confirmed=False)
                    continue
                if row["status"] == "executing":
                    await self.finish(row, "Homeboy's execution trace is no longer available in the live bridge. Check the saved evidence locally; outcome unknown.", "execution_trace_lost")
                    continue
                if row["status"] == "planning":
                    if same_run and not status.get("active") and plan.get("request_id") == row["id"]:
                        expires = min(plan["at"]+300, self.clock()+300)
                        if expires <= self.clock():
                            await self.finish(row, "The plan expired. Text the task again.", "request_expired")
                            continue
                        token = secrets.token_hex(4).upper()
                        self.state.update(row["id"], status="awaiting_confirmation", token=token, expires=expires,
                                          plan_id=plan["id"], motion_hash=plan["sha256"])
                        self.state.event(row["id"], "skill_selected", skill_id=plan["skill_id"], plan_id=plan["id"], motion_hash=plan["sha256"])
                        await self.reply(row["id"]+":plan", row["sender"],
                            f"Found taught task: {plan['description']}. Reply RUN {token} within {max(1, int(expires-self.clock()))} seconds. A local operator must also check the workcell.", row["id"])
                    elif same_run and not status.get("active"):
                        await self.finish(row, "No executable taught skill matched, or planning was blocked. Teach and approve this exact task, then try again.", "no_match")
                    elif self.clock()-row["created"] > 150:
                        await self.finish(row, "Planning timed out or was replaced. No motion was requested. Check Homeboy locally.", "planning_timeout")
                    continue
                if (plan.get("request_id") != row["id"] or plan.get("id") != row["plan_id"]
                        or plan.get("sha256") != row["motion_hash"]):
                    await self.finish(row, "The pending plan was replaced or the bridge restarted. Text the task again.", "plan_replaced")
                elif self.clock() >= row["expires"]:
                    await self.finish(row, "The confirmation window expired. Text the task again; Homeboy did not start motion from SMS.", "request_expired")
                elif row["status"] == "awaiting_confirmation" and plan.get("sms_confirmed") is True:
                    # Recover after an ambiguous confirmation HTTP response or
                    # a crash between bridge acceptance and the SQLite update.
                    self.state.update(row["id"], status="confirmed")
                    self.state.event(row["id"], "confirmation_reconciled", plan_id=plan["id"])


async def main(args):
    load_environment(ROOT/"bridge/.env.local")
    key = api_key()
    if not key:
        raise ValueError("No Dial login. Run: dial auth login YOUR_EMAIL --coupon DIAL_FOR_SPEEDHACKS")
    if key.startswith("kwpat-"):
        raise ValueError("DIAL_API_KEY contains a Keewano token. Use the Dial key from Dial dashboard Settings.")
    from dial_sdk import DialClient, DialConfig, SendMessageParams
    async with DialClient(DialConfig(api_key=key, user_agent="homeboy-sms/0.1")) as dial:
        numbers = await dial.list_numbers()
        configured = os.environ.get("DIAL_NUMBER_ID", "")
        configured_number = os.environ.get("DIAL_NUMBER", "")
        candidates = [n for n in numbers if (not configured or n.id == configured)
                      and (not configured_number or n.number == configured_number)
                      and "sms" in n.capabilities and n.setup_status == "ready"
                      and not n.replaced_by]
        if len(candidates) != 1:
            raise ValueError("Set DIAL_NUMBER_ID to one ready SMS number on this account")
        number = candidates[0]
        bridge = Bridge(os.environ.get("WILLOW_BRIDGE_URL", "http://127.0.0.1:8621"))
        allowed = {x.strip() for x in os.environ.get("WILLOW_SMS_ALLOWED_SENDERS", "").split(",") if x.strip()}
        result_url = os.environ.get("WILLOW_SMS_RESULT_URL", "")
        validate_configuration(number.number, allowed, result_url)
        if args.check:
            status = await bridge.call()
            approved = sum(bool(s.get("approved")) for s in status.get("skills", []))
            print(json.dumps({"dial_authenticated": True, "number_id": number.id, "number": number.number,
                              "bridge_reachable": True, "allowed_senders_configured": True,
                              "approved_skills": approved, "harness_active": status.get("active"),
                              "planning_ready": approved > 0 and status.get("active") is False,
                              "event_stream_verified": False,
                              "live_sms_verified": False}, indent=2))
            return
        state = State(ROOT/"datasets/dial-sms")
        try:
            async def send(to, body):
                await dial.send_message(SendMessageParams(to=to, from_number_id=number.id, body=body, channel="sms"))
            adapter = Adapter(state, bridge, send, number.number, allowed, result_url)
            async def poll():
                while True:
                    await adapter.poll()
                    await asyncio.sleep(1)
            async def listen():
                while True:
                    try:
                        async with dial.new_events_connection() as events:
                            print("Dial SMS listener connected; operator execution remains local.", flush=True)
                            async for event in events:
                                await adapter.handle(event)
                    except Exception as error:
                        state.event(None, "listener_disconnected", error_class=type(error).__name__)
                        print("Dial stream disconnected; reconnecting. Gaps over 2 minutes can miss SMS.", flush=True)
                        await asyncio.sleep(5)
            async with asyncio.TaskGroup() as group:
                group.create_task(poll())
                group.create_task(listen())
        finally:
            state.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Read-only account/number/bridge check; sends no SMS or motion")
    try:
        asyncio.run(main(parser.parse_args()))
    except KeyboardInterrupt:
        pass
    except ValueError as error:
        raise SystemExit(str(error)) from None
