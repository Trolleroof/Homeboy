"use client";

import { useCallback, useEffect, useState } from "react";
import Image from "next/image";

type Skill = { id: string; description: string; approved: boolean; duration: number; conversion: string; episode: number };
type Harness = {
  motion_policy: { max_speed: number; max_command_step: number; controller_hz: number };
  active: boolean; model: string; model_ready: boolean; vision_model: string; vision_ready: boolean; memory: { backend: string; experiences: number; demonstrations: number }; skills: Skill[];
  motors: { positions: Record<string, number>; age_s: number };
  motion: { active: boolean; elapsed_s: number; duration_s: number | null };
  plan: { id: string; description: string; reason: string; duration: number; start_pose: Record<string, number>; source?: string; sms_confirmed?: boolean } | null;
  run: { id: string; state: string; local_calls: number; codex_calls: number; result: string | null; events: { event: string; at: number; error?: string; reason?: string; evidence?: string; tool?: string; result?: unknown; arguments?: unknown; reply?: string; usage?: { total: { inputTokens: number; outputTokens: number; cachedInputTokens: number } } }[]; verification?: { evidence: string; confidence: number } } | null;
};

export default function HarnessPanel() {
  const [status, setStatus] = useState<Harness | null>(null);
  const [prompt, setPrompt] = useState("");
  const [episode, setEpisode] = useState(2);
  const [description, setDescription] = useState("Pick up the orange block and place it down");
  const [reviewed, setReviewed] = useState(false);
  const [confirmedPlanId, setConfirmedPlanId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [offline, setOffline] = useState(false);
  const [connectionError, setConnectionError] = useState("");
  const refresh = useCallback(async () => {
    try {
      const response = await fetch("/api/bridge/harness/status", { cache: "no-store" });
      const body = await response.json();
      if (!response.ok || body.offline || body.ok === false) {
        setOffline(true); setStatus(null);
        setConnectionError(body.calibrating ? "Calibration in progress. Finish or cancel it before connecting." : body.startup_error ?? (body.starting ? "Connecting…" : "Offline. Hit Connect."));
        return;
      }
      setStatus(body); setOffline(false); setConnectionError("");
    } catch { setOffline(true); setStatus(null); setConnectionError("Can't reach the robot."); }
  }, []);
  useEffect(() => {
    const first = window.setTimeout(() => void refresh(), 0);
    const timer = window.setInterval(() => void refresh(), 1500);
    return () => { window.clearTimeout(first); window.clearInterval(timer); };
  }, [refresh]);
  async function command(action: string, body: Record<string, unknown>) {
    setBusy(true); setError("");
    try {
      const response = await fetch(`/api/bridge/harness/${action}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.error ?? "That didn't work");
      await refresh();
    } catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  }
  const active = busy || status?.active;
  const currentSkill = status?.skills.find(s => s.episode === episode);
  const lastEvent = status?.run?.events.at(-1);
  const motorsFresh = status && status.motors.age_s < .5;
  const confirmed = !!status?.plan && confirmedPlanId === status.plan.id;
  const awaitingSMS = status?.plan?.source === "dial_sms" && !status.plan.sms_confirmed;
  return <section className="card harness-card" id="prompt">
    <div className="harness-heading"><div><h2>Tell it what to do.</h2></div><span className={`connection-pill ${motorsFresh ? "connected" : ""}`}><span className="pulse" />{motorsFresh ? "Arm live" : "No robot"}</span></div>
    {offline && <p className="harness-alert">{connectionError}</p>}
    {offline && <div className="harness-actions"><span>Plug in both arms.</span><button className="quiet-button" disabled={!!active || !!status?.motion.active} onClick={async () => {
      setBusy(true); setError("");
      try {
        const response = await fetch("/api/bridge/daemon/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ task: description }) });
        const body = await response.json();
        if (!response.ok || body.ok === false) throw new Error(body.error ?? "Couldn't connect.");
      } catch (reason) { setError((reason as Error).message); }
      finally { setBusy(false); void refresh(); }
    }}>Connect</button></div>}
    <form onSubmit={event => { event.preventDefault(); setConfirmedPlanId(""); void command("plan", { prompt }); }}>
      <label className="field-label" htmlFor="robot-prompt">What should it do?</label>
      <textarea id="robot-prompt" rows={2} maxLength={500} placeholder="Orange block into the bin" value={prompt} onChange={event => setPrompt(event.target.value)} disabled={!!active} />
      <div className="harness-actions"><span>{!status ? "" : !status.model_ready ? "Planner off" : status.skills.some(s => s.approved) ? "Ready" : "Teach one first"}</span><button className="primary-button" disabled={!!active || !prompt.trim() || !status?.model_ready || !status.skills.some(s => s.approved)}>Plan</button></div>
    </form>
    {status?.memory && <p className="harness-memory">{status.memory.demonstrations} demos · {status.memory.experiences} worked</p>}
    {status?.plan && <div className="harness-plan">
      <strong>{status.plan.description}</strong><p>{status.plan.reason}</p>
      <p>~{Math.ceil(status.plan.duration)}s · use it in 5 min</p>
      {status.plan.source === "dial_sms" && <p role="status">{awaitingSMS ? "Waiting for a text." : "They said yes. Check the table."}</p>}
      <details><summary>Match this start pose</summary><div className="motor-grid">{Object.entries(status.plan.start_pose).map(([joint, value]) => <span key={joint}>{joint.replace(".pos", "")}<b>{value.toFixed(1)}</b></span>)}</div><p>Get within 5 of these numbers.</p></details>
      <label className="harness-check"><input type="checkbox" checked={confirmed} onChange={e => setConfirmedPlanId(e.target.checked ? status.plan!.id : "")} />Same setup as the demo. Path is clear. I&apos;m at the stop.</label>
      <button className="primary-button" disabled={!!active || !confirmed || !motorsFresh || awaitingSMS} onClick={() => void command("run", { plan_id: status.plan!.id, scene_confirmed: confirmed })}>Run</button>
    </div>}
    {status?.run && <div className="harness-result" aria-live="polite">
      <div className="harness-actions"><strong>{status.run.state.replaceAll("_", " ")}{status.run.result ? ` · ${status.run.result}` : ""}</strong></div>
      {status.motion.active && <progress max={status.motion.duration_s ?? 1} value={status.motion.elapsed_s} />}
      {(lastEvent?.error || lastEvent?.reason || lastEvent?.evidence) && <p>{lastEvent.error ?? lastEvent.reason ?? lastEvent.evidence}</p>}
      {status.run.verification && <p>Camera: {status.run.verification.evidence} ({Math.round(status.run.verification.confidence * 100)}%). Look yourself.</p>}
      {status.run.verification && <div className="harness-evidence">{["before", "after"].map(frame => <figure key={frame}><Image src={`/api/bridge/harness/evidence?run_id=${status.run!.id}&frame=${frame}`} alt={`${frame} task camera evidence`} width={640} height={480} unoptimized /><figcaption>{frame === "before" ? "Before" : "After"}</figcaption></figure>)}</div>}
      <details><summary>Details</summary><ol>{status.run.events.map((event, i) => <li key={i}>{event.event.replaceAll("_", " ")}{event.tool ? ` · ${event.tool}` : ""}{event.error ? `: ${event.error}` : ""}{event.reply && <p>{event.reply}</p>}{event.result != null && <pre>{JSON.stringify(event.result, null, 2)}</pre>}</li>)}</ol><small>Run {status.run.id}</small></details>
    </div>}
    {error && <p className="harness-alert" role="alert">{error}</p>}
    <details className="harness-setup"><summary>Add a demo</summary><p>Import a good take. Approve it. Moved the bin? Teach it again.</p>
      <div className="harness-inputs"><label>Take #<input type="number" min={2} value={episode} onChange={e => { setEpisode(Number(e.target.value)); setReviewed(false); }} /></label><label>What it did<input value={description} maxLength={200} onChange={e => setDescription(e.target.value)} /></label><button className="quiet-button" disabled={!!active} onClick={() => { setReviewed(false); void command("import", { episode, description }); }}>Import</button></div>
      {currentSkill && <div className="harness-plan"><strong>{currentSkill.description}</strong><p>{currentSkill.conversion}. ~{Math.ceil(currentSkill.duration)}s.</p><p>{currentSkill.approved ? "Approved." : "Not yet."}</p>{!currentSkill.approved && <><label className="harness-check"><input type="checkbox" checked={reviewed} onChange={e => setReviewed(e.target.checked)} />I watched it work. I&apos;ll watch the first run.</label><button className="quiet-button" disabled={!!active || !reviewed} onClick={() => void command("approve", { skill_id: currentSkill.id, reviewed })}>Approve</button></>}</div>}
    </details>
    <div className="harness-actions harness-footer"><span>Goes slow (max {status?.motion_policy?.max_speed ?? 3}). Hold the arm when it stops.</span><button className="estop-button" disabled={offline} onClick={() => void command("stop", {})}>STOP</button></div>
  </section>;
}
