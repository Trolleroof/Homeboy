const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

function load(file, mocks, globals = {}) {
  const module = { exports: {} };
  const source = ts.transpileModule(fs.readFileSync(path.join(__dirname, "..", file), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  vm.runInNewContext(source, { exports: module.exports, module, require: (name) => mocks[name] ?? require(name),
    Request, Response, URL, ...globals });
  return module.exports;
}

async function main() {
  const feedback = load("lib/recorder-feedback.ts", {});
  const tracking = { state: "recording", teleop: { engaged: true, ready: false, record_ready: true }, data_quality: { ready: true } };
  assert.equal(feedback.teleopReady(tracking), true, "Moving the leader must not invalidate a settled controller");
  assert.equal(feedback.teleopReady({ ...tracking, teleop: { ...tracking.teleop, record_ready: false } }), false);
  const error = feedback.recorderWaitError({ ...tracking, message: "frame error: size" }, "starting the recording");
  assert.match(error, /arms are connected/);
  assert.match(error, /frame error: size/);
  assert.doesNotMatch(error, /cables|didn't connect/);
  assert.match(feedback.recorderWaitError(tracking, "starting the recording"), /still stop/);
  assert.match(feedback.recorderWaitError({ state: "idle", teleop: { engaged: false, ready: false } }, "checking teleop"), /Sync arms/);

  const lease = { held: true };
  const calls = [];
  let timeout;
  let failFetch = false;
  const route = load("app/api/bridge/[...path]/route.ts", {
    "@/lib/arm-lease": { armLease: lease },
    "next/server": { NextResponse: { json: (body, init) => new Response(JSON.stringify(body), init) } },
    "@/lib/robot-bridge": { recorderURL: "http://recorder", root: "/unused", config: () => ({ cameras: [] }), status: () => ({ running: true }) },
    "../../skills/route": {}, "../../video/route": {},
  }, {
    AbortSignal: { timeout: (ms) => { timeout = ms; return new AbortController().signal; } },
    fetch: async (url) => {
      calls.push(url);
      if (failFetch) throw Object.assign(new Error("Timeout"), { name: "TimeoutError" });
      return new Response(JSON.stringify({ ok: true }), { headers: { "content-type": "application/json" } });
    },
  });
  const request = (group, action, method = "POST") => route[method](new Request(`http://dashboard/api/bridge/${group}/${action}`, {
    method, ...(method === "POST" ? { body: JSON.stringify({ confirmed: true }) } : {}),
  }), { params: Promise.resolve({ path: [group, action] }) });
  for (const [group, action] of [["recorder", "estop"], ["recorder", "stop"], ["recorder", "disengage"], ["harness", "stop"]]) {
    assert.equal((await request(group, action)).status, 200, "Interrupt commands must pass the calibration lease");
  }
  assert.equal(calls.length, 4);
  assert.equal((await request("recorder", "record")).status, 409, "New recording still respects calibration ownership");
  lease.held = false;
  await request("recorder", "stop");
  assert.equal(timeout, 35000, "Proxy must outlast the recorder's 30-second command wait");
  await request("recorder", "sync");
  assert.equal(timeout, 120000);
  failFetch = true;
  const timedOut = await (await request("recorder", "save")).json();
  assert.notEqual(timedOut.offline, true, "Slow command must not declare working arms disconnected");
  assert.match(timedOut.error, /Check live status/);
  assert.equal((await (await request("recorder", "status", "GET")).json()).offline, true);
  console.log("Recorder feedback, calibration interruption, and proxy timeout regressions passed.");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
