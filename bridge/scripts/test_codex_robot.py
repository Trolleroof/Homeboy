import io
import json
import threading
import unittest
from unittest.mock import patch

from codex_robot import run_codex, tool


class Sink(io.StringIO):
    def __init__(self):
        super().__init__()
        self.messages = []

    def write(self, value):
        self.messages.append(json.loads(value))
        return len(value)


class Process:
    def __init__(self, calls, status="completed"):
        self.stdin = Sink()
        messages = [{"id": 1, "result": {}}, {"id": 2, "result": {"thread": {"id": "thread"}}},
                    {"id": 3, "result": {"turn": {"id": "turn"}}}]
        messages.extend({"id": 100+i, "method": "item/tool/call", "params": {"tool": name, "arguments": args}} for i, (name, args) in enumerate(calls))
        messages.extend([{"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "Checked"}}},
                         {"method": "turn/completed", "params": {"turn": {"status": status}}}])
        self.stdout = io.StringIO("".join(json.dumps(m)+"\n" for m in messages))
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


class CodexProtocolTests(unittest.TestCase):
    def run_client(self, process, callback, cancel=None):
        with patch("codex_robot.subprocess.Popen", return_value=process) as spawn:
            result = run_codex("test", [tool("robot_ping", "Test", {})], callback,
                               cancel or threading.Event(), lambda *args, **kwargs: None)
        self.assertIn("--disable", spawn.call_args.args[0])
        self.assertNotIn("CODEX_APP_TOOLS_PIPE_PATH", spawn.call_args.kwargs["env"])
        return result

    def test_native_tool_request_executes_and_returns_content(self):
        process = Process([("robot_ping", {})])
        calls = []
        self.assertEqual(self.run_client(process, lambda name, args: calls.append(name) or {"ok": True}), "Checked")
        self.assertEqual(calls, ["robot_ping"])
        reply = next(m for m in process.stdin.messages if m.get("id") == 100)
        self.assertTrue(reply["result"]["success"])
        self.assertEqual(json.loads(reply["result"]["contentItems"][0]["text"]), {"ok": True})
        self.assertTrue(process.terminated)

    def test_unregistered_and_malformed_calls_never_dispatch(self):
        process = Process([("shell", {"cmd": "touch unsafe"}), ("robot_ping", "bad")])
        calls = []
        self.run_client(process, lambda *args: calls.append(args))
        self.assertEqual(calls, [])
        replies = [m for m in process.stdin.messages if m.get("id", 0) >= 100]
        self.assertTrue(all(not m["result"]["success"] for m in replies))

    def test_eight_tool_budget_and_failed_turn(self):
        process = Process([("robot_ping", {})]*9)
        calls = []
        with self.assertRaisesRegex(RuntimeError, "eight tool"):
            self.run_client(process, lambda *args: calls.append(args) or {})
        self.assertEqual(len(calls), 8)
        self.assertTrue(process.terminated)
        with self.assertRaisesRegex(RuntimeError, "turn failed"):
            self.run_client(Process([], "failed"), lambda *args: {})

    def test_cancellation_terminates_client_before_dispatch(self):
        process = Process([("robot_ping", {})])
        cancel = threading.Event()
        cancel.set()
        with self.assertRaisesRegex(RuntimeError, "stopped"):
            self.run_client(process, lambda *args: self.fail("Must not dispatch"), cancel)
        self.assertTrue(process.terminated)


if __name__ == "__main__":
    unittest.main()
