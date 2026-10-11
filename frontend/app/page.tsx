"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import HarnessPanel from "./harness-panel";
import CalibrationDialog from "./calibration-dialog";
import FollowerView from "./follower-view";
import { recorderWaitError, teleopReady } from "@/lib/recorder-feedback";

type RecorderStatus = {
  state: string;
  repo_id: string;
  task: string;
  frames: number;
  elapsed_s: number;
  started_at?: string | null;
  stopped_at?: string | null;
  saved_at?: string | null;
  saved_episodes: number;
  offline?: boolean;
  message?: string;
  teleop?: { ready?: boolean; engaged?: boolean; record_ready?: boolean; worst?: number; worst_joint?: string };
  hardware?: { teleop?: { motors_ok?: number; motors_total?: number }; follower?: { motors_ok?: number; motors_total?: number } };
  data_quality?: { ready?: boolean; issues?: string[]; camera_streaming?: Record<string, boolean> };
  estop?: { engaged?: boolean; reason?: string };
  calibration?: Record<string, unknown> | null;
  autonomy?: { active?: boolean };
  delta?: {
    joints?: Record<string, { follower: number; leader: number; delta: number }>;
    max?: number;
    limit?: number;
  };
  motors?: { positions?: Record<string, number>; age_s?: number | null };
};

type CameraView = {
  camera: string;
  video_path?: string;
  video_from?: number;
  video_to?: number;
  video_url?: string;
  caption?: string | null;
  sampled_frames?: number;
};

type Skill = {
  repo_id: string;
  episode_index: number;
  description: string;
  score: number;
  video_url: string;
  match_source?: string;
  sampled_frames?: number;
  caption?: string;
  camera_views?: CameraView[];
};

type SavedSkill = {
  repo_id: string;
  episode_index: number;
  description: string;
  video_path: string;
  video_from: number;
  video_to: number;
  created_at: string;
  video_indexed?: boolean | number;
  video_model?: string;
  caption?: string;
  camera_views?: CameraView[];
};

const EMPTY: RecorderStatus = {
  state: "idle", repo_id: "", task: "", frames: 0, elapsed_s: 0, saved_episodes: 0, offline: true,
};

