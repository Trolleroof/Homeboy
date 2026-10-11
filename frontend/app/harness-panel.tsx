"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Image from "next/image";

type Skill = { id: string; description: string; approved: boolean; duration: number; conversion: string; episode: number };
type Harness = {
  motion_policy: { max_speed: number; max_command_step: number; controller_hz: number };
  active: boolean; model: string; model_ready: boolean; vision_model: string; vision_ready: boolean; memory: { backend: string; experiences: number; demonstrations: number }; skills: Skill[];
  motors: { positions: Record<string, number>; age_s: number };
  motion: { active: boolean; elapsed_s: number; duration_s: number | null; target?: Record<string, number> | null; tracking_error?: number | null; error?: string };
  plan: { direct?: boolean; id: string; description: string; reason: string; duration: number; start_pose: Record<string, number>; source?: string; sms_confirmed?: boolean; phone_confirmed?: boolean; speed?: number; policy?: { policy: string; reason: string; success_rate: number | null } } | null;
  run: { execution_plan?: { copy_audit?: { unchanged_values: number; joint_values: number; bounded_values: number }; approach_seconds?: number; execution_mode?: string; historical_context?: { recent_runs: unknown[] }; start_pose: Record<string, number>; description: string; duration: number }; id: string; state: string; local_calls: number; codex_calls: number; result: string | null; events: { turn?: number; replay_t?: number; decision?: string; event: string; at: number; error?: string; reason?: string; evidence?: string; tool?: string; result?: unknown; arguments?: unknown; reply?: string; usage?: { total: { inputTokens: number; outputTokens: number; cachedInputTokens: number } } }[]; verification?: { evidence: string; confidence: number } } | null;
};
type DialStatus = { configured: boolean; number: string; context: { connected: boolean; tools: number; error: string }; bridge: { online: boolean; active: boolean; approvedSkills: number; motorsFresh: boolean }; toolCalls: { event: string; caller: string; request_id?: string; accepted?: boolean; at?: string }[] };

