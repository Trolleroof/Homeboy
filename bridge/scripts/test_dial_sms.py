"""SMS state-machine checks: no provider traffic, model calls or motor motion."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dial_sms import Adapter, Bridge, State, api_key
import dial_sms

SENDER = "+14155550123"
NUMBER = "+14155550999"


class FakeBridge:
    def __init__(self, now):
        self.now = now
        self.calls = []
        self.status = {"active": False, "plan": None, "run": None}
        self.fail = None

    async def call(self, action="status", **payload):
        self.calls.append((action, payload))
        if self.fail:
            raise self.fail
        if action == "plan":
            if self.status["active"]:
                raise ValueError("A prompt job is already active")
            self.status = {"active": True, "plan": None,
                           "run": {"id": "planning", "request_id": payload["request_id"], "kind": "planning"}}
            return {"ok": True, "run_id": "planning"}
        if action == "insights":
            return {"ok": True, "robot_moved": False, "skills": [{
                "skill_id": "episode-2", "description": "Put the orange block in the bin", "video_score": .78,
                "keewano": {"available": True, "summary": {"last_verdicts": ["failure", "success", "success"], "success_rate": .67,
                            "same_video_decided_runs": 3, "same_video_success_rate": .67, "avg_max_tracking": 5.1,
                            "peak_max_tracking": 7.1, "tracking_stop_at": 8.0, "peak_motor_temp_c": 46},
                            "speed_policy": {"speed": 2.0, "policy": "slowed_by_history"},
                            "failure_modes": {"blocked_reasons": {"tracking": 2}, "error_classes": {"camera": 1}},
                            "recent_runs": [{"verdict": "failure", "at": "2026-10-11T18:33:04",
                                             "log": [{"value": "planning"}, {"value": "tool_failed"}, {"value": "tracking"}]}]}}]}
        if action == "confirm_sms":
            p = self.status["plan"]
            if not p or payload["plan_id"] != p["id"] or payload["request_id"] != p["request_id"]:
                raise ValueError("Plan replaced")
            p["sms_confirmed"] = True
            return {"ok": True}
        return copy.deepcopy(self.status)

    def planned(self):
        self.status["active"] = False
        self.status["plan"] = {"id": "plan-1", "at": self.now, "request_id": self.status["run"]["request_id"],
                               "source": "dial_sms", "sms_confirmed": False, "sha256": "motion-1",
                               "skill_id": "episode-2", "description": "Put the orange block in the bin"}


class SMSTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.state = State(self.directory)
        self.now = 1791738000.
        self.bridge = FakeBridge(self.now)
        self.messages = []

        async def send(to, body):
            self.messages.append((to, body))
        self.send = send
        self.adapter = Adapter(self.state, self.bridge, send, NUMBER, {SENDER}, clock=lambda: self.now)

    async def asyncTearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def event(self, body="Put the orange block in the bin", event_id="event-1", message_id=None, **data):
        return {"id": event_id, "type": "message.received", "version": 1,
                "createdAt": datetime.fromtimestamp(self.now, timezone.utc).isoformat(),
                "data": {"messageId": message_id or event_id, "from": SENDER, "to": NUMBER,
                         "channel": "sms", "source": "external", "body": body, **data}}

    async def pending(self):
        await self.adapter.handle(self.event())
        self.bridge.planned()
        await self.adapter.poll()
        return dict(self.state.active()[0])

    async def test_request_confirm_operator_result_and_duplicate_delivery(self):
        row = await self.pending()
        self.assertIn("RUN "+row["token"], self.messages[-1][1])
        self.assertEqual(row["motion_hash"], "motion-1")
        confirm = self.event("RUN "+row["token"], "confirm")
        await self.adapter.handle(confirm)
        await self.adapter.handle(confirm)
        self.assertEqual(self.state.active()[0]["status"], "confirmed")
        self.assertEqual(sum(c[0] == "confirm_sms" for c in self.bridge.calls), 1)
        self.assertFalse(any(c[0] in ("run", "execute", "stop") for c in self.bridge.calls))
        self.bridge.status = {"active": True, "plan": None,
            "run": {"id": "execution", "request_id": row["id"], "kind": "execution", "plan_id": row["plan_id"]}}
        await self.adapter.poll()
        self.assertEqual(self.state.active()[0]["execution_run_id"], "execution")
        self.bridge.status["active"] = False
        self.bridge.status["run"].update(result="uncertain", evidence_file="execution-evidence.json")
        await self.adapter.poll()
        self.assertIn("uncertain", self.messages[-1][1])
        self.assertEqual(len(self.state.active()), 0)
        trace = self.state.trace.read_text()
        self.assertIn("execution_finished", trace)
        self.assertIn(row["id"], trace)
        self.assertNotIn(SENDER, trace)
        self.assertNotIn("RUN "+row["token"], trace)

    async def test_duplicate_event_and_message_ids_survive_restart(self):
        event = self.event()
        await self.adapter.handle(event)
        await self.adapter.handle(event)
        await self.adapter.handle(self.event(event_id="different-event", message_id="event-1"))
        self.state.close()
        self.state = State(self.directory)
        self.adapter = Adapter(self.state, self.bridge, self.send, NUMBER, {SENDER}, clock=lambda: self.now)
        await self.adapter.handle(event)
        self.assertEqual(sum(c[0] == "plan" for c in self.bridge.calls), 1)

    async def test_wrong_sender_destination_channel_source_and_stale_events_do_nothing(self):
        for data in ({"from": "+14155550444"}, {"to": "+14155550444"}, {"channel": "whatsapp"}, {"source": "unknown"}):
            await self.adapter.handle(self.event(**data))
        stale = self.event()
        stale["createdAt"] = "2020-01-01T00:00:00Z"
        await self.adapter.handle(stale)
        self.assertEqual(self.bridge.calls, [])
        self.assertEqual(self.messages, [])

    async def test_authorized_dial_to_dial_request_is_accepted_without_reply_loop(self):
        await self.adapter.handle(self.event(source="internal"))
        self.assertEqual(sum(c[0] == "plan" for c in self.bridge.calls), 1)
        self.bridge.planned()
        await self.adapter.poll()
        row = dict(self.state.active()[0])
        await self.adapter.handle(self.event("RUN "+row["token"], "internal-confirm", source="internal"))
        self.assertEqual(self.state.active()[0]["status"], "confirmed")
        await self.adapter.handle(self.event("Confirmed", "reply-echo", **{"from": NUMBER, "to": SENDER, "source": "internal"}))
        self.assertEqual(sum(c[0] == "plan" for c in self.bridge.calls), 1)
        self.assertEqual(sum(c[0] == "confirm_sms" for c in self.bridge.calls), 1)

    async def test_wrong_sender_cannot_use_token_and_confirmation_expires(self):
        row = await self.pending()
        self.adapter.allowed.add("+14155550444")
        await self.adapter.handle(self.event("RUN "+row["token"], "wrong-sender", **{"from": "+14155550444"}))
        await self.adapter.handle(self.event("RUN DEADBEEF", "wrong-token"))
        self.now += 301
        await self.adapter.handle(self.event("RUN "+row["token"], "expired"))
        await self.adapter.poll()
        self.assertFalse(any(c[0] == "confirm_sms" for c in self.bridge.calls))
        self.assertEqual(len(self.state.active()), 0)

    async def test_cli_versioned_credentials_and_environment_override(self):
        directory = self.directory/"dial"
        directory.mkdir()
        (directory/"auth.json").write_text(json.dumps({"apiKey": "legacy-fixture"}))
        with patch.dict("os.environ", {"DIAL_API_KEY": "", "XDG_DATA_HOME": str(self.directory)}):
            self.assertEqual(api_key(), "legacy-fixture")
            (directory/"auth.v1.json").write_text(json.dumps({"apiKey": "current-fixture"}))
            self.assertEqual(api_key(), "current-fixture")
            with patch.dict("os.environ", {"DIAL_API_KEY": "override-fixture"}):
                self.assertEqual(api_key(), "override-fixture")

    async def test_one_pending_request_and_replaced_plan(self):
        await self.pending()
        await self.adapter.handle(self.event(event_id="another-request"))
        self.assertEqual(sum(c[0] == "plan" for c in self.bridge.calls), 1)
        self.bridge.status["plan"]["sha256"] = "changed-motion"
        await self.adapter.poll()
        self.assertEqual(len(self.state.active()), 0)
        self.assertIn("replaced", self.messages[-1][1])

    async def test_unsupported_and_blocked_planning_return_no_match(self):
        await self.adapter.handle(self.event("Wash the mug"))
        self.bridge.status["active"] = False
        self.bridge.status["run"]["result"] = "blocked"
        await self.adapter.poll()
        self.assertEqual(len(self.state.active()), 0)
        self.assertIn("No executable taught skill", self.messages[-1][1])

    async def test_ambiguous_reply_is_not_retried_after_restart(self):
        calls = []
        async def timeout(to, body):
            calls.append((to, body))
            raise TimeoutError("may have sent")
        self.adapter.send = timeout
        await self.adapter.reply("reply", SENDER, "hello")
        self.state.close()
        self.state = State(self.directory)
        self.adapter = Adapter(self.state, self.bridge, timeout, NUMBER, {SENDER}, clock=lambda: self.now)
        await self.adapter.reply("reply", SENDER, "hello")
        self.assertEqual(len(calls), 1)

    async def test_hung_send_is_bounded_and_not_retried(self):
        import asyncio
        calls = []
        async def hung_send(to, body):
            calls.append(to)
            await asyncio.Event().wait()
        self.adapter.send = hung_send
        with patch.object(dial_sms, "SEND_TIMEOUT", .01):
            await self.adapter.reply("hung", SENDER, "hello")
            await self.adapter.reply("hung", SENDER, "hello")
        self.assertEqual(calls, [SENDER])
        self.assertEqual(self.state.db.execute("SELECT status FROM replies WHERE id='hung'").fetchone()[0], "unknown")

    async def test_receiving_number_cannot_trigger_its_own_requests(self):
        with self.assertRaisesRegex(ValueError, "cannot also"):
            Adapter(self.state, self.bridge, self.send, NUMBER, {NUMBER})

    async def test_preflight_checks_sdk_number_config_and_real_bridge_status(self):
        from contextlib import redirect_stdout
        import io
        number = SimpleNamespace(id="number-1", number=NUMBER, capabilities=["sms"], setup_status="ready", replaced_by=None)
        class Client:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def list_numbers(self): return [number]
        sdk = SimpleNamespace(DialClient=lambda config: Client(), DialConfig=lambda **kw: kw, SendMessageParams=lambda **kw: kw)
        environment = {"DIAL_API_KEY": "sk_live_fixture", "DIAL_NUMBER_ID": "", "DIAL_NUMBER": NUMBER,
                       "WILLOW_SMS_ALLOWED_SENDERS": SENDER, "WILLOW_SMS_RESULT_URL": ""}
        self.bridge.status.update(skills=[{"approved": True}])
        with patch.dict("sys.modules", {"dial_sdk": sdk}), patch.dict("os.environ", environment), \
             patch.object(dial_sms, "load_environment"), patch.object(dial_sms, "Bridge", return_value=self.bridge):
            output = io.StringIO()
            with redirect_stdout(output):
                await dial_sms.main(SimpleNamespace(check=True))
            result = json.loads(output.getvalue())
            self.assertTrue(result["planning_ready"])
            self.assertFalse(result["live_sms_verified"])
            self.assertEqual(self.bridge.calls, [("status", {})])
            with patch.dict("os.environ", {"WILLOW_SMS_ALLOWED_SENDERS": ""}):
                with self.assertRaises(ValueError):
                    await dial_sms.main(SimpleNamespace(check=True))
            number.replaced_by = {"id": "replacement"}
            with self.assertRaisesRegex(ValueError, "ready SMS"):
                await dial_sms.main(SimpleNamespace(check=True))

    async def test_keewano_token_is_not_sent_to_dial(self):
        with patch.dict("os.environ", {"DIAL_API_KEY": "kwpat-fixture"}), patch.object(dial_sms, "load_environment"):
            with self.assertRaisesRegex(ValueError, "Keewano token"):
                await dial_sms.main(SimpleNamespace(check=True))

    async def test_bridge_timeout_never_repeats_planning(self):
        self.bridge.fail = TimeoutError()
        event = self.event()
        await self.adapter.handle(event)
        await self.adapter.handle(event)
        self.now += 151
        await self.adapter.poll()
        self.assertEqual(sum(c[0] == "plan" for c in self.bridge.calls), 1)
        self.assertEqual(len(self.state.active()), 0)

    async def test_single_listener_and_loopback_only_robot_commands(self):
        with self.assertRaises(BlockingIOError):
            State(self.directory)
        with self.assertRaises(ValueError):
            Bridge("https://robot.example.com")
        with self.assertRaises(ValueError):
            await Bridge("http://127.0.0.1:8621").call("run", plan_id="anything")


    async def test_stats_question_reads_keewano_without_planning(self):
        await self.adapter.handle(self.event("How is the orange block task doing? Show recent logs"))
        self.assertEqual([c[0] for c in self.bridge.calls], ["insights"])
        self.assertEqual(self.bridge.calls[0][1]["query"], "How is the orange block task doing? Show recent logs")
        self.assertEqual(self.state.active(), [])
        body = self.messages[-1][1]
        for expected in ("Keewano", "no motion", "67% success", "peak 7.1", "tracking x2", "planning > tool_failed > tracking", "speed 2"):
            self.assertIn(expected, body)

    async def test_stats_question_is_answered_while_a_task_is_pending(self):
        await self.pending()
        await self.adapter.handle(self.event("why did it fail last time?", event_id="event-2"))
        self.assertEqual(sum(c[0] == "plan" for c in self.bridge.calls), 1)
        self.assertEqual(self.bridge.calls[-1][0], "insights")
        self.assertIn("Keewano", self.messages[-1][1])

    async def test_unavailable_history_is_reported_without_motion(self):
        self.bridge.fail = TimeoutError()
        await self.adapter.handle(self.event("send me the stats"))
        self.assertEqual([c[0] for c in self.bridge.calls], ["insights"])
        self.assertIn("unavailable", self.messages[-1][1])
        self.assertIn("No motion", self.messages[-1][1])

    def test_tasks_are_not_mistaken_for_stats_questions(self):
        for task in ("Put the orange block in the bin", "Please place the red cube on the table", "RUN 123456"):
            self.assertFalse(dial_sms.wants_insights(task), task)
        for question in ("stats", "show logs", "How did the last run go?", "what's the success rate", "why did it fail", "any errors today?"):
            self.assertTrue(dial_sms.wants_insights(question), question)


if __name__ == "__main__":
    unittest.main()
