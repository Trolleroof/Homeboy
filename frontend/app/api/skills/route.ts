import { DatabaseSync } from "node:sqlite";
import { randomUUID } from "node:crypto";
import fs from "node:fs/promises";
import { mkdirSync } from "node:fs";
import path from "node:path";

import { NextResponse, connection } from "next/server";

import { loadEpisodes, loadInfo, type DatasetInfo, type Episode } from "@/lib/dataset";
import { labelsRoot, resolveDataset } from "@/lib/paths";
import { num, readParquet } from "@/lib/parquet";
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
  const dbPath = path.join(root, "homeboy_skills.sqlite3");
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
  db.exec(`CREATE TABLE IF NOT EXISTS caption_embeddings (
    repo_id TEXT NOT NULL, episode_index INTEGER NOT NULL, caption TEXT NOT NULL,
    embedding TEXT NOT NULL, model TEXT NOT NULL, revision TEXT NOT NULL, caption_model TEXT NOT NULL,
    created_at TEXT NOT NULL, PRIMARY KEY (repo_id, episode_index)
  )`);
  db.exec(`CREATE TABLE IF NOT EXISTS camera_embeddings (
    repo_id TEXT NOT NULL, episode_index INTEGER NOT NULL, camera_key TEXT NOT NULL,
    video_path TEXT NOT NULL, video_from REAL NOT NULL, video_to REAL NOT NULL,
    video_embedding TEXT NOT NULL, frame_times TEXT NOT NULL, sample_sha256 TEXT NOT NULL,
    caption TEXT NOT NULL, caption_embedding TEXT NOT NULL, model TEXT NOT NULL,
    revision TEXT NOT NULL, caption_model TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (repo_id, episode_index, camera_key)
  )`);
  const captionColumns = db.prepare("PRAGMA table_info(caption_embeddings)").all() as { name: string }[];
  if (!captionColumns.some((column) => column.name === "caption_model")) {
    db.exec("ALTER TABLE caption_embeddings ADD COLUMN caption_model TEXT NOT NULL DEFAULT 'gemma3:4b'");
  }
  return db;
}

export async function GET() {
  await connection();
  const db = openDb();
  try {
    const skills = db.prepare(`SELECT s.repo_id, s.episode_index, s.description, s.video_path, s.video_from, s.video_to, s.created_at,
      v.embedding IS NOT NULL AS video_indexed, v.model AS video_model, c.caption
      FROM skills s LEFT JOIN video_embeddings v ON s.repo_id=v.repo_id AND s.episode_index=v.episode_index
      AND v.model=? AND v.revision=? LEFT JOIN caption_embeddings c ON s.repo_id=c.repo_id AND s.episode_index=c.episode_index
      AND c.model=? AND c.revision=? ORDER BY s.created_at DESC`).all(VIDEO_MODEL, VIDEO_REVISION, VIDEO_MODEL, VIDEO_REVISION);
    const cameras = db.prepare(`SELECT repo_id, episode_index, camera_key, video_path, video_from, video_to, caption
      FROM camera_embeddings WHERE model=? AND revision=?`).all(VIDEO_MODEL, VIDEO_REVISION) as Array<Record<string, unknown>>;
    const byEpisode = new Map<string, Record<string, unknown>>();
    for (const camera of cameras) {
      const key = `${camera.repo_id}:${camera.episode_index}`;
      const views = byEpisode.get(key) ?? {};
      views[String(camera.camera_key)] = camera;
      byEpisode.set(key, views);
    }
    const episodeCache = new Map<string, Promise<Episode[]>>();
    const result = await Promise.all((skills as Array<Record<string, unknown>>).map(async (skill) => {
      const repoId = String(skill.repo_id);
      if (!episodeCache.has(repoId)) episodeCache.set(repoId, loadInfo(repoId).then(info => loadEpisodes(repoId, info)));
      const episode = (await episodeCache.get(repoId))?.find(item => item.episode_index === Number(skill.episode_index));
      const stored = byEpisode.get(`${repoId}:${skill.episode_index}`) ?? {};
      const cameraViews = Object.entries(episode?.videos ?? {}).map(([camera, video]) => {
        const saved = stored[camera] as Record<string, unknown> | undefined;
        return { camera, video_path: video.file, video_from: video.from, video_to: video.to,
          caption: saved?.caption ?? (camera === "observation.images.wrist" ? skill.caption : null) };
      });
      return { ...skill, camera_views: cameraViews };
    }));
    return NextResponse.json({ skills: result, embedding_model: VIDEO_MODEL });
  } finally {
    db.close();
  }
}

