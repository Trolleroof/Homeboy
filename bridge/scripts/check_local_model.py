"""Real local-model smoke check. Never connects to hardware or approves a live skill."""
import base64
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace

import task_harness
from task_harness import TaskHarness, outcome


def main():
    evidence = {"model": task_harness.MODEL, "physical_motion": False, "checks": []}
    actual_store = task_harness.STORE
    with tempfile.TemporaryDirectory() as temporary:
        task_harness.STORE = Path(temporary)
        harness = TaskHarness(SimpleNamespace(_autonomy=None))
        imported = harness.import_episode({"episode": 2, "description": "Pick up the orange block and place it back down on the table"})
        harness.command("approve", {"skill_id": imported["id"], "reviewed": True})
        cases = [("Pick up the orange block and put it back down on the table", True),
                 ("Wash a coffee mug in the sink", False),
                 ("Pick up the blue block and put it down", False),
                 ("Put the orange block into the bin", False)]
        # Planner now uses Codex. This utility stays strictly local: test the
        # deterministic contract gate and the unchanged vision verifier only.
        contract = task_harness.task_contract(harness.catalog()[0]["description"])
        for prompt, expected in cases:
            matched = task_harness.task_contract(prompt) == contract
            check = {"prompt": prompt, "expected_match": expected, "matched": matched, "passed": matched == expected}
            evidence["checks"].append(check)
            print(json.dumps(check), flush=True)
        reference = base64.b64decode(harness.catalog()[0]["reference"])
        schema = {"type": "object", "properties": {"verdict": {"type": "string", "enum": ["success", "failure", "uncertain"]},
                  "confidence": {"type": "number"}, "evidence": {"type": "string"}}, "required": ["verdict", "confidence", "evidence"]}
        started = time.monotonic()
        result, counts = task_harness.local_model(
            "Task: pick up the orange block and place it inside the bin, released from the gripper. "
            "These are identical before and after frames with no execution. Report success only if the block is clearly "
            "inside the bin and released. If the destination or released block is occluded or outside the frame, verdict=uncertain. "
            "Give concrete visible evidence, never assume success.", schema, [reference, reference])
        verdict = outcome(result)
        check = {"name": "identical_frames_do_not_prove_placement", "verdict": verdict, "response": result,
                 "elapsed_s": round(time.monotonic()-started, 2), **counts, "passed": verdict != "success"}
        evidence["checks"].append(check)
        print(json.dumps(check), flush=True)
    task_harness.STORE = actual_store
    evidence["passed"] = all(check["passed"] for check in evidence["checks"])
    actual_store.mkdir(parents=True, exist_ok=True)
    (actual_store/"model-check.json").write_text(json.dumps(evidence, indent=2))
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
