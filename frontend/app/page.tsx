"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import HarnessPanel from "./harness-panel";

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
  teleop?: { ready?: boolean; engaged?: boolean; record_ready?: boolean };
  data_quality?: { ready?: boolean; issues?: string[]; camera_streaming?: Record<string, boolean> };
  cameras?: string[];
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
    setNotice("Reading the saved video with EmbeddingGemma 2…");
    try {
      const response = await fetch("/api/skills", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "index_video", repo_id: repoId, episode_index: episodeIndex }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error ?? "Video content could not be indexed.");
      setNotice("Video content indexed. You can now search what the camera recorded.");
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
        return;
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
            setNotice("Saved to your skill library. Indexing the recorded video next…");
            void refreshLibrary();
            await indexVideo(next.repo_id, next.saved_episodes - 1);
          }).catch((reason: unknown) => setError((reason as Error).message));
        }
      }
    } catch {
      setStatus((current) => ({ ...current, offline: true }));
    }
  }, [refreshLibrary, indexVideo]);

  useEffect(() => {
    const initial = window.setTimeout(() => {
      void refreshStatus();
      void refreshLibrary();
    }, 0);
    const timer = window.setInterval(() => void refreshStatus(), 900);
    return () => { window.clearTimeout(initial); window.clearInterval(timer); };
  }, [refreshStatus, refreshLibrary]);

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
    throw new Error("The robot bridge did not become ready. Check the local Willow bridge and device connections.");
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
      setNotice("Teaching live — move the leader arm through the task. Stop when the demonstration is done.");
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
      setNotice("Take captured. Save it to keep this demonstration, or discard it and try again.");
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
      await call("recorder/estop", { reason: "operator stop from Willow skillbook" });
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
  const shortTime = `${Math.floor(status.elapsed_s / 60).toString().padStart(2, "0")}:${Math.floor(status.elapsed_s % 60).toString().padStart(2, "0")}`;
  const wristStreaming = bridgeOnline && status.data_quality?.camera_streaming?.wrist === true;
  const teachStage = recording ? "recording" : pending ? "save" : linked ? "ready" : bridgeOnline ? "syncing" : "offline";
  const connectionText = !bridgeOnline ? "Robot not connected" : !status.teleop?.ready ? "Checking leader / follower" : !status.data_quality?.ready ? "Checking camera and dataset" : linked ? "Teleop synced" : "Leader / follower connected";

  return (
    <main className="app-shell">
      <aside className="rail">
        <a className="brand" href="#top" aria-label="Willow home"><span className="brand-mark"><span /><span /><span /></span><span>willow<span className="brand-period">.</span></span></a>
        <div className="rail-label">WORKSPACE</div>
        <nav className="nav-list" aria-label="Main navigation">
          <a className="nav-item" href="#prompt"><span className="nav-icon">✳</span>Prompt robot</a>
          <a className="nav-item active" href="#teach"><span className="nav-icon">↗</span>Teach & recall</a>
          <a className="nav-item" href="#library"><span className="nav-icon">▧</span>Skill library<span className="nav-count">{library.length}</span></a>
        </nav>
        <div className="rail-spacer" />
        <div className="rail-cell">
          <div className="cell-label">ROBOT CELL</div>
          <div className="cell-name"><span className={`pulse ${linked ? "live" : ""}`} />SO-101 · Leader / Follower</div>
          <div className="cell-state">{connectionText}</div>
        </div>
        <div className="rail-foot">BUILT FOR THE MOMENTS<br />YOU DON&apos;T WANT TO REPEAT.</div>
      </aside>

      <section className="main-column" id="top">
        <header className="topbar">
          <div className="crumb"><span>MY WORKSPACE</span><b>/</b><strong>SO-101 SKILLBOOK</strong></div>
          <div className="topbar-actions">{bridgeOnline && status.state === "estopped" && <button className="quiet-button" onClick={() => { void call("recorder/rearm", {}).then(() => refreshStatus()).catch((reason: Error) => setError(reason.message)); }}>Reset stop</button>}{bridgeOnline && <button className="estop-button" onClick={() => void emergencyStop()} aria-label="Emergency stop robot">STOP</button>}<div className={`connection-pill ${linked ? "connected" : ""}`}><span className="pulse" />{connectionText}</div></div>
        </header>

        <div className="content">
          <section className="intro">
            <div className="intro-copy"><div className="eyebrow"><span className="eyebrow-line" />YOUR ROBOT, WITH A MEMORY</div><h1>Teach it once.<br /><em>Get that time back.</em></h1><p>Show Willow a task with the leader arm. It saves the motion and video together, so your team can find the right demonstration next time.</p></div>
            <div className="intro-art" aria-hidden="true"><RobotSketch /></div>
          </section>

          <HarnessPanel />
          <section className="teach-layout" id="teach">
            <aside className="card guide-card" aria-label="Teaching guide">
              <div className="card-topline"><span className="step-tag">YOUR QUICK GUIDE</span><span className="sparkle">✳</span></div>
              <h2>One quick demo.<br /><em>Saved for next time.</em></h2>
              <div className="guide-steps">
                <div className={`guide-step ${teachStage === "ready" || teachStage === "recording" || teachStage === "save" ? "complete" : "current"}`}><span>1</span><div><strong>Describe the task</strong><p>Use a short label you’ll recognize later.</p></div></div>
                <div className={`guide-step ${teachStage === "recording" ? "current" : teachStage === "save" ? "complete" : ""}`}><span>2</span><div><strong>Move the leader arm</strong><p>Wait for teleop to sync, then guide the follower through the task.</p></div></div>
                <div className={`guide-step ${teachStage === "save" ? "current" : ""}`}><span>3</span><div><strong>Stop and save</strong><p>Willow keeps the motion episode and camera recording together.</p></div></div>
                <div className="guide-safety"><strong>Before you begin</strong><p>Clear the arm&apos;s path and keep a hand near the physical stop. Starting teleop can move the follower as it syncs to the leader.</p></div>
              </div>
            </aside>

            <div className="teach-main">
            <article className="card teach-card">
              <div className="card-topline"><span className="step-tag">01 <span>·</span> TEACH A SKILL</span><span className="tiny-live"><i className={recording ? "active" : ""} />{recording ? "RECORDING" : "READY WHEN YOU ARE"}</span></div>
              <label className="field-label" htmlFor="skill-description">What should the robot learn?</label>
              <textarea id="skill-description" value={description} onChange={(event) => setDescription(event.target.value)} placeholder="Describe the task in a few words…" rows={2} disabled={recording || pending} />
              <div className="teach-bottom">
                <div className="camera-pips"><span className="camera-icon">◉</span><span>Wrist preview · synced motion</span></div>
                {recording ? <button className="primary-button stop-button" onClick={() => void stopTeaching()} disabled={busy}><span className="stop-square" />{busy ? "Stopping…" : "Stop teaching"}</button> : pending ? <div className="take-actions"><button className="quiet-button" onClick={() => void discardTake()} disabled={busy}>Discard</button><button className="primary-button" onClick={() => void saveTake()} disabled={busy}>Save demonstration <span>→</span></button></div> : <button className="primary-button" onClick={() => void startTeaching()} disabled={busy || !description.trim()}><span className="button-dot" />{busy ? "Getting the robot ready…" : "Start teaching"}<span>→</span></button>}
              </div>
              {(notice || error) && <div className={`feedback ${error ? "error" : ""}`} role="status" aria-live="polite">{error || notice}</div>}
              {(status.started_at || status.stopped_at || status.saved_at) && <div className="recording-timestamps" aria-label="Take timestamps">{status.started_at && <span>Started {new Date(status.started_at).toLocaleString()}</span>}{status.stopped_at && <span>Stopped {new Date(status.stopped_at).toLocaleString()}</span>}{status.saved_at && <span>Saved {new Date(status.saved_at).toLocaleString()}</span>}</div>}
              {recording && <div className="recording-meter"><div className="recording-meter-bar" /><span>{shortTime}</span><span>{status.frames} frames</span></div>}
            </article>

            <section className="camera-section">
              <div className="section-heading"><div><span className="step-tag">LIVE VIEW</span><h2>Wrist camera</h2></div><span className="section-sub">The recorder&apos;s live camera feed.</span></div>
              <figure className="camera-frame wrist-frame"><figcaption><span className={`camera-led ${wristStreaming ? "on" : ""}`} /><strong>WRIST</strong><span>Gripper view</span><small>{wristStreaming ? "LIVE" : "WAITING"}</small></figcaption>{wristStreaming ? <MjpegStream key="wrist" src="/api/bridge/stream/wrist" alt="Live wrist camera feed" /> : <div className="camera-placeholder"><CameraMark /><span>{bridgeOnline ? "Waiting for wrist camera frames" : "Start the local recorder to connect the camera"}</span></div>}</figure>
            </section>
            </div>
          </section>

          <section className="library-section" id="library">
            <div className="section-heading"><div><span className="step-tag">02 <span>·</span> YOUR SKILL LIBRARY</span><h2>Bring a good take back.</h2></div><span className="episode-count">{library.length} SAVED {library.length === 1 ? "DEMO" : "DEMOS"}</span></div>
            <form className="search-form" onSubmit={(event) => void searchSkills(event)}><span className="search-glyph">⌕</span><input aria-label="Search saved demonstrations" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Try ‘a gripper picking up an orange cube’" /><button type="submit" disabled={busy || !query.trim()}>{busy ? "Searching…" : "Find a demo"}<span>→</span></button></form>
            <div className="search-foot"><span>SEARCHES WHAT THE CAMERA RECORDED</span><span>{searchMode ? `MATCHING: ${searchMode.toUpperCase()}` : "NATIVE EMBEDDINGGEMMA 2 VIDEO EMBEDDINGS"}</span></div>
            {searchRunId && <div className="search-foot"><span>LOCAL REQUEST TRACE · NOT A DIAL OR KEEWANO EVENT</span><a href={`/api/events?run_id=${encodeURIComponent(searchRunId)}`} target="_blank" rel="noreferrer">VIEW TRACE ↗</a></div>}
            {results.length > 0 && <div className="results-grid">{results.map((skill) => <article className="result-card" key={`${skill.repo_id}-${skill.episode_index}`}><div className="result-title"><span className="result-icon">↗</span><div><strong>{`Recording ${skill.episode_index + 1}`}</strong><small>{skill.repo_id} · episode {skill.episode_index}</small></div><span className="match-score">{skill.score.toFixed(3)}<small>SIMILARITY</small></span></div><video controls preload="metadata" src={skill.video_url} /><div className="result-foot"><span>{`Video content · ${skill.sampled_frames} sampled frames`}</span><span>DEMO {String(skill.episode_index + 1).padStart(2, "0")}</span></div></article>)}</div>}
            {searchMode && results.length === 0 && <div className="empty-results"><span>⌕</span><strong>No close match.</strong><p>{searchMessage || "Teach a real task demonstration before searching for it."} A similar clip still needs human review.</p></div>}
            {!searchMode && library.length > 0 && <div className="results-grid">{library.map((skill) => <article className="result-card" key={`${skill.repo_id}-${skill.episode_index}`}><div className="result-title"><span className="result-icon">↗</span><div><strong>{`Recording ${skill.episode_index + 1}`}</strong><small>{skill.repo_id} · episode {skill.episode_index}</small></div><span className="library-tag">{skill.video_indexed ? "VIDEO INDEXED" : "SAVED"}</span></div><video controls preload="metadata" src={`/api/bridge/video?repo_id=${encodeURIComponent(skill.repo_id)}&path=${encodeURIComponent(skill.video_path)}#t=${skill.video_from}`} /><div className="result-foot"><span>{skill.video_indexed ? "Searchable by recorded video content" : "Video content not indexed yet"}</span><button className="quiet-button" disabled={Boolean(indexing)} onClick={() => void indexVideo(skill.repo_id, skill.episode_index)}>{indexing === `${skill.repo_id}:${skill.episode_index}` ? "Indexing video…" : skill.video_indexed ? "Reindex video" : "Index video"}</button></div></article>)}</div>}
            {!searchMode && library.length === 0 && <div className="library-empty"><span className="empty-stack">▤</span><p>Your saved demonstrations will appear here.<br />Teach a skill above to start your library.</p></div>}
          </section>
          <footer className="page-foot"><span>WILLOW SKILLBOOK</span><span>LOCAL ROBOT DATA STAYS ON THIS MACHINE</span></footer>
        </div>
      </section>
    </main>
  );
}

