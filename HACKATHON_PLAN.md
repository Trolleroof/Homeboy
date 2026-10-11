# Homeboy Hackathon Plan — Teach Once, Recall from Video

**Sprint length:** 12 hours from the agreed start time. Use elapsed time H+00 through H+12 so this works regardless of when the event begins.

**Customer promise:** A person teaches the SO-101 one known tabletop task by moving the leader arm. Later they text a request, Homeboy finds the matching saved demonstration, asks them to confirm, runs only that previously taught routine in the same checked workcell, and texts back the result with a clip. Each request leaves a trace of what was asked, which episode was selected, what happened, and how long it took.

**Demo task:** Pick up the orange block and place it in the fixed bin. Keep the table, block, bin, arms, and camera positions fixed for the first full run. If safe replay is not built and verified, show retrieval and the saved clip, then stop at human confirmation. Do not claim the robot completed the task.

## What has been verified previously (historical evidence)

- The Homeboy dashboard and its project-owned bridge run independently of the SUDS dashboard.
- Both SO-101 arms were found on their configured serial ports. All six motors on each arm responded, joint readings were available, and teleop reached record-ready.
- The wrist camera produced a live 640×480 image.
- The browser Start teaching → Stop teaching → Save flow saved an episode and wrist video. Two more short engage/record/stop/discard cycles passed.
- EmbeddingGemma indexed a task description and returned the linked saved episode for a semantic text query.
- The hardware trials in the earlier verification run were brief and stationary. A successful block pick-and-place and safe replay have **not** been verified. Episodes 0 and 1 are diagnostic clips; episode 2 is task-labeled but its outcome is unverified.

## Current execution status — 2026-10-10 22:15 PDT

**Superseded by the 2026-10-11 harness update:** a local prompt planner, bounded trajectory executor and camera outcome checker are implemented. The prompt UI runs at http://127.0.0.1:3217/#prompt. The bridge is in offline planning mode because both configured serial devices are absent. Episode 2 is imported but remains unapproved; no complete physical autonomous task has been tested. See [the harness guide](bridge/HARNESS.md) for current scope, setup and evidence. Sponsor checkpoints below retain their earlier status.

- The dashboard is reachable on `http://127.0.0.1:3217`; its project recorder is online on port 8621 and idle.
- The newest bridge snapshot reports both arms unavailable (0/6 motors each) and no wrist-camera frame. Do not start teleop until the operator reconnects and verifies the cell. The port names in the project config are not proof of current hardware readiness.
- The dataset has three episodes. Episodes 0 and 1 are explicitly diagnostic. Episode 2 is a 309-frame, 10.3-second task-labeled recording; its physical outcome is unverified. It is now indexed as “Pick up the orange block and place it in the bin (outcome unverified)” for retrieval only.
- Local EmbeddingGemma returned episode 2 at 0.655 for “put the orange block into the bin”; “wash a coffee mug in the sink” scored 0.341 and was rejected. Search now requires a score of at least 0.42 and, when there is more than one candidate, an 0.08 lead over the next candidate. This is a small-demo guardrail, not a calibrated confidence measure.
- No Dial or Keewano credential names are present in the app environment. Their integrations and real sponsor events are not verified.
- Local retrieval events now append to `datasets/run-events.jsonl` and can be read by run ID from `/api/events`. One trace records `request_received → skill_search_completed → skill_selected` for episode 2. This is the planned offline audit fallback; it is not Dial or Keewano.
- Checkpoint state: Hour 04 retrieval passes for this demo scope; Hours 01–03 and 05 are blocked on fresh hardware/task proof or sponsor access. Hours 06, 07, and 09 are blocked or cut by sponsor access and the missing safe executor; Hour 08 passes only through the local JSONL fallback; Hours 10–12 remain pending.

## Latest retrieval update

Native video retrieval is now verified through the dashboard APIs. Episode 2 is indexed from eight decoded frames; “A wooden tabletop with black and white square markers” scores 0.696 and retrieves it, whereas the same query in task-label mode scores 0.309 and is rejected. “Wash a mug in the sink” scores 0.579 in video mode and is rejected by the 0.65 demo threshold. This is one-clip pipeline evidence, not a calibrated multi-skill benchmark. Full evidence: [verification](bridge/VIDEO_RETRIEVAL_VERIFICATION.md).

## Keep the claims honest

