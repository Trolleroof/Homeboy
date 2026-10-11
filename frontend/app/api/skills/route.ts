import { DatabaseSync } from "node:sqlite";
import { randomUUID } from "node:crypto";
import fs from "node:fs/promises";
import { mkdirSync } from "node:fs";
import path from "node:path";

import { NextResponse, connection } from "next/server";

import { loadEpisodes, loadInfo } from "@/lib/dataset";
import { labelsRoot, resolveDataset } from "@/lib/paths";
import { appendRunEvent } from "@/lib/run-events";
import { nativeEmbedding, VIDEO_MODEL, VIDEO_REVISION } from "@/lib/native-embeddings";




type SkillRow = {
  repo_id: string;
  episode_index: number;
  description: string;
  video_path: string;
  video_from: number;
  video_to: number;
  embedding: string | null;
  created_at: string;
};

function openDb(): DatabaseSync {
  const root = labelsRoot();
  mkdirSync(root, { recursive: true });
  const dbPath = path.join(root, "willow_skills.sqlite3");
  const db = new DatabaseSync(dbPath);
  db.exec(`CREATE TABLE IF NOT EXISTS skills (
    repo_id TEXT NOT NULL,
    episode_index INTEGER NOT NULL,
    description TEXT NOT NULL,
    video_path TEXT NOT NULL,
    video_from REAL NOT NULL,
    video_to REAL NOT NULL,
    embedding TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (repo_id, episode_index)
  )`);
  db.exec(`CREATE TABLE IF NOT EXISTS video_embeddings (
    repo_id TEXT NOT NULL, episode_index INTEGER NOT NULL, embedding TEXT NOT NULL,
    model TEXT NOT NULL, revision TEXT NOT NULL, frame_times TEXT NOT NULL,
    sample_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (repo_id, episode_index)
  )`);
  return db;
}

export async function GET() {
  await connection();
  const db = openDb();
  try {
    const skills = db.prepare(`SELECT s.repo_id, s.episode_index, s.description, s.video_path, s.video_from, s.video_to, s.created_at,
      v.embedding IS NOT NULL AS video_indexed, v.model AS video_model
      FROM skills s LEFT JOIN video_embeddings v ON s.repo_id=v.repo_id AND s.episode_index=v.episode_index
      AND v.model=? AND v.revision=? ORDER BY s.created_at DESC`).all(VIDEO_MODEL, VIDEO_REVISION);
    return NextResponse.json({ skills, embedding_model: VIDEO_MODEL });
  } finally {
    db.close();
  }
}

