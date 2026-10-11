"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import type { CalibrationStatus, Role } from "@/lib/calibrate";

export default function CalibrationDialog({ onClose }: { onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [status, setStatus] = useState<CalibrationStatus | null>(null);
  const [role, setRole] = useState<Role>("leader");
  const [selecting, setSelecting] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const poll = useCallback(async () => {
    try {
      const response = await fetch("/api/calibrate/status", { cache: "no-store" });
      if (!response.ok) throw new Error("Calibration status unavailable.");
      setStatus(await response.json());
    } catch (reason) { setError((reason as Error).message); }
  }, []);
  useEffect(() => {
    dialog.current?.showModal();
    const first = setTimeout(() => void poll(), 0);
    const timer = setInterval(() => void poll(), 500);
    return () => { clearTimeout(first); clearInterval(timer); };
  }, [poll]);
  async function post(action: "start" | "advance" | "cancel") {
    setBusy(true); setError("");
    try {
      const response = await fetch(`/api/calibrate/${action}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ role }) });
      const next = await response.json();
      if (!response.ok) throw new Error(next.error ?? "Calibration failed.");
      setStatus(next); setSelecting(false);
    } catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  }
  const active = !!status?.running;
  const select = selecting && !active;
  const phase = status?.phase;
  const label = (active || !select ? status?.role : role) === "leader" ? "Leader" : "Follower";
  const done = !select && phase === "done";
  const failed = !select && phase === "failed";
  const waiting = !select && (phase === "starting" || phase === "saving");
  const step = select ? 0 : phase === "range" ? 2 : done ? 3 : 1;
  return <dialog className="calibration-dialog" ref={dialog} aria-labelledby="calibration-title" onCancel={event => { event.preventDefault(); if (!active && !busy) onClose(); }}>
    <header><div><div className="eyebrow">LEROBOT SETUP</div><h2 id="calibration-title">Calibrate your arms</h2></div><button className="quiet-button" aria-label="Close calibration" disabled={active || busy} onClick={onClose}>✕</button></header>
    <ol className="calibration-steps">{["Choose arm", "Set home", "Sweep joints", "Saved"].map((name, index) => <li key={name} className={index <= step ? "reached" : ""} aria-current={index === step ? "step" : undefined}><span>{index + 1}</span>{name}</li>)}</ol>
    {select ? <><h3>Which arm do you want to calibrate?</h3><div className="calibration-choices">{(["leader", "follower"] as const).map(arm => <button key={arm} aria-pressed={role === arm} className={role === arm ? "selected" : ""} onClick={() => setRole(arm)}><strong>{arm === "leader" ? "Leader" : "Follower"}</strong><span>{arm === "leader" ? "The arm you move by hand" : "The arm that follows"}</span></button>)}</div><p>Support both arms in a stable resting position. Next stops the bridge and runs LeRobot calibration for the selected arm. Camera previews and the follower model pause until you reconnect.</p></> : <>
      <h3>{done ? `${label} calibration saved` : failed ? "Calibration needs attention" : phase === "home" ? `Center the ${label.toLowerCase()}` : phase === "range" ? `Sweep the ${label.toLowerCase()} joints` : `Preparing the ${label.toLowerCase()}`}</h3>
      <p>{done ? "Calibrate the other arm if needed. Then match the arm poses and reconnect the bridge to use the new calibration." : phase === "home" ? "The selected arm’s torque is off. Move each joint to the middle of its travel, then click Next to set home." : phase === "range" ? "Move each joint gently through its full travel, including opening and closing the gripper. Reach both ends before saving. LeRobot handles wrist roll as a full turn." : phase === "saving" ? "LeRobot is writing the calibration file…" : phase === "starting" ? "Loading LeRobot and connecting to the selected arm…" : status?.error}</p>
      {phase === "range" && <div className="calibration-joints">{status?.joints.map(joint => <div key={joint.name}><strong>{joint.name.replaceAll("_", " ")}</strong><span>{joint.min} → {joint.max}</span><b className={joint.max - joint.min >= 800 ? "complete" : ""}>{joint.max - joint.min >= 800 ? "Range captured" : "Keep sweeping"}</b></div>)}</div>}
      {phase === "range" && <p className="calibration-note">If you cancel after setting home, calibrate again before using this arm.</p>}
      {done && <p className="calibration-saved">Saved to <code>{status?.savedTo}</code></p>}
      {status?.command && <details className="calibration-log"><summary>LeRobot command & output</summary><code>{status.command}</code><pre>{status.log.join("\n")}</pre></details>}
    </>}
    {(error || (!select && status?.error)) && <p role="alert" className="calibration-error">{error || status?.error}</p>}
    <footer>{active ? <button className="quiet-button" disabled={busy || phase === "saving"} onClick={() => void post("cancel")}>Cancel calibration</button> : <button className="quiet-button" disabled={busy} onClick={onClose}>Close</button>}
      {done ? <button className="primary-button" onClick={() => { setRole(status?.role === "leader" ? "follower" : "leader"); setSelecting(true); }}>Next · other arm</button> : failed ? <button className="primary-button" onClick={() => setSelecting(true)}>Try again</button> : <button className="primary-button" disabled={busy || waiting || !status || (!select && phase === "range" && !status.canFinish)} onClick={() => void post(select ? "start" : "advance")}>{busy || waiting ? "Please wait…" : phase === "range" && !select ? "Next · save calibration" : "Next"}</button>}
    </footer>
  </dialog>;
}