export default function Home() {
  const [status, setStatus] = useState<RecorderStatus>(EMPTY);
  const [description, setDescription] = useState("Move the block into the bin");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Skill[]>([]);
  const [searchMode, setSearchMode] = useState("");
  const [searchMessage, setSearchMessage] = useState("");
  const [searchRunId, setSearchRunId] = useState("");
  const [indexing, setIndexing] = useState("");
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  const [deleting, setDeleting] = useState("");
  const [library, setLibrary] = useState<SavedSkill[]>([]);
  const [busy, setBusy] = useState(false);
  const [stopping, setStopping] = useState(false);
  const teachingAbort = useRef<AbortController | null>(null);
  const [calibrationOpen, setCalibrationOpen] = useState(false);
  const [demoOpen, setDemoOpen] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [forcedCameras, setForcedCameras] = useState<Record<string, boolean>>({});
  const [feedReloads, setFeedReloads] = useState<Record<string, number>>({});
  const [overheadIndex, setOverheadIndex] = useState(0);
  const [cameraSaving, setCameraSaving] = useState(false);
  const [cameraNotice, setCameraNotice] = useState("");
  const [cameraConfigLoaded, setCameraConfigLoaded] = useState(false);
  const savedCount = useRef<number | null>(null);
  const skillToIndex = useRef<string | null>(null);

  const refreshLibrary = useCallback(async () => {
    try {
      const response = await fetch("/api/bridge/skills", { cache: "no-store" });
      if (!response.ok) return;
      const body = await response.json() as { skills?: SavedSkill[] };
      setLibrary(body.skills ?? []);
    } catch { /* the bridge service may still be starting */ }
  }, []);

  const indexVideo = useCallback(async (repoId: string, episodeIndex: number) => {
    setIndexing(`${repoId}:${episodeIndex}`); setError("");
    setNotice("Indexing in the background. You can record another take.");
    try {
      const response = await fetch("/api/skills", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "index_video", repo_id: repoId, episode_index: episodeIndex }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error ?? "Video content could not be indexed.");
      setNotice("Searchable now.");
      await refreshLibrary();
    } catch (reason) { setError((reason as Error).message); }
    finally { setIndexing(""); }
  }, [refreshLibrary]);

  const deleteTake = useCallback(async (repoId: string, episodeIndex: number) => {
    const key = `${repoId}:${episodeIndex}`;
    if (pendingDelete !== key) {
      setPendingDelete(key);
      setError("");
      return;
    }
    setDeleting(key);
    setError("");
    try {
      const response = await fetch("/api/bridge/skills", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "delete", repo_id: repoId, episode_index: episodeIndex }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error ?? "Could not delete that take.");
      setPendingDelete(null);
      setResults((current) => current.filter((skill) => !(skill.repo_id === repoId && skill.episode_index === episodeIndex)));
      await refreshLibrary();
    } catch (reason) { setError((reason as Error).message); }
    finally { setDeleting(""); }
  }, [pendingDelete, refreshLibrary]);

  const refreshStatus = useCallback(async () => {
    try {
      const response = await fetch("/api/bridge/recorder/status", { cache: "no-store" });
      const next = await response.json() as RecorderStatus;
      if (!response.ok || next.offline) {
        setStatus((current) => ({ ...current, offline: true }));
        return false;
      }
      setStatus(next);
      if (next.message?.startsWith("save failed:") || next.message?.startsWith("nothing to save")) {
        setNotice("");
        setError(next.message);
      }
      if (savedCount.current === null) savedCount.current = next.saved_episodes;
      else if (next.saved_episodes < savedCount.current) {
        // A restarted writer or library deletion can reduce the saved count.
        // Reset the baseline so the very next take is still registered.
        savedCount.current = next.saved_episodes;
        skillToIndex.current = null;
      }
      else if (next.state === "idle" && next.saved_at && next.saved_episodes > savedCount.current) {
        savedCount.current = next.saved_episodes;
        const skill = skillToIndex.current;
        skillToIndex.current = null;
        if (skill && next.repo_id) {
          void fetch("/api/bridge/skills", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "teach", repo_id: next.repo_id, episode_index: next.saved_episodes - 1, description: skill }),
          }).then(async (res) => {
            const body = await res.json();
            if (!res.ok) throw new Error(body.error ?? "Saved, but search indexing failed.");
            setNotice("Saved. Making it searchable in the background…");
            void refreshLibrary();
            await indexVideo(next.repo_id, next.saved_episodes - 1);
          }).catch((reason: unknown) => setError((reason as Error).message));
        }
      }
      return true;
    } catch {
      setStatus((current) => ({ ...current, offline: true }));
      return false;
    }
  }, [refreshLibrary, indexVideo]);

  useEffect(() => {
    let cancelled = false;
    let timer = 0;
    const loop = async () => {
      const online = await refreshStatus();
      if (cancelled) return;
      timer = window.setTimeout(loop, online ? 900 : 5000);
    };
    void refreshLibrary();
    void loop();
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [refreshStatus, refreshLibrary]);

  useEffect(() => {
    if (!demoOpen) return;
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") setDemoOpen(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [demoOpen]);

  useEffect(() => {
    let cancelled = false;
    void fetch("/api/bridge/config/cameras", { cache: "no-store" }).then((response) => response.json()).then((body: { cameras?: Array<{ name: string; index: number }> }) => {
      const overhead = body.cameras?.find((camera) => camera.name === "overhead");
      if (!cancelled && overhead) setOverheadIndex(overhead.index);
      if (!cancelled) setCameraConfigLoaded(true);
    }).catch(() => { if (!cancelled) setCameraConfigLoaded(true); });
    return () => { cancelled = true; };
  }, []);

  const call = useCallback(async (endpoint: string, body?: Record<string, unknown>, signal?: AbortSignal) => {
    const response = await fetch(`/api/bridge/${endpoint}`, {
      method: body ? "POST" : "GET",
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      cache: "no-store",
      signal,
    });
    const result = await response.json();
    if (!response.ok || result.ok === false) throw new Error(result.error ?? `Request failed: ${endpoint}`);
    return result;
  }, []);

  async function waitFor(test: (next: RecorderStatus) => boolean, timeoutMs = 90_000, action = "waiting for the recorder", signal?: AbortSignal) {
    const deadline = Date.now() + timeoutMs;
    let last: RecorderStatus | undefined;
    while (Date.now() < deadline) {
      signal?.throwIfAborted();
      const response = await fetch("/api/bridge/recorder/status", { cache: "no-store", signal });
      const next = await response.json() as RecorderStatus;
      last = next;
      if (response.ok && !next.offline) {
        setStatus(next);
        if (test(next)) return next;
        if (next.state === "estopped") throw new Error("Stopped. Look at the arm, then hit Reset.");
        if (next.state === "recording" && next.message?.startsWith("frame error:")) throw new Error(recorderWaitError(next, action));
      }
      if (next.offline) {
        const daemon = await call("daemon/status") as { running: boolean; log: string[] };
        if (!daemon.running) throw new Error(`Didn't start. ${daemon.log.slice(-3).join(" ") || "Plug in both arms and turn them on."}`);
      }
      await new Promise((resolve) => window.setTimeout(resolve, 700));
    }
    signal?.throwIfAborted();
    throw new Error(recorderWaitError(last, action));
  }

  async function startBridge(task: string) {
    const daemon = await call("daemon/status") as { running?: boolean };
    if (daemon.running) return;
    const [arms, cameraConfig] = await Promise.all([call("config/arms"), call("config/cameras")]);
    const result = await call("daemon/start", {
      repoId: "homeboy/speedhacks-wrist-v1",
      task,
      fps: 30,
      robotPort: arms.robotPort,
      teleopPort: arms.teleopPort,
      cameras: cameraConfig.cameras,
      deltaLimit: 25,
      autoEstop: false,
      engageOnStart: false,
      commitSeconds: 6,
    });
    if (result.ok === false && !String(result.error ?? "").includes("already")) throw new Error(result.error ?? "Couldn't start.");
  }

  async function restartBridge() {
    if (recording || pending || status.state === "saving") {
      setError("Stop and save or discard the current take before restarting the bridge.");
      return;
    }
    if (calibrationOpen || status.calibration) {
      setError("Finish or cancel calibration before restarting the bridge.");
      return;
    }
    if (status.estop?.engaged) {
      setError("The emergency stop is active. Inspect the arm and use Reset before restarting the bridge.");
      return;
    }
    if (status.autonomy?.active) {
      setError("Wait for the robot motion to stop before restarting the bridge.");
      return;
    }
    setBusy(true); setError(""); setNotice("Checking the bridge…");
    try {
      const task = status.task || description.trim() || "Camera preview";
      const initialDaemon = await call("daemon/status") as { running?: boolean };
      const wasRunning = !!initialDaemon.running;
      if (wasRunning) {
        const harness = await call("harness/status") as { active?: boolean };
        if (harness.active) throw new Error("Wait for the current harness planning or robot run to finish before restarting the bridge.");
      }
      if (wasRunning && !window.confirm("The arm may jump when it reconnects. Clear the path and keep a hand on STOP before restarting the bridge. Continue?")) {
        setNotice("");
        return;
      }
      if (wasRunning) {
        setNotice("Stopping the bridge…");
        await call("daemon/stop", {});
        const deadline = Date.now() + 10_000;
        while (Date.now() < deadline) {
          const daemon = await call("daemon/status") as { running?: boolean };
          if (!daemon.running) break;
          await new Promise((resolve) => window.setTimeout(resolve, 150));
        }
        const daemon = await call("daemon/status") as { running?: boolean };
        if (daemon.running) throw new Error("The bridge is still shutting down. Check status, then try again.");
      }
      setNotice("Connecting the bridge…");
      await startBridge(task);
      const current = await waitFor((next) => next.state === "idle" && next.data_quality?.ready === true, 30_000);
      setForcedCameras({});
      setNotice(current.teleop?.ready
        ? "Bridge restarted. Check the live joint positions before moving."
        : "Bridge restarted. Check arm alignment and live joint positions before moving.");
    } catch (reason) {
      setError((reason as Error).message);
      setNotice("");
      await refreshStatus();
    } finally { setBusy(false); }
  }

  async function connectCameras() {
    setBusy(true); setError(""); setNotice("Connecting cameras…");
    try {
      if (status.offline) {
        await startBridge(description.trim() || "Camera preview");
        await waitFor((next) => Object.values(next.data_quality?.camera_streaming ?? {}).some(Boolean));
      }
      setForcedCameras({ wrist: true, overhead: true });
      setNotice("");
    } catch (reason) { setNotice(""); setError((reason as Error).message); }
    finally { setBusy(false); }
  }

  async function syncArms() {
    if (!syncAvailable) return;
    setBusy(true); setError(""); setNotice("Syncing the follower slowly; large gaps can take several seconds…");
    try {
      await call("recorder/sync", { confirmed: true });
      await waitFor((next) => next.teleop?.engaged === true && teleopReady(next), 10_000, "finishing arm sync");
      setNotice("Arms synced. Teleop is tracking and ready to record.");
    } catch (reason) {
      setError((reason as Error).message);
      setNotice("");
      await refreshStatus();
    } finally { setBusy(false); }
  }

  async function startTeaching() {
    if (!description.trim()) return;
    const controller = new AbortController();
    teachingAbort.current?.abort();
    teachingAbort.current = controller;
    const { signal } = controller;
    setBusy(true); setError(""); setNotice("");
    try {
      let current = status;
      if (current.offline) {
        setNotice("Connecting…");
        await startBridge(description.trim());
        current = await waitFor((next) => next.state === "idle" && next.data_quality?.ready === true, 90_000, "connecting the bridge", signal);
      }
      current = await call("recorder/status", undefined, signal) as RecorderStatus;
      if (current.state === "estopped") throw new Error("Stopped. Look at the arm, then hit Reset.");
      if (current.state === "recording" || current.state === "saving") throw new Error("Already recording.");
      if (current.state === "pending") throw new Error("Save or toss this one first.");
      if (!current.data_quality?.ready) {
        const issues = current.data_quality?.issues?.join(" · ");
        throw new Error(issues || "Not ready. Check the camera.");
      }
      if (!teleopReady(current)) {
        setNotice("Checking the arms…");
        current = await waitFor((next) => teleopReady(next) && next.data_quality?.ready === true, 8_000, "checking teleop", signal);
      }
      if (!current.teleop?.engaged) {
        setNotice("Linking the arms…");
        await call("recorder/engage", {}, signal);
        current = await waitFor((next) => next.teleop?.record_ready === true, 8_000, "engaging teleop", signal);
      }
      current = await waitFor((next) => next.teleop?.record_ready === true && next.data_quality?.ready === true, 8_000, "checking recording readiness", signal);
      await call("recorder/task", { task: description.trim() }, signal);
      skillToIndex.current = description.trim();
      await call("recorder/record", {}, signal);
      await waitFor((next) => next.state === "recording" && next.frames > 0, 8_000, "starting the recording", signal);
      setNotice("Go. Hit stop when you're done.");
      await refreshStatus();
    } catch (reason) {
      if (signal.aborted) return;
      setError((reason as Error).message);
      setNotice("");
    } finally {
      if (teachingAbort.current === controller) {
        teachingAbort.current = null;
        setBusy(false);
      }
    }
  }

  async function stopTeaching() {
    teachingAbort.current?.abort();
    teachingAbort.current = null;
    setStopping(true);
    setBusy(true); setError("");
    try {
      await call("recorder/stop", {});
      setNotice("Stopped. Save it or toss it.");
      await refreshStatus();
    } catch (reason) { setError((reason as Error).message); }
    finally { setStopping(false); setBusy(false); }
  }

  async function saveTake() {
    setBusy(true); setError("");
    setNotice("Saving…");
    try {
      const result = await call("recorder/save", {}) as { saved?: boolean; episode?: number };
      if (result.saved) setNotice(`Saved take ${(result.episode ?? 0) + 1}.`);
      await refreshStatus();
    }
    catch (reason) { setNotice(""); setError((reason as Error).message); await refreshStatus(); }
    finally { setBusy(false); }
  }

  async function discardTake() {
    setBusy(true); setError("");
    try { await call("recorder/discard", {}); skillToIndex.current = null; setNotice("Tossed."); await refreshStatus(); }
    catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  }

  async function emergencyStop() {
    teachingAbort.current?.abort();
    teachingAbort.current = null;
    setBusy(false);
    setError("");
    try {
      await call("recorder/estop", { reason: "operator stop from Homeboy skillbook" });
      skillToIndex.current = null;
      setNotice("Stopped. Look at the arm, then hit Reset.");
      await refreshStatus();
    } catch (reason) { setError((reason as Error).message); }
  }

  async function searchSkills(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!query.trim()) return;
    setBusy(true); setError("");
    try {
      const response = await fetch("/api/bridge/skills", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "search", query: query.trim(), source: "video" }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error ?? "Search failed.");
      setSearchMode(body.mode);
      setSearchMessage(body.reason ?? "");
      setSearchRunId(body.run_id ?? "");
      setResults((body.results ?? []).map((skill: Skill) => ({ ...skill, video_url: skill.video_url.replace("/api/video", "/api/bridge/video") })));
    } catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  }

  const recording = status.state === "recording";
  const saving = status.state === "saving";
  const pending = status.state === "pending";
  const bridgeOnline = !status.offline;
  const preflightReady = teleopReady(status) && status.data_quality?.ready === true;
  const linked = preflightReady && status.teleop?.engaged === true;
  const syncHint = status.estop?.engaged
    ? "Emergency stop is active. Inspect the arm fault before resetting it."
    : (status.hardware?.teleop?.motors_ok ?? 0) !== 6 || (status.hardware?.follower?.motors_ok ?? 0) !== 6
      ? `Sync locked: leader ${status.hardware?.teleop?.motors_ok ?? 0}/6, follower ${status.hardware?.follower?.motors_ok ?? 0}/6 motors responding. Inspect the fault before syncing.`
      : typeof status.teleop?.worst === "number"
        ? `Largest gap ${status.teleop.worst.toFixed(1)} / 25. Sync moves the follower slowly; large gaps take longer.`
        : "Both arms need fresh readings before syncing.";
  const syncAvailable = bridgeOnline && status.state === "idle" && !status.estop?.engaged
    && !status.calibration && (status.hardware?.teleop?.motors_ok ?? 0) === 6
    && (status.hardware?.follower?.motors_ok ?? 0) === 6 && Object.keys(status.delta?.joints ?? {}).length === 6;
  const shortTime = `${Math.floor(status.elapsed_s / 60).toString().padStart(2, "0")}:${Math.floor(status.elapsed_s % 60).toString().padStart(2, "0")}`;
  const wristStreaming = bridgeOnline && (status.data_quality?.camera_streaming?.wrist === true || forcedCameras.wrist === true);
  const overheadStreaming = bridgeOnline && (status.data_quality?.camera_streaming?.overhead === true || forcedCameras.overhead === true);
  const teachStage = recording ? "recording" : pending ? "save" : linked ? "ready" : bridgeOnline ? "syncing" : "offline";
  const connectionText = !bridgeOnline ? "Offline" : !teleopReady(status) ? "Checking arms" : !status.data_quality?.ready ? "Checking camera" : linked ? "Ready" : "Arms on";

  return (
    <>
    {demoOpen && (
      <div className="demo-backdrop" role="presentation" onClick={() => setDemoOpen(false)}>
        <div className="demo-dialog" role="dialog" aria-modal="true" aria-label="Demo video" onClick={(event) => event.stopPropagation()}>
          <div className="demo-dialog-bar"><strong>Demo</strong><button type="button" className="quiet-button" onClick={() => setDemoOpen(false)}>Close</button></div>
          <video src="/demo.mp4" controls autoPlay playsInline />
        </div>
      </div>
    )}
    <main className="app-shell">
      {calibrationOpen && <CalibrationDialog onClose={() => { setCalibrationOpen(false); void refreshStatus(); }} />}
      <aside className="rail">
        <a className="brand" href="#top" aria-label="Homeboy home"><span className="brand-mark"><span /><span /><span /></span><span>homeboy</span></a>
        <nav className="nav-list" aria-label="Main navigation">
          <a className="nav-item" href="#prompt"><span className="nav-icon">✳</span>Ask</a>
          <a className="nav-item active" href="#teach"><span className="nav-icon">↗</span>Teach</a>
          <a className="nav-item" href="#library"><span className="nav-icon">▧</span>Saved<span className="nav-count">{library.length}</span></a>
          <button type="button" className="nav-item calibration-nav" disabled={busy} onClick={() => setCalibrationOpen(true)}><span className="nav-icon" aria-hidden="true">↻</span>Calibrate</button>
          <button type="button" className="nav-item calibration-nav" onClick={() => setDemoOpen(true)}><span className="nav-icon" aria-hidden="true">▶</span>Demo</button>
        </nav>
        <button className="rail-sync-button" type="button" disabled={!syncAvailable || busy} onClick={() => void syncArms()} title={syncAvailable ? "Move follower to the leader's current pose and engage teleop" : "Sync is available when both arms are connected, idle, and reporting six fresh joints"}>
          <span aria-hidden="true">↻</span><b>{busy && notice.startsWith("Syncing") ? "Syncing arms…" : "Sync arms"}</b>
        </button>
        <div className="rail-spacer" />
      </aside>

      <section className="main-column" id="top">
        <header className="topbar">
          <div className="crumb"><strong>SKILLBOOK</strong></div>
          <div className="topbar-actions"><button type="button" className="quiet-button bridge-restart-button" disabled={busy || recording || pending || !!status.calibration} onClick={() => void restartBridge()} title="Restart the local robot bridge; the arm may jump as it reconnects"><span className="restart-full">Restart bridge</span><span className="restart-short">Restart</span></button>{bridgeOnline && status.state === "estopped" && <button className="quiet-button" onClick={() => { void call("recorder/rearm", {}).then(() => refreshStatus()).catch((reason: Error) => setError(reason.message)); }}>Reset</button>}{bridgeOnline && <button className="estop-button" onClick={() => void emergencyStop()} aria-label="Emergency stop robot">STOP</button>}<div className={`connection-pill ${linked ? "connected" : ""}`}><span className="pulse" />{connectionText}</div></div>
        </header>

        <div className="content">
          <section className="intro" aria-label="Teach it once">
            <div className="intro-copy">
              <h1>Teach it once.</h1>
              <p>Move the leader through the chore. This arm follows.</p>
            </div>
            <div className="intro-art"><FollowerView /></div>
          </section>

          <HarnessPanel />
          <section className="teach-layout" id="teach">
            <aside className="card guide-card" aria-label="Teaching guide">
              <h2>Do this.</h2>
              <div className="guide-steps">
                <div className={`guide-step ${teachStage === "ready" || teachStage === "recording" || teachStage === "save" ? "complete" : "current"}`}><span>1</span><div><strong>Name it</strong></div></div>
                <div className={`guide-step ${teachStage === "recording" ? "current" : teachStage === "save" ? "complete" : ""}`}><span>2</span><div><strong>Move the arm</strong></div></div>
                <div className={`guide-step ${teachStage === "save" ? "current" : ""}`}><span>3</span><div><strong>Save it</strong></div></div>
                <div className="guide-safety"><strong>Clear the path. Hand on the stop.</strong><p>The arm can jump when it links up.</p></div>
              </div>
            </aside>

            <div className="teach-main">
            <article className="card teach-card">
              <div className="card-topline"><span className="step-tag">TEACH</span><span className="tiny-live"><i className={recording ? "active" : ""} />{recording ? "REC" : "READY"}</span></div>
              {bridgeOnline && <div className={`sync-check ${status.teleop?.ready ? "sync-check-ready" : ""}`}><span className="sync-check-led" /><div><strong>{status.teleop?.ready ? "Leader and follower aligned" : status.teleop?.worst_joint ? `${status.teleop.worst_joint.replaceAll("_", " ")} is apart` : "Waiting for fresh arm readings"}</strong><small>{status.teleop?.ready && !status.estop?.engaged && (status.hardware?.teleop?.motors_ok ?? 0) === 6 && (status.hardware?.follower?.motors_ok ?? 0) === 6 ? "Within the 25-unit tracking limit" : syncHint}</small></div><span className="sync-motor-count">L {status.hardware?.teleop?.motors_ok ?? 0}/{status.hardware?.teleop?.motors_total ?? 6} · F {status.hardware?.follower?.motors_ok ?? 0}/{status.hardware?.follower?.motors_total ?? 6}</span></div>}
              <label className="field-label" htmlFor="skill-description">Name it</label>
              <textarea id="skill-description" value={description} onChange={(event) => setDescription(event.target.value)} placeholder="Block into the bin" rows={2} disabled={recording || pending || saving} />
              <div className="teach-bottom">
                <div className="camera-pips"><span className="camera-icon">◉</span><span>Both cameras</span></div>
                {recording ? <button className="primary-button stop-button" onClick={() => void stopTeaching()} disabled={stopping}><span className="stop-square" />{stopping ? "Stopping…" : "Stop"}</button> : pending ? <div className="take-actions"><button className="quiet-button" onClick={() => void discardTake()} disabled={busy}>Toss</button><button className="primary-button" onClick={() => void saveTake()} disabled={busy}>Save <span>→</span></button></div> : <button className="primary-button" onClick={() => void startTeaching()} disabled={busy || saving || !description.trim()}><span className="button-dot" />{saving ? "Saving…" : busy ? "Wait…" : "Start"}<span>→</span></button>}
              </div>
              {(notice || error) && <div className={`feedback ${error ? "error" : ""}`} role="status" aria-live="polite">{error || notice}</div>}
              {(status.started_at || status.stopped_at || status.saved_at) && <div className="recording-timestamps" aria-label="Take timestamps">{status.started_at && <span>Started {new Date(status.started_at).toLocaleString()}</span>}{status.stopped_at && <span>Stopped {new Date(status.stopped_at).toLocaleString()}</span>}{status.saved_at && <span>Saved {new Date(status.saved_at).toLocaleString()}</span>}</div>}
              {recording && <div className="recording-meter"><div className="recording-meter-bar" /><span>{shortTime}</span><span>{status.frames} frames</span></div>}
            </article>

            <section className="camera-section">
              <div className="section-heading"><div><h2>Camera</h2></div></div>
              <div className="camera-grid">
                <figure className="camera-frame wrist-frame"><figcaption><span className={`camera-led ${wristStreaming ? "on" : ""}`} /><strong>WRIST</strong><small>{wristStreaming ? "LIVE" : "WAIT"}</small>{wristStreaming && <button type="button" className="camera-reconnect" onClick={() => setFeedReloads((prev) => ({ ...prev, wrist: (prev.wrist ?? 0) + 1 }))}>Reconnect</button>}</figcaption>{wristStreaming ? <MjpegStream key={`wrist-${feedReloads.wrist ?? 0}`} src="/api/bridge/stream/wrist" alt="Live wrist camera" /> : <div className="camera-placeholder"><CameraMark /><span>{bridgeOnline ? "No picture yet" : "Robot is off"}</span><button type="button" className="camera-connect" disabled={busy} onClick={() => bridgeOnline ? setForcedCameras((prev) => ({ ...prev, wrist: true })) : void connectCameras()}>{bridgeOnline ? "Reconnect feed" : "Connect cameras"}</button></div>}</figure>
                <figure className="camera-frame wrist-frame"><figcaption><span className={`camera-led ${overheadStreaming ? "on" : ""}`} /><strong>OVERHEAD</strong><small>{overheadStreaming ? "LIVE" : "WAIT"}</small>{overheadStreaming && <button type="button" className="camera-reconnect" onClick={() => setFeedReloads((prev) => ({ ...prev, overhead: (prev.overhead ?? 0) + 1 }))}>Reconnect</button>}</figcaption>{overheadStreaming ? <MjpegStream key={`overhead-${feedReloads.overhead ?? 0}`} src="/api/bridge/stream/overhead" alt="Live overhead camera" /> : <div className="camera-placeholder"><CameraMark /><span>{bridgeOnline ? "No picture yet" : "Robot is off"}</span><button type="button" className="camera-connect" disabled={busy} onClick={() => bridgeOnline ? setForcedCameras((prev) => ({ ...prev, overhead: true })) : void connectCameras()}>{bridgeOnline ? "Reconnect feed" : "Connect cameras"}</button></div>}</figure>
              </div>
              {cameraConfigLoaded && <div className="camera-index-control"><label htmlFor="overhead-index">Overhead camera index</label><select id="overhead-index" value={overheadIndex} disabled={bridgeOnline || cameraSaving} onChange={(event) => setOverheadIndex(Number(event.target.value))}>{[0, 1, 2, 3, 4, 5, 6, 7].map((index) => <option key={index} value={index}>{index}{index === 1 ? " · currently used by wrist" : ""}</option>)}</select><button className="quiet-button" disabled={bridgeOnline || cameraSaving} onClick={() => {
                setCameraSaving(true); setCameraNotice("");
                void fetch("/api/bridge/config/cameras", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ overheadIndex }) }).then(async (response) => {
                  const body = await response.json(); if (!response.ok) throw new Error(body.error ?? "Could not save camera mapping.");
                  setCameraNotice(`Saved overhead camera index ${overheadIndex}.`);
                }).catch((reason: Error) => setCameraNotice(reason.message)).finally(() => setCameraSaving(false));
              }}>{cameraSaving ? "Saving…" : "Save index"}</button><span>{cameraNotice || (bridgeOnline ? "Camera map is active; stop and restart the recorder to apply index changes." : "Choose the device index assigned to your overhead camera.")}</span></div>}
            </section>
            </div>
          </section>

          <section className="library-section" id="library">
            <div className="section-heading"><div><h2>Saved</h2></div><span className="episode-count">{library.length}</span></div>
            <form className="search-form" onSubmit={(event) => void searchSkills(event)}><span className="search-glyph">⌕</span><input aria-label="Search saved demonstrations" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="orange cube" /><button type="submit" disabled={busy || !query.trim()}>{busy ? "Searching…" : "Search"}<span>→</span></button></form>
            {searchRunId && <div className="search-foot"><a href={`/api/events?run_id=${encodeURIComponent(searchRunId)}`} target="_blank" rel="noreferrer">Trace</a></div>}
            {results.length > 0 && <div className="results-grid">{results.map((skill) => <article className="result-card" key={`${skill.repo_id}-${skill.episode_index}`}><div className="result-title"><span className="result-icon">↗</span><div><strong className="result-task" title={skill.description}>{skill.description?.trim() || "Untitled take"}</strong><small>{`Take ${skill.episode_index + 1} · ${skill.repo_id}`}</small></div><span className="match-score">{skill.score.toFixed(2)}<small>MATCH</small></span></div>{(skill.camera_views?.length ? skill.camera_views : [{ camera: "observation.images.wrist", video_url: skill.video_url, caption: skill.caption, sampled_frames: skill.sampled_frames }]).map((view) => <div className="saved-camera" key={view.camera}><strong className="saved-camera-label">{cameraName(view.camera)}</strong><video controls preload="metadata" src={view.video_url ?? skill.video_url} /><CaptionDetails taskName={skill.description} caption={view.caption} /></div>)}<div className="result-foot"><span>{`${skill.sampled_frames ?? ""} sampled frames`}</span><span>{String(skill.episode_index + 1).padStart(2, "0")}</span></div></article>)}</div>}
            {searchMode && results.length === 0 && <div className="empty-results"><span>⌕</span><strong>Nothing close.</strong><p>{searchMessage || "Teach one first."}</p></div>}
            {!searchMode && library.length > 0 && <div className="results-grid">{library.map((skill) => <article className="result-card" key={`${skill.repo_id}-${skill.episode_index}`}><div className="result-title"><span className="result-icon">↗</span><div><strong className="result-task" title={skill.description}>{skill.description?.trim() || "Untitled take"}</strong><small>{`Take ${skill.episode_index + 1} · ${skill.repo_id}`}</small></div><span className="library-tag">{skill.video_indexed ? "SEARCHABLE" : "SAVED"}</span></div>{(skill.camera_views?.length ? skill.camera_views : [{ camera: "observation.images.wrist", video_path: skill.video_path, video_from: skill.video_from, caption: skill.caption }]).map((view) => <div className="saved-camera" key={view.camera}><strong className="saved-camera-label">{cameraName(view.camera)}</strong><video controls preload="metadata" src={view.video_url ?? `/api/bridge/video?repo_id=${encodeURIComponent(skill.repo_id)}&path=${encodeURIComponent(view.video_path ?? skill.video_path)}#t=${view.video_from ?? skill.video_from}`} /><CaptionDetails taskName={skill.description} caption={view.caption} /></div>)}<div className="result-foot"><span>{skill.video_indexed ? "Searchable" : "Not searchable yet"}</span><div className="result-actions"><button className="quiet-button" disabled={Boolean(indexing) || deleting === `${skill.repo_id}:${skill.episode_index}`} onClick={() => { setPendingDelete(null); void indexVideo(skill.repo_id, skill.episode_index); }}>{indexing === `${skill.repo_id}:${skill.episode_index}` ? "Indexing…" : skill.video_indexed ? "Index again" : "Index"}</button><button className={`quiet-button delete-take${pendingDelete === `${skill.repo_id}:${skill.episode_index}` ? " armed" : ""}`} disabled={Boolean(indexing) || Boolean(deleting)} onClick={() => void deleteTake(skill.repo_id, skill.episode_index)}>{deleting === `${skill.repo_id}:${skill.episode_index}` ? "Deleting…" : pendingDelete === `${skill.repo_id}:${skill.episode_index}` ? "Delete this take?" : "Delete"}</button></div></div></article>)}</div>}
            {!searchMode && library.length === 0 && <div className="library-empty"><span className="empty-stack">▤</span><p>Nothing here yet.</p></div>}
          </section>
          <footer className="page-foot"><span>HOMEBOY</span><span>STAYS ON THIS COMPUTER</span></footer>
        </div>
      </section>
    </main>
    </>
  );
}