export async function POST(request: Request) {
  const body = (await request.json()) as {
    action?: "teach" | "search" | "index_video" | "delete";
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
      const info = await loadInfo(row.repo_id);
      const episode = (await loadEpisodes(row.repo_id, info)).find((item) => item.episode_index === row.episode_index);
      if (!episode) return NextResponse.json({ error: "saved episode was not found" }, { status: 404 });
      const views = Object.entries(episode.videos).filter(([key]) => key.startsWith("observation.images."));
      if (!views.length) return NextResponse.json({ error: "episode has no recorded video" }, { status: 422 });
      const indexed = [];
      for (const [cameraKey, video] of views) {
        const videoResult = await nativeEmbedding("video", { repo_id: row.repo_id, video_path: video.file, start: video.from, end: video.to, max_frames: 8 });
        const caption = await nativeEmbedding("caption", { repo_id: row.repo_id, video_path: video.file, start: video.from, end: video.to, max_frames: 4 });
        if (!caption.caption?.trim()) throw new Error(`The local vision model returned an empty ${cameraKey} caption.`);
        db.prepare(`INSERT INTO camera_embeddings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
          ON CONFLICT(repo_id, episode_index, camera_key) DO UPDATE SET video_path=excluded.video_path,
          video_from=excluded.video_from, video_to=excluded.video_to, video_embedding=excluded.video_embedding,
          frame_times=excluded.frame_times, sample_sha256=excluded.sample_sha256, caption=excluded.caption,
          caption_embedding=excluded.caption_embedding, model=excluded.model, revision=excluded.revision,
          caption_model=excluded.caption_model, created_at=excluded.created_at`)
          .run(row.repo_id, row.episode_index, cameraKey, video.file, video.from, video.to,
            JSON.stringify(videoResult.embedding), JSON.stringify(videoResult.frame_times), videoResult.sample_sha256!,
            caption.caption.trim(), JSON.stringify(caption.embedding), videoResult.model, videoResult.revision,
            caption.caption_model ?? "gemma3:4b", new Date().toISOString());
        indexed.push({ camera: cameraKey, caption: caption.caption, frame_times: videoResult.frame_times });
        if (cameraKey === "observation.images.wrist") {
          db.prepare(`INSERT INTO video_embeddings VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo_id, episode_index) DO UPDATE SET embedding=excluded.embedding, model=excluded.model,
            revision=excluded.revision, frame_times=excluded.frame_times, sample_sha256=excluded.sample_sha256, created_at=excluded.created_at`)
            .run(row.repo_id, row.episode_index, JSON.stringify(videoResult.embedding), videoResult.model, videoResult.revision,
              JSON.stringify(videoResult.frame_times), videoResult.sample_sha256!, new Date().toISOString());
          db.prepare(`INSERT INTO caption_embeddings VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo_id, episode_index) DO UPDATE SET caption=excluded.caption, embedding=excluded.embedding,
            model=excluded.model, revision=excluded.revision, caption_model=excluded.caption_model, created_at=excluded.created_at`)
            .run(row.repo_id, row.episode_index, caption.caption.trim(), JSON.stringify(caption.embedding), caption.model, caption.revision,
              caption.caption_model ?? "gemma3:4b", new Date().toISOString());
        }
      }
      return NextResponse.json({ ok: true, model: VIDEO_MODEL, source: "multi_camera_video_pixels_plus_captions", cameras: indexed });
    } catch (error) { return NextResponse.json({ error: (error as Error).message }, { status: 503 }); }
    finally { db.close(); }
  }

  if (body.action === "delete") {
    const repoId = body.repo_id?.trim();
    const episodeIndex = body.episode_index;
    if (!repoId || typeof episodeIndex !== "number" || !Number.isInteger(episodeIndex) || episodeIndex < 0) {
      return NextResponse.json({ error: "repo_id and episode_index are required" }, { status: 400 });
    }
    try {
      await removeSavedTake(repoId, episodeIndex);
      return NextResponse.json({ ok: true, episode_index: episodeIndex });
    } catch (error) {
      return NextResponse.json({ error: (error as Error).message }, { status: 500 });
    }
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
    const mode = "embeddinggemma2 wrist+overhead video and captions";
    const db = openDb();
    try {
      const rows = db.prepare(`SELECT s.repo_id, s.episode_index, s.description, s.video_path, s.video_from, s.video_to, s.created_at, v.embedding, v.frame_times, c.caption, c.embedding AS caption_embedding
          FROM skills s JOIN video_embeddings v ON s.repo_id=v.repo_id AND s.episode_index=v.episode_index
          LEFT JOIN caption_embeddings c ON s.repo_id=c.repo_id AND s.episode_index=c.episode_index AND c.model=? AND c.revision=?
          WHERE v.model=? AND v.revision=?`).all(VIDEO_MODEL, VIDEO_REVISION, VIDEO_MODEL, VIDEO_REVISION) as unknown as (SkillRow & { frame_times?: string; caption?: string; caption_embedding?: string })[];
      const cameraRows = db.prepare(`SELECT repo_id, episode_index, camera_key, video_path, video_from, video_to,
        video_embedding, frame_times, caption, caption_embedding FROM camera_embeddings WHERE model=? AND revision=?`)
        .all(VIDEO_MODEL, VIDEO_REVISION) as Array<Record<string, unknown>>;
      const camerasByEpisode = new Map<string, Record<string, unknown>[]>();
      for (const camera of cameraRows) {
        const key = `${camera.repo_id}:${camera.episode_index}`;
        camerasByEpisode.set(key, [...(camerasByEpisode.get(key) ?? []), camera]);
      }
      const ranked = rows.map((row) => {
        const key = `${row.repo_id}:${row.episode_index}`;
        const cameraViews = camerasByEpisode.get(key) ?? [];
        const candidates = cameraViews.length ? cameraViews.map((camera) => {
          const videoVector = JSON.parse(String(camera.video_embedding)) as number[];
          const captionVector = JSON.parse(String(camera.caption_embedding)) as number[];
          return { camera: camera.camera_key, caption: camera.caption,
            score: Math.max(cosine(vector, videoVector), cosine(vector, captionVector)),
            video_score: cosine(vector, videoVector), caption_score: cosine(vector, captionVector),
            video_url: `/api/video?repo_id=${encodeURIComponent(row.repo_id)}&path=${encodeURIComponent(String(camera.video_path))}#t=${camera.video_from}`,
            sampled_frames: JSON.parse(String(camera.frame_times)).length };
        }) : [{ camera: "observation.images.wrist", caption: row.caption,
          score: Math.max(cosine(vector, row.embedding ? JSON.parse(row.embedding) as number[] : []),
            cosine(vector, row.caption_embedding ? JSON.parse(row.caption_embedding) as number[] : [])),
          video_score: 0, caption_score: null,
          video_url: `/api/video?repo_id=${encodeURIComponent(row.repo_id)}&path=${encodeURIComponent(row.video_path)}#t=${row.video_from}`,
          sampled_frames: row.frame_times ? JSON.parse(row.frame_times).length : undefined }];
        const best = candidates.reduce((a, b) => a.score >= b.score ? a : b);
        return { ...row, score: best.score, video_score: best.video_score, caption_score: best.caption_score,
          match_source: "multi_camera_video_pixels_plus_captions", sampled_frames: best.sampled_frames,
          video_url: best.video_url, caption: best.caption,
          camera_views: candidates.map(({ camera, caption, video_url, sampled_frames }) => ({ camera, caption, video_url, sampled_frames })) };
      }).sort((a, b) => b.score - a.score);
      // A nearest neighbor is only a candidate. Keep weak/ambiguous results out
      // of the consumer's recall flow until a real task dataset can calibrate
      // this conservative default. Override locally for controlled evaluation.
      const threshold = Number(process.env.HOMEBOY_VIDEO_MATCH_THRESHOLD ?? 0.65);
      const margin = Number(process.env.HOMEBOY_VIDEO_MATCH_MARGIN ?? 0.03);
      const first = ranked[0];
      const gap = first && ranked.length > 1 ? first.score - ranked[1].score : null;
      const unambiguous = ranked.length === 1 || (gap !== null && gap >= margin);
      const safeMatch = Boolean(first && first.score >= threshold && unambiguous);
      const results = safeMatch
        ? ranked.filter(row => row.score >= threshold).slice(0, 5).map(row => { const result = { ...row }; delete (result as Partial<SkillRow>).embedding; delete (result as { caption_embedding?: string }).caption_embedding; return result; })
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
        source: "multi_camera_video_pixels_plus_captions",
      });
    } finally {
      db.close();
    }
  }
  return NextResponse.json({ error: "action must be teach, index_video, delete, or search" }, { status: 400 });
}

