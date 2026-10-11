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
  const refresh = useCallback(async () => {
    try {
      const response = await fetch("/api/bridge/harness/status", { cache: "no-store" });
      const body = await response.json();
      if (!response.ok) { setOffline(true); return; }
      setStatus(body); setOffline(false);
    } catch { setOffline(true); }
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
      if (!response.ok || !result.ok) throw new Error(result.error ?? "Robot command failed");
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
    <div className="card-topline"><span className="step-tag">PROMPT → ACTION → CHECK</span><span className="harness-cost">CODEX PLANNING · LOCAL MOTOR CONTROL</span></div>
    <div className="harness-heading"><div><h2>Tell Homeboy what to do.</h2><p>Codex retrieves a skill from vector memory and builds a motor program. Homeboy runs it locally, checks the camera, and remembers verified successes.</p></div><span className={`connection-pill ${motorsFresh ? "connected" : ""}`}><span className="pulse" />{motorsFresh ? "Motor readings live" : "Robot unavailable"}</span></div>
    {offline && <p className="harness-alert">The prompt controller is offline. Start the bridge below to import skills and plan tasks.</p>}
    <div className="harness-actions"><span>Reconnect and power both arms for physical execution.</span><button className="quiet-button" disabled={!!active || !!status?.motion.active} onClick={async () => {
      setBusy(true); setError("");
      try {
        const response = await fetch("/api/bridge/daemon/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ task: description }) });
        const body = await response.json();
        if (!response.ok || body.ok === false) throw new Error(body.error ?? "Could not start bridge");
      } catch (reason) { setError((reason as Error).message); }
      finally { setBusy(false); void refresh(); }
    }}>Connect bridge</button></div>
    <form onSubmit={event => { event.preventDefault(); setConfirmedPlanId(""); void command("plan", { prompt }); }}>
      <label className="field-label" htmlFor="robot-prompt">Your request</label>
      <textarea id="robot-prompt" rows={2} maxLength={500} placeholder="Pick up the orange block and place it in the bin…" value={prompt} onChange={event => setPrompt(event.target.value)} disabled={!!active} />
      <div className="harness-actions"><span>{status?.model ?? "Codex"} · {status?.model_ready ? "CLI installed" : "CLI unavailable"} · {status?.vision_ready ? "local vision ready" : "local vision unavailable"}</span><button className="primary-button" disabled={!!active || !prompt.trim() || !status?.model_ready || !status.skills.some(s => s.approved)}>Plan task ↗</button></div>
      {status && !status.skills.some(s => s.approved) && <p>Import and approve a successful demonstration below to enable planning. Include the block&apos;s color and destination in your request.</p>}
    </form>
    {status?.memory && <p className="harness-memory">{status.memory.backend} · {status.memory.demonstrations} demonstrations · {status.memory.experiences} verified experiences. Codex uses your account allowance; motor ticks and vector search run locally.</p>}
    {status?.plan && <div className="harness-plan">
      <strong>{status.plan.description}</strong><p>{status.plan.reason}</p>
      <p>One bounded routine · approximately {Math.ceil(status.plan.duration)} seconds · plan expires in 5 minutes.</p>
      {status.plan.source === "dial_sms" && <p role="status">{awaitingSMS ? "Waiting for the customer's RUN confirmation by text." : "Customer confirmed by text. Check the workcell below before running."}</p>}
      <details><summary>Align the follower to its starting pose</summary><div className="motor-grid">{Object.entries(status.plan.start_pose).map(([joint, value]) => <span key={joint}>{joint.replace(".pos", "")}<b>{value.toFixed(1)}</b></span>)}</div><p>Use the taught starting pose. The controller refuses a difference above 5 normalized units.</p></details>
      <label className="harness-check"><input type="checkbox" checked={confirmed} onChange={e => setConfirmedPlanId(e.target.checked ? status.plan!.id : "")} />The block, bin and camera are in the taught positions; the path is clear and I am at the stop.</label>
      <button className="primary-button" disabled={!!active || !confirmed || !motorsFresh || awaitingSMS} onClick={() => void command("run", { plan_id: status.plan!.id, scene_confirmed: confirmed })}>Run on robot ↗</button>
    </div>}
    {status?.run && <div className="harness-result" aria-live="polite">
      <div className="harness-actions"><strong>{status.run.state.replaceAll("_", " ")}{status.run.result ? ` · ${status.run.result}` : ""}</strong><span>{status.run.codex_calls} Codex turn(s) · {status.run.local_calls} local vision calls</span></div>
      {status.motion.active && <progress max={status.motion.duration_s ?? 1} value={status.motion.elapsed_s} />}
      {(lastEvent?.error || lastEvent?.reason || lastEvent?.evidence) && <p>{lastEvent.error ?? lastEvent.reason ?? lastEvent.evidence}</p>}
      {status.run.verification && <p>Camera assessment: {status.run.verification.evidence} Confidence reported by the model: {Math.round(status.run.verification.confidence * 100)}%. Inspect the physical result before relying on this check.</p>}
      {status.run.verification && <div className="harness-evidence">{["before", "after"].map(frame => <figure key={frame}><Image src={`/api/bridge/harness/evidence?run_id=${status.run!.id}&frame=${frame}`} alt={`${frame} task camera evidence`} width={640} height={480} unoptimized /><figcaption>{frame === "before" ? "Before the task" : "After the task"}</figcaption></figure>)}</div>}
      <details><summary>Tools and run evidence</summary><ol>{status.run.events.map((event, i) => <li key={i}>{event.event.replaceAll("_", " ")}{event.tool ? ` · ${event.tool}` : ""}{event.error ? `: ${event.error}` : ""}{event.reply && <p>{event.reply}</p>}{event.result != null && <pre>{JSON.stringify(event.result, null, 2)}</pre>}{event.usage && <small>Codex usage: {event.usage.total.inputTokens.toLocaleString()} input tokens · {event.usage.total.cachedInputTokens.toLocaleString()} cached · {event.usage.total.outputTokens.toLocaleString()} output</small>}</li>)}</ol><small>Run {status.run.id}</small></details>
    </div>}
    {error && <p className="harness-alert" role="alert">{error}</p>}
    <details className="harness-setup"><summary>Set up an executable taught skill</summary><p>Teach and save a successful demonstration in the teaching panel below. Import its episode, review the conversion, then approve it. Diagnostic episodes cannot execute. A changed block or bin position requires another demonstration.</p>
      <div className="harness-inputs"><label>Episode<input type="number" min={2} value={episode} onChange={e => { setEpisode(Number(e.target.value)); setReviewed(false); }} /></label><label>What did it successfully do?<input value={description} maxLength={200} onChange={e => setDescription(e.target.value)} /></label><button className="quiet-button" disabled={!!active} onClick={() => { setReviewed(false); void command("import", { episode, description }); }}>Import motion</button></div>
      {currentSkill && <div className="harness-plan"><strong>{currentSkill.description}</strong><p>{currentSkill.conversion}. Duration with speed limiting: {Math.ceil(currentSkill.duration)} seconds.</p><p>{currentSkill.approved ? "Approved for this fixed workcell." : "Imported for review; execution is disabled."}</p>{!currentSkill.approved && <><label className="harness-check"><input type="checkbox" checked={reviewed} onChange={e => setReviewed(e.target.checked)} />I watched this demonstration succeed and reviewed any clamping shown above. I will supervise its first replay.</label><button className="quiet-button" disabled={!!active || !reviewed} onClick={() => void command("approve", { skill_id: currentSkill.id, reviewed })}>Approve this skill</button></>}</div>}
    </details>
    <div className="harness-actions harness-footer"><span>Slow replay: at most {status?.motion_policy?.max_speed ?? 3} normalized joint units/s · small bounded steps.<br />Fixed workcell · one routine per prompt · uncertain camera checks stay uncertain.<br />At the end, torque releases at the final pose; support the arm as needed.</span><button className="estop-button" disabled={offline} onClick={() => void command("stop", {})}>STOP ROBOT</button></div>
  </section>;
}