function CameraMark() {
  return <svg viewBox="0 0 48 38" aria-hidden="true" fill="none"><path d="M5 11.5h7l3-4h13l3 4h12a2 2 0 0 1 2 2v17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-17a2 2 0 0 1 2-2Z" stroke="currentColor" strokeWidth="1.5"/><circle cx="24" cy="21.5" r="7" stroke="currentColor" strokeWidth="1.5"/><circle cx="24" cy="21.5" r="2.5" fill="currentColor"/></svg>;
}

function cameraName(key: string) {
  return key.split(".").at(-1)?.toUpperCase() ?? key;
}

function CaptionDetails({ taskName, caption }: { taskName?: string; caption?: string | null }) {
  return <details className="video-caption"><summary>Caption</summary>
    <p><strong>Task name</strong>{taskName?.trim() || "No task name saved."}</p>
    <p><strong>Visual caption</strong>{caption?.trim() || "Generated when video indexing finishes."}</p>
  </details>;
}

function MjpegStream({ src, alt }: { src: string; alt: string }) {
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const timer = useRef<number | null>(null);
  useEffect(() => {
    timer.current = window.setTimeout(() => {
      void fetch(`${src}?mode=snapshot`, { cache: "no-store", signal: AbortSignal.timeout(3000) })
        .then(response => setFailed(!response.ok)).catch(() => setFailed(true));
    }, 8_000);
    return () => { if (timer.current !== null) window.clearTimeout(timer.current); };
  }, [attempt, src]);
  useEffect(() => {
    if (!failed) return;
    const retry = window.setTimeout(() => { setFailed(false); setAttempt(value => value + 1); }, 3000);
    return () => window.clearTimeout(retry);
  }, [failed]);
  return <div className="stream-wrap">
    {/* Next Image optimization cannot process the recorder's multipart MJPEG stream. */}
    {/* eslint-disable-next-line @next/next/no-img-element */}
    <img src={`${src}?retry=${attempt}`} alt={alt} onLoad={() => { if (timer.current !== null) window.clearTimeout(timer.current); setFailed(false); }} onError={() => setFailed(true)} />
    {failed && <div className="stream-error"><span>Camera feed interrupted. Reconnecting…</span><button type="button" onClick={() => { setFailed(false); setAttempt((value) => value + 1); }}>Try again</button></div>}
  </div>;
}