export async function POST(request: Request) {
  const body = (await request.json()) as {
    action?: "teach" | "search" | "index_video";
    source?: "video" | "label";
    repo_id?: string;
    episode_index?: number;
    description?: string;
    query?: string;
  };

  if (body.action === "index_video") {
    if (!body.repo_id || !Number.isInteger(body.episode_index)) return NextResponse.json({ error: "repo_id and episode_index are required" }, { status: 400 });
    const db = openDb();
    try {
      const row = db.prepare("SELECT * FROM skills WHERE repo_id=? AND episode_index=?").get(body.repo_id, body.episode_index!) as SkillRow | undefined;
      if (!row) return NextResponse.json({ error: "Save the skill before indexing video" }, { status: 404 });
      const result = await nativeEmbedding("video", { repo_id: row.repo_id, video_path: row.video_path, start: row.video_from, end: row.video_to, max_frames: 8 });
      db.prepare(`INSERT INTO video_embeddings VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo_id, episode_index) DO UPDATE SET embedding=excluded.embedding, model=excluded.model,
        revision=excluded.revision, frame_times=excluded.frame_times, sample_sha256=excluded.sample_sha256, created_at=excluded.created_at`)
        .run(row.repo_id, row.episode_index, JSON.stringify(result.embedding), result.model, result.revision,
          JSON.stringify(result.frame_times), result.sample_sha256!, new Date().toISOString());
      return NextResponse.json({ ok: true, model: result.model, source: "video_pixels", frame_times: result.frame_times, elapsed_s: result.elapsed_s });
    } catch (error) { return NextResponse.json({ error: (error as Error).message }, { status: 503 }); }
    finally { db.close(); }
  }

  if (body.action === "teach") {
    const repoId = body.repo_id?.trim();
    const episodeIndex = body.episode_index;
    const description = body.description?.trim() ?? "";
    if (!repoId || typeof episodeIndex !== "number" || !Number.isInteger(episodeIndex)) {
      return NextResponse.json({ error: "repo_id and episode_index are required" }, { status: 400 });
    }
    try {
      const info = await loadInfo(repoId);
      const episode = (await loadEpisodes(repoId, info)).find((item) => item.episode_index === episodeIndex);
      if (!episode) return NextResponse.json({ error: "saved episode was not found" }, { status: 404 });
      const video = episode.videos["observation.images.wrist"] ?? Object.values(episode.videos)[0];
      if (!video) return NextResponse.json({ error: "episode has no recorded video" }, { status: 422 });
      const fullPath = path.resolve(resolveDataset(repoId), video.file);
      if (!fullPath.startsWith(resolveDataset(repoId) + path.sep)) throw new Error("video path escaped dataset root");
      await fs.access(fullPath);
      const db = openDb();
      try {
        db.prepare(`INSERT INTO skills (repo_id, episode_index, description, video_path, video_from, video_to, embedding, created_at)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?)
          ON CONFLICT(repo_id, episode_index) DO UPDATE SET description=excluded.description, video_path=excluded.video_path,
            video_from=excluded.video_from, video_to=excluded.video_to, embedding=excluded.embedding, created_at=excluded.created_at`)
          .run(repoId, episodeIndex, description, video.file, video.from, video.to, null, new Date().toISOString());
      } finally {
        db.close();
      }
      return NextResponse.json({ ok: true, embedded: false, episode_index: episodeIndex });
    } catch (error) {
      return NextResponse.json({ error: (error as Error).message }, { status: 500 });
    }
  }

  if (body.action === "search") {
    const query = body.query?.trim();
    if (!query) return NextResponse.json({ error: "query is required" }, { status: 400 });
    const runId = randomUUID();
    const startedAt = new Date().toISOString();
    await appendRunEvent({ run_id: runId, event: "request_received", at: startedAt, details: { query } });
    if (body.source && body.source !== "video") return NextResponse.json({ error: "Only video content retrieval is supported" }, { status: 400 });
    let vector: number[];
    try { vector = (await nativeEmbedding("text", { text: query })).embedding; }
    catch (error) { return NextResponse.json({ error: (error as Error).message }, { status: 503 }); }
    const mode = "embeddinggemma2 video";
    const db = openDb();
    try {
      const rows = db.prepare(`SELECT s.repo_id, s.episode_index, s.description, s.video_path, s.video_from, s.video_to, s.created_at, v.embedding, v.frame_times
          FROM skills s JOIN video_embeddings v ON s.repo_id=v.repo_id AND s.episode_index=v.episode_index
          WHERE v.model=? AND v.revision=?`).all(VIDEO_MODEL, VIDEO_REVISION) as unknown as (SkillRow & { frame_times?: string })[];
      const ranked = rows.map((row) => {
        let score = 0;
        const saved = row.embedding ? (JSON.parse(row.embedding) as number[]) : null;
        if (vector && saved?.length === vector.length) score = cosine(vector, saved);
        return { ...row, score, match_source: "video_pixels", sampled_frames: row.frame_times ? JSON.parse(row.frame_times).length : undefined, video_url: `/api/video?repo_id=${encodeURIComponent(row.repo_id)}&path=${encodeURIComponent(row.video_path)}#t=${row.video_from}` };
      }).sort((a, b) => b.score - a.score);
      // A nearest neighbor is only a candidate. Keep weak/ambiguous results out
      // of the consumer's recall flow until a real task dataset can calibrate
      // this conservative default. Override locally for controlled evaluation.
      const threshold = Number(process.env.WILLOW_VIDEO_MATCH_THRESHOLD ?? 0.65);
      const margin = Number(process.env.WILLOW_VIDEO_MATCH_MARGIN ?? 0.03);
      const first = ranked[0];
      const gap = first && ranked.length > 1 ? first.score - ranked[1].score : null;
      const unambiguous = ranked.length === 1 || (gap !== null && gap >= margin);
      const safeMatch = Boolean(first && first.score >= threshold && unambiguous);
      const results = safeMatch
        ? ranked.filter(row => row.score >= threshold).slice(0, 5).map(row => { const result = { ...row }; delete (result as Partial<SkillRow>).embedding; return result; })
        : [];
      const completedAt = new Date().toISOString();
      await appendRunEvent({
        run_id: runId,
        event: "skill_search_completed",
        at: completedAt,
        details: { mode, candidate_count: ranked.length, top_score: first?.score ?? null, threshold },
      });
      await appendRunEvent({
        run_id: runId,
        event: safeMatch ? "skill_selected" : "no_match",
        at: new Date().toISOString(),
        details: safeMatch && first
          ? { repo_id: first.repo_id, episode_index: first.episode_index, score: first.score }
          : { reason: !first ? "empty_library" : "below_threshold_or_ambiguous" },
      });
      return NextResponse.json({
        run_id: runId,
        results,
        safe_match: safeMatch,
        top_score: first?.score ?? null,
        score_gap: gap,
        threshold,
        required_gap: margin,
        reason: !first ? "No videos are indexed. Click Index video on a saved demonstration." : !safeMatch ? "No close match: the best candidate is too weak or ambiguous." : null,
        mode,
        model: VIDEO_MODEL,
        source: "video_pixels",
      });
    } finally {
      db.close();
    }
  }
  return NextResponse.json({ error: "action must be teach, index_video or search" }, { status: 400 });
}

function cosine(a: number[], b: number[]): number {
  let dot = 0, aa = 0, bb = 0;
  for (let i = 0; i < a.length; i++) { dot += a[i] * b[i]; aa += a[i] * a[i]; bb += b[i] * b[i]; }
  return aa && bb ? dot / Math.sqrt(aa * bb) : 0;
}
