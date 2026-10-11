#!/usr/bin/env python3
"""Claude Code hook -> Keewano agent-trace events.

Entity = Claude Code session. Wired in .claude/settings.json for
UserPromptSubmit, PreToolUse, PostToolUse, PostToolUseFailure and Stop.
Always exits 0 and sends in a forked child, so it never blocks the agent.
"""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import keewano  # noqa: E402

STATE = Path.home() / ".cache" / "homeboy-keewano" / "sessions"

INTENTS = [  # first match wins; keeps raw prompt text out of Keewano
    ("debug", ("error", "bug", "fix", "broken", "fail", "why")),
    ("test", ("test", "verify", "check")),
    ("robot", ("robot", "arm", "motion", "grasp", "so-101", "harness")),
    ("retrieval", ("search", "embedding", "recall", "video", "index")),
    ("frontend", ("ui", "page", "dashboard", "css", "component")),
    ("setup", ("install", "set up", "setup", "config", "env")),
    ("git", ("commit", "push", "pr ", "merge", "branch")),
]


def classify(prompt):
    text = prompt.lower()
    for label, words in INTENTS:
        if any(w in text for w in words):
            return label
    return "other"


def error_class(response):
    text = json.dumps(response)[:4000].lower() if response is not None else ""
    for label, needle in (("timeout", "timed out"), ("permission", "permission"),
                          ("not_found", "no such file"), ("not_found", "not found"),
                          ("syntax", "syntaxerror"), ("type_error", "typeerror"),
                          ("exit_nonzero", "exit code"), ("denied", "denied")):
        if needle in text:
            return label
    return "unknown"


def build_events(hook):
    name = hook.get("hook_event_name", "")
    session = hook.get("session_id", "unknown")
    state_dir = STATE / session
    state_dir.mkdir(parents=True, exist_ok=True)
    tool = hook.get("tool_name", "")
    tool_use_id = hook.get("tool_use_id", "")
    events = []

    if not (state_dir / "started").exists():
        (state_dir / "started").write_text(str(time.time()))
        events += [keewano.event("RunStarted", "claude_code"),
                   keewano.event("AgentKind", "claude_code")]

    if name == "UserPromptSubmit":
        (state_dir / "turn_tools").write_text("0")
        events.append(keewano.event("PromptIntent", classify(hook.get("prompt", ""))))
    elif name == "PreToolUse":
        if tool_use_id:
            (state_dir / f"t-{tool_use_id}").write_text(str(time.time()))
        count_file = state_dir / "turn_tools"
        count = int(count_file.read_text() or 0) if count_file.exists() else 0
        count_file.write_text(str(count + 1))
        events.append(keewano.event("ToolCalled", tool))
    elif name in ("PostToolUse", "PostToolUseFailure"):
        start_file = state_dir / f"t-{tool_use_id}"
        if tool_use_id and start_file.exists():
            latency = (time.time() - float(start_file.read_text())) * 1000
            start_file.unlink(missing_ok=True)
            events.append(keewano.event("StepLatencyMs", int(latency)))
        response = hook.get("tool_response")
        failed = name == "PostToolUseFailure" or (
            isinstance(response, dict) and (response.get("is_error") or response.get("interrupted")))
        if failed:
            events.append(keewano.event("ErrorClass", error_class(response or hook.get("error"))))
            events.append(keewano.event("ToolFailed", tool))
        else:
            events.append(keewano.event("ToolSucceeded", tool))
    elif name == "Stop":
        count_file = state_dir / "turn_tools"
        count = int(count_file.read_text() or 0) if count_file.exists() else 0
        events.append(keewano.event("TurnEnded", count))
        started = float((state_dir / "started").read_text())
        events.append(keewano.event("RunDurationSec", int(time.time() - started)))

    return session, events


def main():
    try:
        hook = json.load(sys.stdin)
        session, events = build_events(hook)
    except Exception:
        return
    if not events or not keewano.api_key():
        return
    if hasattr(os, "fork") and os.fork() != 0:
        return  # parent returns immediately; child sends
    try:
        keewano.send(session, events)
    finally:
        os._exit(0)


if __name__ == "__main__":
    main()
    sys.exit(0)