function RobotSketch() {
  return <svg viewBox="0 0 380 270" fill="none" xmlns="http://www.w3.org/2000/svg"><path className="sketch-grid" d="M24 228H354M48 198H322M73 168H291M99 138H262M124 108H233M149 78H204M52 245L188 65M103 245L213 65M154 245L238 65M205 245L263 65M256 245L288 65M307 245L313 65"/><path className="robot-shadow" d="M88 218h88l16 18H70l18-18Z"/><path className="robot-base" d="M95 214v-15h70v15"/><path className="robot-segment" d="m129 198-10-58 42-24 35 31-23 34-10 17"/><path className="robot-outline" d="m119 140 42-24 35 31m-58-7 38 38m20-31 22-53m-22 53 42 19m-42-19-23 34m-62 19h70m25-31 22 13 5 15m-5-15 8-12"/><circle className="robot-joint" cx="119" cy="140" r="9"/><circle className="robot-joint" cx="161" cy="116" r="9"/><circle className="robot-joint" cx="196" cy="147" r="9"/><circle className="robot-joint" cx="218" cy="94" r="8"/><circle className="robot-joint" cx="215" cy="200" r="5"/><path className="robot-grip" d="m239 178 9 4m-9-4 2-9m7 13 8 2m-8-2 3-8"/><path className="task-block" d="M278 206h22v22h-22z"/><path className="task-bin" d="M268 236h44l-5 15h-34l-5-15Z"/><path className="motion-dash" d="M244 155c21 3 30 12 38 29"/><path className="motion-arrow" d="m274 184 9 1-3-8"/><text className="sketch-label" x="60" y="45">SO-101 / LEADER-FOLLOWER</text><text className="sketch-coord" x="289" y="196">TARGET</text></svg>;
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
