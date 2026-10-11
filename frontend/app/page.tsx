"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import HarnessPanel from "./harness-panel";
import So101Diagram from "./so101-diagram";

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
  delta?: {
    joints?: Record<string, { follower: number; leader: number; delta: number }>;
    max?: number;
    limit?: number;
  };
  motors?: { positions?: Record<string, number>; age_s?: number | null };
};

type Skill = {
  repo_id: string;
  episode_index: number;
  description: string;
  score: number;
  video_url: string;
  match_source?: string;
  sampled_frames?: number;
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
  const [library, setLibrary] = useState<SavedSkill[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
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
    } catch { /* the SUDS service may still be starting */ }
  }, []);

  const indexVideo = useCallback(async (repoId: string, episodeIndex: number) => {
    setIndexing(`${repoId}:${episodeIndex}`); setError("");
    setNotice("Indexing video embeddings…");
    try {
      const response = await fetch("/api/skills", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "index_video", repo_id: repoId, episode_index: episodeIndex }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error ?? "Video content could not be indexed.");
      setNotice("Video indexed. Search is ready.");
      await refreshLibrary();
    } catch (reason) { setError((reason as Error).message); }
    finally { setIndexing(""); }
  }, [refreshLibrary]);

  const refreshStatus = useCallback(async () => {
    try {
      const response = await fetch("/api/bridge/recorder/status", { cache: "no-store" });
      const next = await response.json() as RecorderStatus;
      if (!response.ok || next.offline) {
        setStatus((current) => ({ ...current, offline: true }));
        return false;
      }
      setStatus(next);
      if (savedCount.current === null) savedCount.current = next.saved_episodes;
      else if (next.saved_episodes > savedCount.current) {
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
            if (!res.ok) throw new Error(body.error ?? "The episode saved but its skill index failed.");
            setNotice("Episode saved. Indexing video…");
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
    let cancelled = false;
    void fetch("/api/bridge/config/cameras", { cache: "no-store" }).then((response) => response.json()).then((body: { cameras?: Array<{ name: string; index: number }> }) => {
      const overhead = body.cameras?.find((camera) => camera.name === "overhead");
      if (!cancelled && overhead) setOverheadIndex(overhead.index);
      if (!cancelled) setCameraConfigLoaded(true);
    }).catch(() => { if (!cancelled) setCameraConfigLoaded(true); });
    return () => { cancelled = true; };
  }, []);

  const call = useCallback(async (endpoint: string, body?: Record<string, unknown>) => {
    const response = await fetch(`/api/bridge/${endpoint}`, {
      method: body ? "POST" : "GET",
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      cache: "no-store",
    });
    const result = await response.json();
    if (!response.ok || result.ok === false) throw new Error(result.error ?? `Request failed: ${endpoint}`);
    return result;
  }, []);

  async function waitFor(test: (next: RecorderStatus) => boolean, timeoutMs = 90_000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const response = await fetch("/api/bridge/recorder/status", { cache: "no-store" });
      const next = await response.json() as RecorderStatus;
      if (response.ok && !next.offline) {
        setStatus(next);
        if (test(next)) return next;
        if (next.state === "estopped") throw new Error("Robot is stopped. Check the workspace, then use Reset stop before teaching.");
      }
      if (next.offline) {
        const daemon = await call("daemon/status") as { running: boolean; log: string[] };
        if (!daemon.running) throw new Error(`Recorder could not start. ${daemon.log.slice(-3).join(" ") || "Check that both arms are plugged in and powered."}`);
      }
      await new Promise((resolve) => window.setTimeout(resolve, 700));
    }
    throw new Error("The robot bridge did not become ready. Check the local Homeboy bridge and device connections.");
  }

  async function syncArms() {
    if (!syncAvailable) return;
    const approved = window.confirm(
      "Sync arms will move the follower smoothly to the leader's current pose over about 2 seconds, then leave teleop engaged. Clear the follower's path and keep a hand near STOP. Continue?",
    );
    if (!approved) return;
    setBusy(true); setError(""); setNotice("Syncing the follower to the leader over about 2 seconds…");
    try {
      await call("recorder/sync", { confirmed: true });
      const current = await waitFor((next) => next.teleop?.engaged === true && next.teleop?.ready === true, 10_000);
      if (!current.teleop?.ready) throw new Error("Sync completed, but measured joint gaps are still above the 25-unit limit. Check follower motion and alignment.");
      setNotice("Arms synced. Teleop is engaged and within the tracking limit.");
    } catch (reason) {
      setError((reason as Error).message);
      setNotice("");
      await refreshStatus();
    } finally { setBusy(false); }
  }

  async function startTeaching() {
    if (!description.trim()) return;
    setBusy(true); setError(""); setNotice("");
    try {
      let current = status;
      if (current.offline) {
        setNotice("Connecting the SO-101 bridge…");
        const daemon = await call("daemon/status") as { running?: boolean };
        if (!daemon.running) {
          const [arms, cameraConfig] = await Promise.all([call("config/arms"), call("config/cameras")]);
          const result = await call("daemon/start", {
            repoId: "willow/speedhacks-wrist-v1",
            task: description.trim(),
            fps: 30,
            robotPort: arms.robotPort,
            teleopPort: arms.teleopPort,
            cameras: cameraConfig.cameras,
            deltaLimit: 25,
            autoEstop: false,
            engageOnStart: false,
            commitSeconds: 6,
          });
          if (result.ok === false && !String(result.error ?? "").includes("already")) throw new Error(result.error ?? "Could not start the robot recorder.");
        }
        current = await waitFor((next) => next.state === "idle" && next.data_quality?.ready === true);
      }
      if (current.state === "estopped") throw new Error("Robot is stopped. Check the workspace, then use Reset stop before teaching.");
      if (current.state === "recording" || current.state === "saving") throw new Error("A recording is already in progress.");
      if (current.state === "pending") throw new Error("Save or discard the stopped take before starting another demonstration.");
      if (!current.data_quality?.ready) {
        const issues = current.data_quality?.issues?.join(" · ");
        throw new Error(issues || "The recorder is not ready. Check the camera and dataset setup.");
      }
      if (!current.teleop?.ready) {
        setNotice("Checking the leader and follower connection…");
        current = await waitFor((next) => next.teleop?.ready === true && next.data_quality?.ready === true, 8_000);
      }
      if (!current.teleop?.engaged) {
        setNotice("Syncing leader and follower…");
        await call("recorder/engage", {});
        current = await waitFor((next) => next.teleop?.record_ready === true, 8_000);
      }
      current = await waitFor((next) => next.teleop?.record_ready === true && next.data_quality?.ready === true, 8_000);
      await call("recorder/task", { task: description.trim() });
      skillToIndex.current = description.trim();
      await call("recorder/record", {});
      await waitFor((next) => next.state === "recording" && next.frames > 0, 8_000);
      setNotice("Recording — move the leader through the task, then stop.");
      await refreshStatus();
    } catch (reason) {
      setError((reason as Error).message);
      setNotice("");
    } finally {
      setBusy(false);
    }
  }

  async function stopTeaching() {
    setBusy(true); setError("");
    try {
      await call("recorder/stop", {});
      setNotice("Stopped. Save or discard this take.");
      await refreshStatus();
    } catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  }

  async function saveTake() {
    setBusy(true); setError("");
    try { await call("recorder/save", {}); setNotice("Saving the episode and video…"); await refreshStatus(); }
    catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  }

  async function discardTake() {
    setBusy(true); setError("");
    try { await call("recorder/discard", {}); skillToIndex.current = null; setNotice("Take discarded."); await refreshStatus(); }
    catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  }

  async function emergencyStop() {
    setError("");
    try {
      await call("recorder/estop", { reason: "operator stop from Homeboy skillbook" });
      skillToIndex.current = null;
      setNotice("Robot stopped. Re-arm only after checking the workspace and arm position.");
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
  const pending = status.state === "pending";
  const bridgeOnline = !status.offline;
  const preflightReady = bridgeOnline && status.teleop?.ready === true && status.data_quality?.ready === true;
  const linked = preflightReady && status.teleop?.engaged === true;
  const syncAvailable = bridgeOnline && status.state === "idle" && !status.teleop?.engaged && !status.estop?.engaged
    && !status.calibration && (status.hardware?.teleop?.motors_ok ?? 0) === 6
    && (status.hardware?.follower?.motors_ok ?? 0) === 6 && Object.keys(status.delta?.joints ?? {}).length === 6;
  const shortTime = `${Math.floor(status.elapsed_s / 60).toString().padStart(2, "0")}:${Math.floor(status.elapsed_s % 60).toString().padStart(2, "0")}`;
  const wristStreaming = bridgeOnline && status.data_quality?.camera_streaming?.wrist === true;
  const overheadStreaming = bridgeOnline && status.data_quality?.camera_streaming?.overhead === true;
  const teachStage = recording ? "recording" : pending ? "save" : linked ? "ready" : bridgeOnline ? "syncing" : "offline";
  const connectionText = !bridgeOnline
    ? "Robot not connected"
    : !status.teleop?.ready
      ? status.teleop?.worst_joint && typeof status.teleop.worst === "number"
        ? `Align ${status.teleop.worst_joint} · ${status.teleop.worst.toFixed(1)} / 25`
        : "Waiting for fresh arm readings"
      : !status.data_quality?.ready
        ? "Checking camera and dataset"
        : linked ? "Teleop synced" : "Leader / follower connected";

  return (
    <main className="app-shell">
      <aside className="rail">
        <a className="brand" href="#top" aria-label="Homeboy home"><span className="brand-mark"><span /><span /><span /></span><span>homeboy<span className="brand-period">.</span></span></a>
        <div className="rail-label">WORKSPACE</div>
        <nav className="nav-list" aria-label="Main navigation">
          <a className="nav-item" href="#prompt"><span className="nav-icon">✳</span>Prompt robot</a>
          <a className="nav-item active" href="#teach"><span className="nav-icon">↗</span>Teach & recall</a>
          <a className="nav-item" href="#library"><span className="nav-icon">▧</span>Recorded episodes<span className="nav-count">{library.length}</span></a>
        </nav>
        <button className="rail-sync-button" type="button" disabled={!syncAvailable || busy} onClick={() => void syncArms()} title={syncAvailable ? "Move follower to the leader's current pose and engage teleop" : "Sync is available when both arms are connected, idle, disengaged, and reporting six fresh joints"}>
          <span aria-hidden="true">↻</span><b>{busy && notice.startsWith("Syncing") ? "Syncing arms…" : "Sync arms"}</b>
        </button>
        <div className="rail-spacer" />
        <div className="rail-foot">LOCAL RECORDER<br />SO-101 TELEOP</div>
      </aside>

      <section className="main-column" id="top">
        <header className="topbar">
          <div className="crumb"><span>WORKSPACE</span><b>/</b><strong>SO-101</strong></div>
          <div className="topbar-actions">{bridgeOnline && status.state === "estopped" && <button className="quiet-button" onClick={() => { void call("recorder/rearm", {}).then(() => refreshStatus()).catch((reason: Error) => setError(reason.message)); }}>Reset stop</button>}{bridgeOnline && <button className="estop-button" onClick={() => void emergencyStop()} aria-label="Emergency stop robot">STOP</button>}<div className={`connection-pill ${linked ? "connected" : ""}`}><span className="pulse" />{connectionText}</div></div>
        </header>

        <div className="content">
          <section className="intro">
            <div className="intro-copy"><div className="eyebrow"><span className="eyebrow-line" />TELEOP RECORDING</div><h1>Record episodes<br /><em>from the leader arm.</em></h1><p>Motion and wrist video are stored together. Search by description or by what the camera saw.</p></div>
            <div className="intro-art"><So101Diagram status={status} /></div>
          </section>

          <HarnessPanel />
          <section className="teach-layout" id="teach">
            <aside className="card guide-card" aria-label="Teaching guide">
              <div className="card-topline"><span className="step-tag">STEPS</span><span className="sparkle">✳</span></div>
              <h2>Record a take</h2>
              <div className="guide-steps">
                <div className={`guide-step ${teachStage === "ready" || teachStage === "recording" || teachStage === "save" ? "complete" : "current"}`}><span>1</span><div><strong>Label the task</strong><p>Short description for this episode.</p></div></div>
                <div className={`guide-step ${teachStage === "recording" ? "current" : teachStage === "save" ? "complete" : ""}`}><span>2</span><div><strong>Teleop the leader</strong><p>Sync leader and follower, then move through the task.</p></div></div>
                <div className={`guide-step ${teachStage === "save" ? "current" : ""}`}><span>3</span><div><strong>Stop and save</strong><p>Writes parquet frames and wrist video to the dataset.</p></div></div>
                <div className="guide-safety"><strong>Safety</strong><p>Clear the workspace. The follower may move when teleop engages.</p></div>
              </div>
            </aside>

            <div className="teach-main">
            <article className="card teach-card">
              <div className="card-topline"><span className="step-tag">01 <span>·</span> RECORD</span><span className="tiny-live"><i className={recording ? "active" : ""} />{recording ? "RECORDING" : "IDLE"}</span></div>
              {bridgeOnline && <div className={`sync-check ${status.teleop?.ready ? "sync-check-ready" : ""}`}><span className="sync-check-led" /><div><strong>{status.teleop?.ready ? "Leader and follower aligned" : status.teleop?.worst_joint ? `Align ${status.teleop.worst_joint.replaceAll("_", " ")}` : "Waiting for fresh arm readings"}</strong><small>{status.teleop?.ready ? "Within the 25-unit tracking limit" : typeof status.teleop?.worst === "number" ? `Largest gap ${status.teleop.worst.toFixed(1)} / 25 units · move the leader or follower by hand until all joint gaps are under 25` : "Both arms must report fresh readings before teleop can start"}</small></div><span className="sync-motor-count">L {status.hardware?.teleop?.motors_ok ?? 0}/{status.hardware?.teleop?.motors_total ?? 6} · F {status.hardware?.follower?.motors_ok ?? 0}/{status.hardware?.follower?.motors_total ?? 6}</span></div>}
              <label className="field-label" htmlFor="skill-description">Task label</label>
              <textarea id="skill-description" value={description} onChange={(event) => setDescription(event.target.value)} placeholder="Describe the task in a few words…" rows={2} disabled={recording || pending} />
              <div className="teach-bottom">
                <div className="camera-pips"><span className="camera-icon">◉</span><span>Wrist · motion sync</span></div>
                {recording ? <button className="primary-button stop-button" onClick={() => void stopTeaching()} disabled={busy}><span className="stop-square" />{busy ? "Stopping…" : "Stop"}</button> : pending ? <div className="take-actions"><button className="quiet-button" onClick={() => void discardTake()} disabled={busy}>Discard</button><button className="primary-button" onClick={() => void saveTake()} disabled={busy}>Save episode <span>→</span></button></div> : <button className="primary-button" onClick={() => void startTeaching()} disabled={busy || !description.trim()}><span className="button-dot" />{busy ? "Connecting…" : "Start recording"}<span>→</span></button>}
              </div>
              {(notice || error) && <div className={`feedback ${error ? "error" : ""}`} role="status" aria-live="polite">{error || notice}</div>}
              {(status.started_at || status.stopped_at || status.saved_at) && <div className="recording-timestamps" aria-label="Take timestamps">{status.started_at && <span>Started {new Date(status.started_at).toLocaleString()}</span>}{status.stopped_at && <span>Stopped {new Date(status.stopped_at).toLocaleString()}</span>}{status.saved_at && <span>Saved {new Date(status.saved_at).toLocaleString()}</span>}</div>}
              {recording && <div className="recording-meter"><div className="recording-meter-bar" /><span>{shortTime}</span><span>{status.frames} frames</span></div>}
            </article>

              <section className="camera-section">
              <div className="section-heading"><div><span className="step-tag">LIVE VIEW</span><h2>Camera split view</h2></div><span className="section-sub">Live streams from the recorder.</span></div>
              <div className="camera-grid live-camera-grid">
                <figure className="camera-frame wrist-frame"><figcaption><span className={`camera-led ${wristStreaming ? "on" : ""}`} /><strong>WRIST</strong><span>Gripper view</span><small>{wristStreaming ? "LIVE" : "WAITING"}</small></figcaption>{wristStreaming ? <MjpegStream key="wrist" src="/api/bridge/stream/wrist" alt="Live wrist camera feed" /> : <div className="camera-placeholder"><CameraMark /><span>{bridgeOnline ? "Waiting for wrist camera frames" : "Start the local recorder to connect the camera"}</span></div>}</figure>
                <figure className="camera-frame wrist-frame"><figcaption><span className={`camera-led ${overheadStreaming ? "on" : ""}`} /><strong>OVERHEAD</strong><span>Workspace view</span><small>{overheadStreaming ? "LIVE" : "WAITING"}</small></figcaption>{overheadStreaming ? <MjpegStream key="overhead" src="/api/bridge/stream/overhead" alt="Live overhead workspace camera feed" /> : <div className="camera-placeholder"><CameraMark /><span>{bridgeOnline ? "Waiting for overhead camera frames; restart the recorder after configuring this camera." : "Start the local recorder to connect the camera"}</span></div>}</figure>
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
            <div className="section-heading"><div><span className="step-tag">02 <span>·</span> RECORDED EPISODES</span><h2>Saved takes</h2></div><span className="episode-count">{library.length} EPISODE{library.length === 1 ? "" : "S"}</span></div>
            <form className="search-form" onSubmit={(event) => void searchSkills(event)}><span className="search-glyph">⌕</span><input aria-label="Search recorded episodes" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="e.g. gripper picking up an orange cube" /><button type="submit" disabled={busy || !query.trim()}>{busy ? "Searching…" : "Search"}<span>→</span></button></form>
            <div className="search-foot"><span>VIDEO SEMANTIC SEARCH</span><span>{searchMode ? `MODE: ${searchMode.toUpperCase()}` : "EMBEDDINGGEMMA 2"}</span></div>
            {searchRunId && <div className="search-foot"><span>LOCAL REQUEST TRACE · NOT A DIAL OR KEEWANO EVENT</span><a href={`/api/events?run_id=${encodeURIComponent(searchRunId)}`} target="_blank" rel="noreferrer">VIEW TRACE ↗</a></div>}
            {results.length > 0 && <div className="results-grid">{results.map((skill) => <article className="result-card" key={`${skill.repo_id}-${skill.episode_index}`}><div className="result-title"><span className="result-icon">↗</span><div><strong>{`Recording ${skill.episode_index + 1}`}</strong><small>{skill.repo_id} · episode {skill.episode_index}</small></div><span className="match-score">{skill.score.toFixed(3)}<small>SIMILARITY</small></span></div><video controls preload="metadata" src={skill.video_url} /><div className="result-foot"><span>{`Video content · ${skill.sampled_frames} sampled frames`}</span><span>EP {String(skill.episode_index + 1).padStart(2, "0")}</span></div></article>)}</div>}
            {searchMode && results.length === 0 && <div className="empty-results"><span>⌕</span><strong>No close match.</strong><p>{searchMessage || "Teach a real task demonstration before searching for it."} A similar clip still needs human review.</p></div>}
            {!searchMode && library.length > 0 && <div className="results-grid">{library.map((skill) => <article className="result-card" key={`${skill.repo_id}-${skill.episode_index}`}><div className="result-title"><span className="result-icon">↗</span><div><strong>{`Recording ${skill.episode_index + 1}`}</strong><small>{skill.repo_id} · episode {skill.episode_index}</small></div><span className="library-tag">{skill.video_indexed ? "VIDEO INDEXED" : "SAVED"}</span></div><video controls preload="metadata" src={`/api/bridge/video?repo_id=${encodeURIComponent(skill.repo_id)}&path=${encodeURIComponent(skill.video_path)}#t=${skill.video_from}`} /><div className="result-foot"><span>{skill.video_indexed ? "Searchable by recorded video content" : "Video content not indexed yet"}</span><button className="quiet-button" disabled={Boolean(indexing)} onClick={() => void indexVideo(skill.repo_id, skill.episode_index)}>{indexing === `${skill.repo_id}:${skill.episode_index}` ? "Indexing video…" : skill.video_indexed ? "Reindex video" : "Index video"}</button></div></article>)}</div>}
            {!searchMode && library.length === 0 && <div className="library-empty"><span className="empty-stack">▤</span><p>Saved episodes appear here after you record above.</p></div>}
          </section>
          <footer className="page-foot"><span>HOMEBOY · SO-101</span><span>DATA STAYS ON THIS MACHINE</span></footer>
        </div>
      </section>
    </main>
  );
}

function CameraMark() {
  return <svg viewBox="0 0 48 38" aria-hidden="true" fill="none"><path d="M5 11.5h7l3-4h13l3 4h12a2 2 0 0 1 2 2v17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-17a2 2 0 0 1 2-2Z" stroke="currentColor" strokeWidth="1.5"/><circle cx="24" cy="21.5" r="7" stroke="currentColor" strokeWidth="1.5"/><circle cx="24" cy="21.5" r="2.5" fill="currentColor"/></svg>;
}

function MjpegStream({ src, alt }: { src: string; alt: string }) {
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const timer = useRef<number | null>(null);
  useEffect(() => {
    timer.current = window.setTimeout(() => setFailed(true), 8_000);
    return () => { if (timer.current !== null) window.clearTimeout(timer.current); };
  }, [attempt]);
  return <div className="stream-wrap">
    {/* Next Image optimization cannot process the recorder's multipart MJPEG stream. */}
    {/* eslint-disable-next-line @next/next/no-img-element */}
    <img src={`${src}?retry=${attempt}`} alt={alt} onLoad={() => { if (timer.current !== null) window.clearTimeout(timer.current); setFailed(false); }} onError={() => setFailed(true)} />
    {failed && <div className="stream-error"><span>Camera feed stopped responding.</span><button type="button" onClick={() => { setFailed(false); setAttempt((value) => value + 1); }}>Reconnect feed</button></div>}
  </div>;
}
