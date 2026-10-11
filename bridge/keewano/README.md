# Keewano agent traces

Homeboy sends small, typed agent-trace events to [Keewano](https://www.keewano.com/) and an agent asks questions about them over MCP. One entity = one agent run (a robot-harness run or a Claude Code session).

## Setup

1. Sign up at keewano.com, create a project, and send `#SpeedHacks` in the bottom-left chat to unlock MCP access.
2. Create an ingest key (Settings → Project → API Keys) and put it in `bridge/.env.local` as `KEEWANO_API_KEY=...`.
3. Create a personal access token (Settings → Personal → Security) and connect MCP:

   ```sh
   claude mcp add --scope user --transport http keewano https://api.keewano.com/mcp --header "k-token: $KEEWANO_PAT"
   ```

4. Backfill history, then stream live runs during the demo:

   ```sh
   python3 bridge/keewano/simulate.py --runs 300 --days 14
   python3 bridge/keewano/simulate.py --live --interval 20
   ```

5. Wait about 5 minutes, then ask: "How many runs and events does my project have, and which event types does it record?"

Claude Code sessions in this repo are traced automatically through `.claude/settings.json` hooks once the key is set. Without a key the hooks do nothing.

## One schema, two senders

`keewano.events.json` at the repo root is the only event list. Ids are 2500 plus position, as `@keewano/codegen` assigns them, so append new events and never reorder.

- **Python** (`bridge/keewano/keewano.py`, REST): the harness, the Claude Code hooks and the simulator.
- **Node** (`@keewano/node-sdk`, `frontend/lib/keewano.ts`): the dashboard's video-search trail in `lib/run-events.ts` (request, top score, selected skill or no-match reason). The raw query text is not sent.

Both send under the codegen schema version. After editing the event list, regenerate:

```sh
cd frontend && npx keewano-codegen --target node --input ../keewano.events.json --code lib --json
```

`KEEWANO_API_KEY` must be an **SDK**-type project key. The `kwpat-` personal token only reads (SQL and MCP). `KEEWANO_PROJECT_ID` is the project UUID, not the org id. The Node SDK reads the key from `bridge/.env.local` when `frontend/.env.local` doesn't set it, and it queues batches in `datasets/keewano-node/`.

## How it feeds the robot harness

`bridge/scripts/run_analytics.py` closes the loop:

1. **Export.** After every plan or execution job, after motor shutdown, the harness sends the run's lifecycle trace to Keewano: skill, motion hash, replay speed, verdict, max tracking error, motor temperature and blocked-reason category. One entity is one request, so planning and execution share a timeline.
2. **Read back.** At planning time the harness queries Keewano over its Postgres endpoint for past verdicts of the same approved motion, grouped by replay speed.
3. **Adapt.** `speed_policy` walks the ladder 3.0 → 2.0 → 1.5 → 1.0 units/s. It picks the fastest speed that is not "bad" (at least 3 decided runs with under 60% success, or average max tracking at or above 6 units). If every speed is bad, it holds the skill for re-teaching. Uncertain and blocked runs don't count.
4. **Explain.** Codex calls `read_run_analytics` for the skill whose demonstration video EmbeddingGemma retrieved. It returns the speed policy plus `run_context`: the last 8 finished runs of that exact motion (verdict, speed, tracking error, motor temperature, verdict confidence, retrieval score, blocked reason, last error), failure-mode counts, and the deduplicated lifecycle log of up to 3 recent non-successful runs. Every run carries `HarnessRunId` and `VideoSampleHash`, so each one is flagged `same_video` against the indexed clip. A run with local evidence can be opened with `read_example(run_id)` for its full motor telemetry and recorder log. The plan panel shows the chosen speed and why.

Keewano can only slow a skill down or hold it. It never raises speed above the controller cap, never changes the taught trajectory, and never skips the scene, approval or telemetry gates. If Keewano is unset or unreachable, the harness keeps its default speed and records `keewano_unconfigured` or `keewano_unavailable` as the policy.

Requires `KEEWANO_PROJECT_ID`, `KEEWANO_EMAIL` and `KEEWANO_PAT` in `bridge/.env.local`, and `pg8000` in the harness venv.

## Instrumenting the harness

```python
from keewano import Run

with Run("robot_harness", trigger="sms", intent="fetch_object") as run:
    run.add("RetrievalTopScore", 71).add("RetrievalMatched", True)
    run.add("SkillSelected", "pick_cup")
    run.tool("motion_program", ok=True, latency_ms=4200)
    run.add("MotionSucceeded", True)
```

Event types live in `CUSTOM_EVENTS` in `keewano.py`. The list is append-only: add a new id rather than changing an existing type. Send categories, counts and flags, never prompts, transcripts or stack traces.

## Questions worth asking

- "Which skill fails most, and what was RetrievalTopScore right before the failures?"
- "Did motion_program timeouts drop after the fix four days ago?"
- "Are SMS-triggered runs denied approval more often than dashboard runs?"
- "Which Claude Code tools fail most, and with which ErrorClass?"
