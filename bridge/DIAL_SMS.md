# Homeboy Dial SMS adapter

Dial carries the existing **teach once, recall by text** workflow. The Python
sidecar listens to official `message.received` events, submits the text to
Homeboy's existing Codex harness, sends an expiring RUN token, and reports the
result of the locally supervised execution. No new motion planner is added.

## Sponsor access and coupon

The [SpeedHacks resources](https://hackersquad.io/events/speedhacks-oct11/resources?tab=resources)
list **DIAL_FOR_SPEEDHACKS** for $100 in Dial credit. The official
[Dial signup instructions](https://getdial.ai/) support the coupon on login:

```sh
npm install -g @getdial/cli@0.56.0
dial auth login YOUR_EMAIL --coupon DIAL_FOR_SPEEDHACKS
dial auth verify-otp --code YOUR_EMAIL_CODE
```

For a new account, follow the CLI's request to verify a real phone you control:

```sh
dial auth register-number YOUR_E164_PHONE
dial auth verify-otp --number --code YOUR_PHONE_CODE
dial doctor --json
dial billing --json
```

Use the CLI's actual responses to determine whether an account, number and
coupon credit exist. The presence of the code here does not prove redemption.
Do not add paid credit or buy extra numbers to work around a failed coupon.
Existing-account coupon handling must be checked with Dial; do not assume
signup grants can be applied retroactively.

The full [sponsor one-pager](https://drive.google.com/file/d/1D1eyN1o0CNJFfCt5EmjdizCgAswhZdod/view)
also provides a [builder-credit request form](https://getdial.ai/hackathon/speedhacks/quickstart#request).
Sign up first, then use the same account email to request the $100 credits,
optionally iMessage or WhatsApp access. The API key is available in
**Dial dashboard → Settings**, and the number in **Numbers**; there is no shared
API key printed in the PDF. `DIAL_API_KEY` and `DIAL_NUMBER` from that guide are
accepted by this adapter.

**Hackathon delivery constraint:** the sponsor guide says SMS works immediately
between Dial numbers. Outbound texts to regular phones require business 10DLC
registration. For a live SMS demo, use an authorized second Dial number as the
requester, or ask the Dial table for iMessage/WhatsApp access for a later channel
adapter. This adapter only implements SMS. A regular phone sending a request
does not establish that Homeboy's SMS reply can reach it.

The CLI stores its key in a private local file. The adapter reads
`~/.local/share/dial/auth.v1.json`, falling back to legacy `auth.json`, respecting
`XDG_DATA_HOME`. No key goes to the browser or the robot's Codex tool process.
The sidecar owns its event connection; it does not need `dial listen install`.

## Start locally

From the project root:

```sh
uv venv .sms-venv --python 3.12
uv pip install --python .sms-venv/bin/python -r bridge/sms-requirements.txt
cp bridge/.env.example bridge/.env.local
```

Edit the ignored `bridge/.env.local`: set `HOMEBOY_SMS_ALLOWED_SENDERS` to the
comma-separated E.164 sender numbers allowed to request this robot. For the
hackathon SMS demo these should be authorized Dial numbers. Set `DIAL_NUMBER_ID`
or `DIAL_NUMBER` if the account has multiple ready SMS numbers. Start the
existing Homeboy UI and bridge using [HARNESS.md](HARNESS.md), then:

```sh
.sms-venv/bin/python bridge/scripts/dial_sms.py --check
.sms-venv/bin/python bridge/scripts/dial_sms.py
```

`--check` reads the account, number and bridge and validates the same sender and
result-URL configuration as the listener. It excludes replaced numbers, reports
whether the harness has approved skills and is idle, and does not mark the event
stream or SMS delivery verified. It is not a live SMS test. Reply attempts time
out after 15 seconds and remain unknown rather than being retried.
The normal listener uses Dial SDK 0.40.2, verified against its installed source
(`send_message(SendMessageParams(...))`, `new_events_connection()`).

## Customer and operator flow

1. The authorized requester texts **Put the orange block in the bin** to Homeboy's Dial number.
2. Homeboy plans through the same video retrieval, task contract, approval,
   telemetry/example/log inspection and bounded-program tools as the prompt UI.
   An unapproved or unsupported task gets a refusal, without motion.
3. The requester receives the matched task and **RUN XXXXXXXX**, valid for the
   remaining plan lifetime, at most five minutes.
4. That same sender replies with the token. Homeboy records consent; this does
   not move the robot.
5. The operator sees the SMS confirmation in the prompt panel, checks the
   original workcell and start pose, and presses **Run on robot**. The button
   and backend both reject an SMS plan before customer confirmation. Existing
   physical readiness, scene, freshness and single-use gates still apply.
6. The sidecar observes only the execution with the matching parent request
   ID and sends its result. Camera success is explicitly provisional; failed,
   blocked and uncertain outcomes are not promoted in the SMS reply.

`HELP` returns these instructions. No STOP, raw joint, arbitrary bridge action,
or automatic physical retry command is exposed through SMS.

## State, trace and delivery limits

`datasets/dial-sms/sms.sqlite3` persists event IDs, message IDs, sender-bound
tokens, request/plan/motion IDs, execution IDs and reply attempts. Its private
sender addresses are needed for replies; the directory and DB are private and
Git-ignored. A file lock prevents concurrent listeners using the same state.

`datasets/dial-sms/events.jsonl` records request lifecycles and hashed sender
identity, without raw phone numbers or message text. Execution evidence retains
the parent request and plan IDs in `datasets/harness/`. These events are the
future Keewano ingestion seam; this adapter does not itself ingest into Keewano.

Duplicate event/message IDs do not repeat planning or consent. RUN tokens are
sender-bound, plan-bound and expire. Restart never resubmits robot commands.
Authorized `source=internal` Dial-to-Dial events and `source=external` events
are both accepted, but only when the receiving number, channel and sender
match configuration. The adapter's outbound reply cannot become a new request.
Outbound SMS attempts are reserved before network I/O and are never automatically
retried: an ambiguous timeout may already have sent the message. `submitted`
means API acceptance, not verified carrier delivery.

Dial's [event stream](https://docs.getdial.ai/documentation/platform/stream-account-events)
is presence-based and only replays short gaps within two minutes. The listener
must stay running during the demo. Durable deployment needs a signed HTTPS
webhook; never publicly expose the robot control endpoints.

SMS returns the run ID by default. Optionally set `HOMEBOY_SMS_RESULT_URL` to an
already verified HTTPS **read-only** result page that accepts `run_id` and
`request_id`. This adapter does not deploy that page or publish camera media.
Current execution evidence is snapshots and telemetry, not a newly recorded
replay video; do not claim that a phone clip was delivered.

## Verification

```sh
.sms-venv/bin/python -m unittest discover -s bridge/scripts -p 'test_dial_sms.py' -v
"$HOMEBOY_PYTHON" -m unittest discover -s bridge/scripts -p 'test_*.py' -v
```

Tests use realistic text requests, temporary SQLite state and fake provider /
robot dependencies. They cover deduplication across restart, sender/number/
channel checks, stale events, bad/expired tokens, busy requests, replaced plans,
ambiguous sends, and confirmation without motion. They do not prove carrier
delivery, coupon redemption, or physical block placement.

Pass Hour 05 only after a real inbound event arrives. Pass Hour 06 only after
a real phone receives the matched task/token and duplicate delivery causes no
second planning/run. Hour 09 still requires independently validated physical
execution and the operator's local check.

## SDK and harness audit (October 11)

The installed 0.40.2 SDK's authentication header, number deserialization and
`send_message(SendMessageParams(...))` request format passed a mocked HTTP
transport check. `uv pip check` reports compatible dependencies. All 70 harness
tests passed, including the actual HTTP bridge planning/confirmation path with
mocked model, delivery and motors; all 15 adapter tests passed in `.sms-venv`.

The Dial SMS SDK and mocked transport passed local checks, but SMS delivery has
not been exercised with a real message. Voice is configured separately; its
number, tool connection, and local harness status are shown in the Homeboy
frontend. Keep Dial credentials in ignored `bridge/.env.local`. A `kwpat-` token
is for Keewano analytics and is not a Dial credential. Provider delivery and
physical execution remain separate checks.

## Run history and stats over SMS

Questions about past runs ("how is the orange block task doing?", "show logs", "why did it fail?", "stats") go to the bridge's read-only `POST /harness/insights` endpoint, not the planner. The question is matched against the indexed demonstration videos (EmbeddingGemma). Homeboy replies with that skill's recent Keewano outcomes, success rate (including for the same video), tracking error, motor temperature, next replay speed, top failure reasons and the log of the latest run that didn't succeed. No request, plan or run is created. It works while a task is pending, and an unavailable Keewano is reported without retrying.