async function removeSavedTake(repoId: string, episodeIndex: number) {
  let info: DatasetInfo | null = null;
  let episodes: Episode[] = [];
  try {
    info = await loadInfo(repoId);
    episodes = await loadEpisodes(repoId, info);
  } catch { /* a library row can outlive its dataset */ }
  const episode = episodes.find((item) => item.episode_index === episodeIndex);
  const root = resolveDataset(repoId);
  if (episode && info) {
    const shared = new Set<string>();
    for (const other of episodes) {
      if (other.episode_index === episodeIndex) continue;
      shared.add(other.data_file);
      for (const video of Object.values(other.videos)) shared.add(video.file);
    }
    const own = [episode.data_file, ...Object.values(episode.videos).map((video) => video.file)];
    for (const relative of own) {
      if (shared.has(relative)) continue;
      await unlinkInside(root, relative);
    }
    for (const file of await parquetFiles(path.join(root, "meta", "episodes"))) {
      const rows = await readParquet(file, { columns: ["episode_index"] });
      const indexes = rows.map((row) => num(row.episode_index));
      if (indexes.length && indexes.every((index) => index === episodeIndex)) await fs.unlink(file);
    }
    const remaining = episodes.filter((item) => item.episode_index !== episodeIndex);
    const document = info as DatasetInfo & { splits?: { train: string } };
    document.total_episodes = remaining.length;
    document.total_frames = remaining.reduce((sum, item) => sum + item.length, 0);
    document.splits = { train: `0:${remaining.length}` };
    await fs.writeFile(path.join(root, "meta", "info.json"), `${JSON.stringify(document, null, 4)}\n`);
  }
  const db = openDb();
  try {
    db.prepare("DELETE FROM skills WHERE repo_id=? AND episode_index=?").run(repoId, episodeIndex);
    db.prepare("DELETE FROM video_embeddings WHERE repo_id=? AND episode_index=?").run(repoId, episodeIndex);
    db.prepare("DELETE FROM caption_embeddings WHERE repo_id=? AND episode_index=?").run(repoId, episodeIndex);
    db.prepare("DELETE FROM camera_embeddings WHERE repo_id=? AND episode_index=?").run(repoId, episodeIndex);
  } finally {
    db.close();
  }
  await fs.unlink(path.join(labelsRoot(), "harness", `skill-${episodeIndex}.json`)).catch((error: NodeJS.ErrnoException) => {
    if (error.code !== "ENOENT") throw error;
  });
}

async function unlinkInside(root: string, relative: string) {
  const file = path.resolve(root, relative);
  if (file !== root && !file.startsWith(root + path.sep)) throw new Error("path escapes the dataset");
  await fs.unlink(file).catch((error: NodeJS.ErrnoException) => {
    if (error.code !== "ENOENT") throw error;
  });
}

async function parquetFiles(dir: string): Promise<string[]> {
  const out: string[] = [];
  let entries;
  try { entries = await fs.readdir(dir, { withFileTypes: true }); } catch { return out; }
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...await parquetFiles(full));
    else if (entry.name.endsWith(".parquet")) out.push(full);
  }
  return out;
}

function cosine(a: number[], b: number[]): number {
  let dot = 0, aa = 0, bb = 0;
  for (let i = 0; i < a.length; i++) { dot += a[i] * b[i]; aa += a[i] * a[i]; bb += b[i] * b[i]; }
  return aa && bb ? dot / Math.sqrt(aa * bb) : 0;
}
