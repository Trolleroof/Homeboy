"""Real Codex + existing video-vector DB planning check; no motor hardware.

A temporary copy is approved only to test the planner. The persistent skill's
approval and all demonstrated actions remain unchanged. No execute tool exists.
"""
import copy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

import task_harness
from task_harness import TaskHarness


def main():
    root = Path(__file__).resolve().parents[2]
    source = root/"datasets/harness/skill-2.json"
    skill = json.loads(source.read_text())
    skill["approved"] = True
    recorder = SimpleNamespace(_last_robot_pos={}, _last_robot_pos_at=0, _autonomy=None,
                               autonomy_status=lambda: {"active": False},
                               motor_telemetry=lambda: {"positions": {}, "age_s": None, "hardware_available": False,
                                                         "offline": True, "temperatures": {}, "motion": {"active": False}})
    with tempfile.TemporaryDirectory(prefix="willow-plan-test-") as directory, patch.object(task_harness, "STORE", Path(directory)):
        (Path(directory)/"skill-2.json").write_text(json.dumps(skill))
        harness = TaskHarness(recorder)
        harness.command("plan", {"prompt": "Pick up the orange block and place it down"})
        deadline = time.monotonic()+130
        while harness.active and time.monotonic() < deadline:
            time.sleep(.2)
        if harness.active:
            harness.cancel.set()
            raise RuntimeError("Planner did not complete")
        report = {"physical_execution": False, "temporary_catalog_approval": True,
                  "persistent_skill_approved": json.loads(source.read_text())["approved"],
                  "plan": harness.plan, "run": copy.deepcopy(harness.run)}
        target = root/"datasets/harness/codex-check.json"
        target.write_text(json.dumps(report, indent=2))
        print(json.dumps({"report": str(target), "state": report["run"]["state"], "codex_turns": report["run"]["codex_calls"],
                          "plan_ready": bool(report["plan"]), "tools": [e["tool"] for e in report["run"]["events"] if e["event"] == "tool_called"],
                          "error": next((e.get("error") for e in reversed(report["run"]["events"]) if e.get("error")), None)}, indent=2))
        if not report["plan"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
