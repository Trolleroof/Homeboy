# Call Homeboy through Dial

Dial voice number: **+1 218-231-5815**.

Calls connect to Dial's managed voice agent and its Homeboy Context MCP. The
agent can read harness readiness, plan a taught task, read the plan back, and,
after the caller confirms on the call that the workcell is set up and the path is
clear, run it on the robot (`willow_run_plan`). It reports the vision-verified
verdict through `willow_task_progress`, and `willow_stop` halts the robot
immediately. The harness still requires an approved skill, a confident camera
scene check and bounded replay. Set `DIAL_VOICE_EXECUTION=off` in
`bridge/.env.local` to return to plan-only. The Context MCP checks Dial's static bearer credential plus the live
inbound caller number and Dial number on every tool call. The only allowed
caller is configured by `DIAL_VOICE_ALLOWED_CALLERS` in ignored
`bridge/.env.local`.

## Current test status

- Dial number and inbound calling are enabled.
- Dial Context MCP `Homeboy robot harness` is connected with two tools.
- The local MCP endpoint runs at `127.0.0.1:8623`; a temporary ngrok HTTPS
  tunnel makes it reachable to Dial. Both are currently running from the local
  task session. The ngrok URL and MCP bearer are private local configuration.
- The robot harness and local vision model are online. Motor telemetry is
  fresh, but the live wrist feed reported a reconnect issue on the dashboard.
- Episode 2 is indexed but unapproved. Phone planning is correctly blocked until
  a successful demonstration is reviewed and approved in the frontend.

## Start after a restart

The private variables already reside in `bridge/.env.local`. Run these from the
repository root, keeping both processes open:

```sh
.sms-venv/bin/python bridge/scripts/dial_voice_mcp.py
ngrok http 8623
```

If ngrok gives a new URL, update `DIAL_MCP_URL` in `bridge/.env.local`, update
the FastMCP `allowed_hosts` setting in `dial_voice_mcp.py`, and replace the
existing `Homeboy robot harness` context MCP URL in Dial. The HTTP listener
shows whether Dial currently sees two tools in the frontend card.

## Try a call

From the configured caller **+1 925-819-4040**, call **+1 218-231-5815** and
ask “Can Homeboy access the robot right now?” The agent calls `willow_status`.
Then ask it to “Put the orange block in the bin.” It calls `willow_plan_task`.
At present this should report that planning is blocked because no skill is
approved. Use the local skill-review controls to watch, validate and approve a
successful taught demonstration first. For safe physical testing, reconnect the
wrist camera and arms, verify the fixed workcell, then supervise from the
frontend.

The dashboard call card shows the number, Dial MCP/tool connection, harness,
approved skill count, motor freshness, and recent Homeboy tool calls. Each call
is written to `datasets/dial-voice/tools.jsonl` with a hashed caller identity.
It does not store transcripts or expose API credentials.

## Evidence and limits

The endpoint was reached through the public HTTPS tunnel with a local MCP
integration probe using the allowlisted caller headers. A `willow_status` tool
call returned current live harness state; a `willow_plan_task` call reached the
real harness and was rejected because Episode 2 is unapproved. This confirms
tool connectivity and the approval gate, not a completed real telephone
conversation or any robot motion. The frontend activity list will show actual
tool calls as Dial invokes the tools during a call. Dial's own
[receive-call guide](https://docs.getdial.ai/documentation/capabilities/receive-a-voice-call)
explains managed inbound voice; [Context MCP](https://docs.getdial.ai/documentation/platform/context-mcp)
documents server tools and the per-call headers used by the caller check.

## Run history tool

`willow_run_history(question)` answers callers' questions about past runs, stats, failures and logs from Keewano through the bridge's read-only `/harness/insights` endpoint. It returns a `spoken_summary` plus per-run outcomes and telemetry for the skill whose demonstration video best matches the question. It never plans or moves the robot. A first call can take about 10–15 s while the embedding model and Keewano respond.