- The default **Video content** search now uses native EmbeddingGemma 2 video embeddings from eight sampled wrist-camera frames per saved episode. Task-label search remains a separate legacy option. Video retrieval does not establish task completion or produce robot actions. See [video verification](bridge/VIDEO_RETRIEVAL_VERIFICATION.md).
- Similarity search can select a relevant demonstration. It cannot adapt that demonstration to a new object, moved bin, or unseen layout. The 12-hour target is one known routine in one verified setup, with human confirmation. A moved target or novel task is out of scope unless a separately tested controller handles it.
- Dial is the customer’s text interface. Inbound message.received events arrive over its account event stream or a signed webhook. The stream is easiest for a local demo but is presence-based; a webhook is durable and requires public HTTPS, signature checking, and event-ID deduplication. See [Dial event stream](https://docs.getdial.ai/documentation/platform/stream-account-events) and [Dial webhooks](https://docs.getdial.ai/documentation/platform/webhooks).
- Keewano is the run-history and analysis tool. First verify the event-ingestion method, workspace, and credentials actually provided by the sponsor. Do not assume its Unity SDK is the right interface for this robot app. The [KeewanoDB overview](https://www.keewano.com/overview) describes SDK/API ingestion and REST, SQL, and MCP queries.
- Keep robot control deterministic. A Dial message may request a named saved skill; it must never become free-form joint commands. Require an allowed sender, unique request/run ID, exact skill match above a measured threshold, explicit confirmation, workspace readiness, fresh joint readings, and an accessible physical stop. Refuse replay if any check fails.

## Hour-by-hour plan

Each hour ends with an evidence file in the [checkpoints folder](checkpoints/). Each file starts as a template; fill in its evidence and mark it PASS, BLOCKED, or CUT. Code compiling alone does not pass a checkpoint. Use the observable proof below.

### H+00–01 — Freeze the demo and make the cell safe

Agree on the single customer story and fixed tabletop scene. Clear the work area, identify the physical stop, name the person at the controls, and photograph the block/bin arrangement. Keep both arms in observing mode while confirming the USB port mapping and camera identity. Do not begin by adding features.

**Checkpoint:** [Hour 01 — safe, named demo setup](checkpoints/hour-01.md). Pass only when the scene photo, task sentence, operator, physical stop, and confirmed leader/follower port names are recorded.

### H+01–02 — Prove teaching with a real task

Record a complete orange-block-to-fixed-bin demonstration. Watch the wrist feed. Stop and save it; check that the library shows a playable clip and that the episode contains joint actions. If the task fails physically, repeat teleop once. Do not proceed with only diagnostic clips.

**Checkpoint:** [Hour 02 — first real demonstration](checkpoints/hour-02.md). Pass only with the saved episode ID, frame count, playable clip, and an operator-confirmed task outcome.

### H+02–03 — Make capture dependable

Fix only capture problems that block the user: task naming, button state, readiness feedback, stop/save/discard behavior, and clear errors. Check that restart cannot leave a hidden recorder or a second process holding the serial ports. Save one clean demo and discard failed takes.

**Checkpoint:** [Hour 03 — repeatable teach/save](checkpoints/hour-03.md). Pass only after two consecutive real demos can be saved or safely discarded from the dashboard, with both outcomes visible in the episode list.

### H+03–04 — Confirm what skill search retrieves

Index the actual saved wrist video with local EmbeddingGemma 2. Search for visible details absent from the title, plus an unrelated request. Show the selected episode, similarity score, and clip. Reject weak or ambiguous matches. Verify that only sampled pixels enter the video vector. Test several distinct task clips before claiming reliable selection; one clip proves the pipeline only.

**Checkpoint:** [Hour 04 — grounded video retrieval](checkpoints/hour-04.md). Pass only if a paraphrase finds the correct saved task and an unrelated request is rejected or clearly marked as no match.

### H+04–05 — Test sponsor accounts before integrating

Verify the Dial number, API credential, receipt of a real inbound test SMS, and exact event payload. Keep the inbound listener alive and deduplicate by event ID. Verify the Keewano workspace, event-write credentials, and one documented query path. Put secrets in ignored local environment settings, never browser code or the repository. If either account is unavailable, switch to its fallback now.

**Checkpoint:** [Hour 05 — sponsor access proven](checkpoints/hour-05.md). Pass only with one real inbound Dial event captured and one test event visible in Keewano. If access fails, mark it blocked and use the fallback; do not spend the next hour guessing endpoints.

### H+05–06 — Build Dial as the customer request channel

Route an inbound SMS to saved-skill search. Accept only the allowed sender and supported request. Reply with the matched task and a short confirmation token, such as RUN 4K2P. Ignore duplicate event IDs. Add timeout, no-match, and busy-robot replies. For the local demo, use Dial’s account event stream; use a signed webhook only if public HTTPS is already available and verified.

**Checkpoint:** [Hour 06 — SMS selects and confirms a skill](checkpoints/hour-06.md). Pass only when a real phone can text a task, receive the correct skill/confirmation prompt, and a duplicate event cannot create a second run.

### H+06–07 — Prove controlled execution before connecting SMS to motion

Only if a validated fixed-scene executor exists, run the saved routine locally without Dial. Compare the requested episode with the executed action sequence; watch the whole motion and stop if the follower diverges, readings go stale, the camera drops, or the cell changes. If there is no safe executor, keep execution manual and mark autonomous replay CUT; use the saved clip as the human guide.

**Checkpoint:** [Hour 07 — execution gate](checkpoints/hour-07.md). Pass only with a full, operator-observed completion in the original fixed scene and a trace of the exact selected episode. Otherwise record the reason and disable robot execution from SMS.

### H+07–08 — Add Keewano run evidence

Emit an ordered event trail: request_received, skill_search_completed, skill_selected or no_match, confirmation_received, execution_started, execution_stopped, and task_completed or task_failed. Include request/run ID, episode ID, timestamps, result, and error category. Send no video or personal phone number to the database; use a clip path and, if needed, an allowed sender ID/hash. Query one run back from Keewano.

**Checkpoint:** [Hour 08 — one trace is queryable](checkpoints/hour-08.md). Pass only if Keewano returns the events for one run in order and the IDs link to its Dial request and saved episode. Otherwise retain a local JSONL audit and mark Keewano unavailable in the demo.

### H+08–09 — Connect confirmation to the bounded action

Only after Hour 07 passes, connect RUN plus token to the tested executor. Enforce one active run, confirmation expiry, same-scene/operator check, live teleop readiness, and idempotency so message retries cannot repeat motion. If any condition fails, reply with the reason and leave the robot idle. Refuse new or moved targets.

**Checkpoint:** [Hour 09 — one confirmed request, one bounded run](checkpoints/hour-09.md). Pass only after one real SMS starts one execution and a duplicate/replayed SMS causes no second execution. If Hour 07 failed, use this hour to harden human-confirmed recall instead.

### H+09–10 — Measure customer time saved

Run the workflow as a fresh user: teach once, send an SMS later, confirm, observe completion or refusal, and open the clip. Measure elapsed time and count manual steps from message to result. Compare with repeating the original manual setup. Ask a teammate who did not build it to follow the dashboard guide without coaching.

**Checkpoint:** [Hour 10 — customer walkthrough and time measure](checkpoints/hour-10.md). Pass only with a screen recording, timings, step count, and the teammate’s confusion point or confirmation that the flow was clear.

### H+10–11 — Rehearse failures and prepare the story

Exercise no match, wrong sender, duplicate text, expired confirmation, busy recorder, stale arm readings, disconnected wrist camera, Keewano outage, and Dial outage. Rejected requests must leave the robot idle. Prepare a 60-second explanation: what was taught, how Dial carries the request, how text search selects the saved episode, what Keewano records, and what the system refuses to generalize.

**Checkpoint:** [Hour 11 — failures handled and pitch ready](checkpoints/hour-11.md). Pass only with a completed failure table, clean demo reset, short pitch, and backup screen recording.

### H+11–12 — Freeze, run once, and hand in

Do not add capabilities. Restart from documented commands, run the rehearsed path once, capture the result, and verify the evidence files. Confirm the physical arms are disengaged and the stop is accessible. Package the app link, source/run instructions, checkpoints, and pitch. List blocked sponsor integrations plainly.

**Checkpoint:** [Hour 12 — submission-ready handoff](checkpoints/hour-12.md). Pass only when a teammate can start the app from its README, evidence links open, the final run is documented, and both arms are confirmed disengaged.

## Cut order if time slips

Keep in order: (1) safe real teaching and saved clip, (2) EmbeddingGemma 2 video search with no-match behavior, (3) Dial SMS selecting and returning a clip, (4) Keewano event trail, (5) bounded robot replay only if independently proven safe. If the executor is not proven by H+07, do not claim text-triggered physical execution. Drop extra tasks, moved-bin/OOD claims, voice calls, continuous live-stream indexing, and model training first.

## Commands and places to find evidence

- Homeboy UI: from frontend/, run npm run dev; open http://127.0.0.1:3217 (currently reachable).
- The project bridge starts from the dashboard when teaching begins and listens on localhost port 8621.
- EmbeddingGemma 2: project `.embedding-venv`, service auto-start on port 8622. See frontend/README.md. Ollama is only used by optional Task labels mode.
- Arm config: [bridge/config/arms.json](bridge/config/arms.json). Current camera config: [bridge/config/cameras.json](bridge/config/cameras.json), wrist=0.
- Episodes and videos: ~/.cache/huggingface/lerobot/willow/speedhacks-wrist-v1.
- Local skill index: datasets/willow_skills.sqlite3. Recorder log: bridge/logs/recorder.log.
- Start at [Hour 01 checkpoint](checkpoints/hour-01.md). Record a timestamp and a link or filename at each checkpoint. Never fill a checkpoint with expected results before observing them.


## Codex harness integration — 2026-10-11

The prompt panel now uses native Codex app-server tools to retrieve an indexed video skill, read motor state, build a bounded motor program, and execute the confirmed program through `task_harness.py`. Successful camera verdicts save experience memory in the existing SQLite vector DB; experience recall requires an independently matched video and unchanged approved motion hash. Codex uses account allowance, with at most eight tool calls per turn and no calls in the 30 Hz controller. The fixed-workcell and physical-validation limits still apply. See [harness runbook](bridge/HARNESS.md).

The harness also reads the retrieved example's recorded actions and measured joint states, current motor/temperature/camera telemetry, bounded daemon logs, and previous execution traces. Planning must inspect the selected example, telemetry and logs before building a program. Every execution saves local evidence; native EmbeddingGemma indexes its outcome/error/telemetry summary and associates the full evidence with the unchanged motion. Failures and uncertain outcomes are diagnostic history only. Replay still repeats the approved demonstration through the existing controller and live scene checks; it does not synthesize new motor angles from logs or train a new policy. Hardware-free checks passed; physical replay remains a separate acceptance gate.

## Dial SMS adapter — 2026-10-11

The local Dial sidecar is implemented in `bridge/scripts/dial_sms.py`. It uses the official SDK event stream, authorizes the sender and receiving number, deduplicates event/message IDs persistently, sends expiring sender-bound RUN tokens, and records customer consent against the exact harness plan. Execution remains a separate local operator action after the original workcell check. Planning and execution now retain one parent request ID; the sidecar reports only the correlated execution outcome. It never dispatches motor execution itself.

Setup and the event coupon `DIAL_FOR_SPEEDHACKS` are documented in [the Dial SMS runbook](bridge/DIAL_SMS.md). CLI 0.56.0 and isolated SDK 0.40.2 are installed. The idle offline bridge was restarted with this implementation. Forty-nine hardware-free Python tests passed, including the actual bridge HTTP request/confirmation path with fake provider/hardware dependencies; TypeScript, focused ESLint and the frontend production build passed. A separate live-local-bridge check refused the unapproved task and suppressed a duplicate event. Evidence: `datasets/dial-sms-verification.json`.

Dial's backend is reachable, but the account is not signed in, coupon redemption is unverified, and no real inbound/outbound SMS has been observed. Hours 05–06 remain BLOCKED for live sponsor acceptance. Episode 2 remains unapproved and production experience/run-evidence memory remains empty. The sidecar emits local structured events for a future Keewano adapter; Keewano ingestion and a phone-accessible clip/result page are not implemented by this change.

### Sponsor one-pager correction

The full [Dial SpeedHacks PDF](https://drive.google.com/file/d/1D1eyN1o0CNJFfCt5EmjdizCgAswhZdod/view) and its [live quickstart](https://getdial.ai/hackathon/speedhacks/quickstart) specify that immediate hackathon SMS delivery is **Dial-to-Dial**; replies to ordinary phones require business 10DLC registration. Use an authorized Dial sender for the SMS demo. iMessage/WhatsApp access can be requested at the sponsor table but require a later channel adapter. The sidecar now accepts authorized internal Dial events as well as external SMS events, while still checking sender, receiving number and channel and rejecting reply echoes. It also accepts the guide's `DIAL_NUMBER` setting. The PDF contains signup/API-key instructions, not a shared key: sign up, obtain the key from Settings, and request $100 builder credits using that account email on the live quickstart form.