export default function HarnessPanel() {
  const [status, setStatus] = useState<Harness | null>(null);
  const [prompt, setPrompt] = useState("");
  const [episode, setEpisode] = useState(1);
  const [description, setDescription] = useState("Pick up the orange block and place it down");
  const [reviewed, setReviewed] = useState(false);
  const [confirmedPlanId, setConfirmedPlanId] = useState("");
  const [busy, setBusy] = useState(false);
  const [busyAction, setBusyAction] = useState("");
  const submitting = useRef(false);
  const [pendingRun, setPendingRun] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [offline, setOffline] = useState(false);
  const [connectionError, setConnectionError] = useState("");
  const [dial, setDial] = useState<DialStatus | null>(null);
  const refresh = useCallback(async () => {
    try {
      const response = await fetch("/api/bridge/harness/status", { cache: "no-store" });
      const body = await response.json();
      if (!response.ok || body.offline || body.ok === false) {
        setOffline(true); setStatus(null);
        setConnectionError(body.calibrating ? "Calibration in progress. Finish or cancel it before connecting." : body.startup_error ?? (body.starting ? "Connecting…" : "Offline. Hit Connect."));
        return false;
      }
      setStatus(body);
      setPendingRun(current => current && (body.run?.id !== current || body.active) ? current : null);
      setOffline(false); setConnectionError("");
      return true;
    } catch { setOffline(true); setStatus(null); setConnectionError("Can't reach the robot."); return false; }
  }, []);
  useEffect(() => {
    let cancelled = false;
    let timer = 0;
    const loop = async () => {
      const online = await refresh();
      if (cancelled) return;
      timer = window.setTimeout(loop, online ? 1500 : 5000);
    };
    void loop();
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [refresh]);
  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try { const response = await fetch("/api/bridge/dial/status", { cache: "no-store" }); const data = await response.json(); if (!cancelled) setDial(data); }
      catch { if (!cancelled) setDial(null); }
      if (!cancelled) window.setTimeout(poll, 4000);
    };
    void poll();
    return () => { cancelled = true; };
  }, []);
  async function command(action: string, body: Record<string, unknown>) {
    if (submitting.current && action !== "stop") return null;
    submitting.current = true;
    setBusyAction(action);
    setBusy(true); setError("");
    setNotice(action === "do" ? "Finding a matching saved take and checking the motion…" : "Sending request…");
    try {
      const response = await fetch(`/api/bridge/harness/${action}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.error ?? "That didn't work");
      if (result.run_id) setPendingRun(result.run_id);
      await refresh();
      return result;
    } catch (reason) { setError((reason as Error).message); return null; }
    finally { submitting.current = false; setBusy(false); }
  }
  const [setupOpen, setSetupOpen] = useState(false);
  async function runTask() {
    setConfirmedPlanId("");
    const started = await command("do", { prompt });
    if (started) setNotice(`Using take ${started.episode + 1} (${Number(started.score).toFixed(2)} similarity). Approaching its start from live telemetry, then following recorded motion. STOP remains available.`);
  }
  const active = busy || !!pendingRun || status?.active;
  const currentSkill = status?.skills.find(s => s.episode === episode);
  const lastEvent = status?.run?.events.at(-1);
  const failure = status?.run?.result === "blocked" ? status.run.events.findLast(event => event.error)?.error : null;
  const stage = busy ? (busyAction === "do" ? "Finding a saved take…" : busyAction === "stop" ? "Stopping…" : "Sending request…") : status?.motion.active ? (status.motion.elapsed_s < (status.run?.execution_plan?.approach_seconds ?? 0) ? "Approaching demo start" : "Following recorded motion") : status?.active && status.run?.result ? "Saving run evidence…" : status?.active ? (status.run?.state.replaceAll("_", " ") ?? "Preparing…") : pendingRun ? "Preparing…" : "Run it";
  const latestTurn = status?.run?.events.findLast(event => event.turn != null);
  const startPose = status?.plan?.start_pose ?? status?.run?.execution_plan?.start_pose;
  const motorsFresh = status && status.motors.age_s < .5;
  const confirmed = !!status?.plan && confirmedPlanId === status.plan.id;
  const awaitingSMS = status?.plan?.source === "dial_sms" && !status.plan.sms_confirmed;
  return <section className="card harness-card" id="prompt">
    <div className="harness-heading"><div><h2>Tell it what to do.</h2></div><span className={`connection-pill ${motorsFresh ? "connected" : ""}`}><span className="pulse" />{motorsFresh ? "Arm live" : "No robot"}</span></div>
    {offline && <p className="harness-alert">{connectionError}</p>}
    <div className="dial-card" aria-label="Dial phone connection">
      <div className="dial-card-heading"><div><span className="step-tag">CALL HOMEBOY</span><h3>{dial?.number || "Loading Dial number…"}</h3><p>Call this number to ask Homeboy about the harness or request a taught task plan.</p></div><a className="quiet-button" href={`tel:${dial?.number ?? ""}`}>Call ↗</a></div>
      <details className="dial-activity"><summary>Did the voice agent reach Homeboy? {dial?.toolCalls.length ? ` · ${dial.toolCalls.length} recent tool call(s)` : " · No Homeboy tool calls recorded yet"}</summary>{dial?.toolCalls.length ? <ol>{dial.toolCalls.map((event, index) => <li key={`${event.event}-${event.at}-${index}`}><strong>{event.event.replaceAll("_", " ")}</strong>{event.accepted !== undefined ? ` · ${event.accepted ? "plan request accepted" : "plan request refused"}` : " · read-only"}{event.request_id && <code> · {event.request_id.slice(0, 8)}</code>}</li>)}</ol> : <p>Call the number. Homeboy tool calls will appear here after Dial invokes the harness tools.</p>}</details>
      <p className="dial-note">Phone agent can inspect status and submit plans. You must review the plan and start any robot motion from this page.</p>
    </div>
    {offline && <div className="harness-actions"><span>Plug in both arms.</span><button className="quiet-button" disabled={!!active || !!status?.motion.active} onClick={async () => {
      setBusy(true); setError("");
      try {
        const response = await fetch("/api/bridge/daemon/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ task: description }) });
        const body = await response.json();
        if (!response.ok || body.ok === false) throw new Error(body.error ?? "Couldn't connect.");
      } catch (reason) { setError((reason as Error).message); }
      finally { setBusy(false); void refresh(); }
    }}>Connect</button></div>}
    <form onSubmit={event => { event.preventDefault(); void runTask(); }}>
      <label className="field-label" htmlFor="robot-prompt">What should it do?</label>
      <textarea id="robot-prompt" rows={2} maxLength={500} placeholder="Orange block into the bin" value={prompt} onChange={event => setPrompt(event.target.value)} disabled={!!active} />
      <div className="harness-actions"><span>{!status ? "" : !motorsFresh ? "Connect the arm" : "Copies measured joints from the closest saved take and replays them"}</span><button className="primary-button" disabled={!!active || !prompt.trim() || !motorsFresh || !status?.memory.demonstrations}>{active ? stage : "Run it"}</button></div>
    </form>
    {active && !status?.motion.active && <progress aria-label={stage} />}
    {active && <p className="harness-notice" role="status"><span className="pulse" /> {stage} {status?.motion.active && `${Math.floor(status.motion.elapsed_s)} / ${Math.ceil(status.motion.duration_s ?? 0)} seconds`}</p>}
    {status && <details className="harness-telemetry" open={!!active}><summary>Live arm telemetry · readings {status.motors.age_s.toFixed(2)}s old</summary><div className="motor-grid">{Object.entries(status.motors.positions).map(([joint, value]) => <span key={joint}>{joint.replace(".pos", "")}<b>{value.toFixed(1)}</b>{status.motion.target?.[joint] != null && <small>Target {status.motion.target[joint].toFixed(1)}</small>}{startPose?.[joint] != null && <small>Demo start {startPose[joint].toFixed(1)} · gap {Math.abs(value-startPose[joint]).toFixed(1)}</small>}</span>)}</div><p>{status.motion.tracking_error != null && `Tracking error ${status.motion.tracking_error.toFixed(2)} · `}Speed cap {status.motion_policy.max_speed} units/s · command cap {status.motion_policy.max_command_step} · {status.motion_policy.controller_hz} Hz</p></details>}
    {status?.memory && <p className="harness-memory">{status.memory.demonstrations} demos · {status.memory.experiences} worked</p>}
    {status?.plan && !status.plan.direct && <div className="harness-plan">
      <strong>{status.plan.description}</strong><p>{status.plan.reason}</p>
      <p>~{Math.ceil(status.plan.duration)}s · use it in 5 min</p>
      {status.plan.policy && <p>Replay speed {status.plan.speed} units/s from Keewano run history: {status.plan.policy.reason}</p>}
      {status.plan.source === "dial_sms" && <p role="status">{awaitingSMS ? "Waiting for a text." : "They said yes. Check the table."}</p>}
      {status.plan.source === "dial_voice" && <p role="status">Requested by the verified Dial caller. Review the plan and workcell here before choosing whether to run.</p>}
      <details><summary>Match this start pose</summary><div className="motor-grid">{Object.entries(status.plan.start_pose).map(([joint, value]) => <span key={joint}>{joint.replace(".pos", "")}<b>{value.toFixed(1)}</b></span>)}</div><p>Get within 5 of these numbers.</p></details>
      <label className="harness-check"><input type="checkbox" checked={confirmed} onChange={e => setConfirmedPlanId(e.target.checked ? status.plan!.id : "")} />Same setup as the demo. Path is clear. I&apos;m at the stop.</label>
      <button className="primary-button" disabled={!!active || !confirmed || !motorsFresh || awaitingSMS} onClick={() => void command("run", { plan_id: status.plan!.id, scene_confirmed: confirmed })}>Run</button>
    </div>}
    {status?.run && <div className="harness-result" aria-live="polite">
      <div className="harness-actions"><strong>{status.run.state.replaceAll("_", " ")}{status.run.result ? ` · ${status.run.result}` : ""}</strong></div>
      {status.motion.active && <progress max={status.motion.duration_s ?? 1} value={status.motion.elapsed_s} />}
      {status.run.execution_plan && <p>Selected: {status.run.execution_plan.description} · replay ~{Math.ceil(status.run.execution_plan.duration)}s</p>}
      {status.run.execution_plan?.execution_mode === "historical_telemetry_turns" && <p>Following saved motor telemetry in 2-second turns · {status.run.execution_plan.historical_context?.recent_runs.length ?? 0} previous runs loaded{latestTurn ? ` · turn ${latestTurn.turn} checked at ${latestTurn.replay_t?.toFixed(1)}s` : " · first turn"}</p>}
      {status.run.execution_plan?.execution_mode === "copied_measured_trajectory" && <p>Copying recorded shoulder, pan, elbow, wrist, and gripper positions · continuous replay with live tracking</p>}
      {status.run.execution_plan?.copy_audit && <p>{status.run.execution_plan.copy_audit.unchanged_values.toLocaleString()} / {status.run.execution_plan.copy_audit.joint_values.toLocaleString()} motor values copied exactly · {status.run.execution_plan.copy_audit.bounded_values} endpoint values bounded</p>}
      {failure && <p className="harness-alert" role="alert">{failure}</p>}
      {(lastEvent?.error || lastEvent?.reason || lastEvent?.evidence) && <p>{lastEvent.error ?? lastEvent.reason ?? lastEvent.evidence}</p>}
      {status.run.verification && <p>Camera: {status.run.verification.evidence} ({Math.round(status.run.verification.confidence * 100)}%). Look yourself.</p>}
      {status.run.verification && <div className="harness-evidence">{["before", "after"].map(frame => <figure key={frame}><Image src={`/api/bridge/harness/evidence?run_id=${status.run!.id}&frame=${frame}`} alt={`${frame} task camera evidence`} width={640} height={480} unoptimized /><figcaption>{frame === "before" ? "Before" : "After"}</figcaption></figure>)}</div>}
      <details><summary>Details</summary><ol>{status.run.events.map((event, i) => <li key={i}>{event.event.replaceAll("_", " ")}{event.turn != null ? ` · turn ${event.turn} · ${event.replay_t?.toFixed(1)}s · ${event.decision?.replaceAll("_", " ")}` : ""}{event.tool ? ` · ${event.tool}` : ""}{event.error ? `: ${event.error}` : ""}{event.reply && <p>{event.reply}</p>}{event.result != null && <pre>{JSON.stringify(event.result, null, 2)}</pre>}</li>)}</ol><small>Run {status.run.id}</small></details>
    </div>}
    {error && <p className="harness-alert" role="alert">{error}</p>}
    {notice && !status?.run?.result && <p className="harness-notice" role="status">{notice}</p>}
    <details className="harness-setup" open={setupOpen} onToggle={event => setSetupOpen(event.currentTarget.open)}><summary>Add a demo</summary><p>Find a saved take from your task, review it, then approve the example for Codex.</p>
      <div className="harness-inputs"><label>Episode ID<input type="number" min={0} value={episode} onChange={e => { setEpisode(Number(e.target.value)); setReviewed(false); }} /></label><label>What it did<input value={description} maxLength={200} onChange={e => setDescription(e.target.value)} /></label><button className="quiet-button" disabled={!!active || !prompt.trim()} onClick={async () => { setReviewed(false); const result = await command("find_episode", { query: prompt }); if (result && Number.isInteger(result.episode)) { setEpisode(result.episode); setDescription(result.description); } }}>Find for task</button><button className="quiet-button" disabled={!!active} onClick={() => { setReviewed(false); void command("import", { episode, description }); }}>Import ID</button></div>
      {currentSkill && <div className="harness-plan"><strong>{currentSkill.description}</strong><p>{currentSkill.conversion}. ~{Math.ceil(currentSkill.duration)}s.</p><p>{currentSkill.approved ? "Approved." : "Not yet."}</p>{!currentSkill.approved && <><label className="harness-check"><input type="checkbox" checked={reviewed} onChange={e => setReviewed(e.target.checked)} />I watched it work. I&apos;ll watch the first run.</label><button className="quiet-button" disabled={!!active || !reviewed} onClick={() => void command("approve", { skill_id: currentSkill.id, reviewed })}>Approve</button></>}</div>}
    </details>
    <div className="harness-actions harness-footer"><span>Replay cap {status?.motion_policy?.max_speed ?? 6} units/s. Hold the arm when it stops.</span><button className="estop-button" disabled={offline} onClick={() => void command("stop", {})}>STOP</button></div>
  </section>;
}
