import fs from "node:fs/promises";
import path from "node:path";

import { num, readParquet, toVector, type Row } from "./parquet";
import { resolveDataset, datasetRoot } from "./paths";

export type Feature = { dtype: string; shape: number[]; names?: string[] | null };

export type DatasetInfo = {
  codebase_version: string;
  fps: number;
  total_episodes: number;
  total_frames: number;
  robot_type?: string | null;
  data_path: string;
  video_path?: string | null;
  features: Record<string, Feature>;
};

export type Episode = {
  episode_index: number;
  length: number;
  duration_s: number;
  task: string | null;
  /** Row range of this episode inside the flat, dataset-wide frame index. */
  dataset_from_index: number;
  dataset_to_index: number;
  data_file: string;
  /**
   * v3 concatenates many episodes into one mp4, so a clip is a file plus a
   * time window into it. The player seeks to `from` and stops at `to`.
   */
  videos: Record<string, { file: string; from: number; to: number }>;
};

export async function listDatasets(): Promise<string[]> {
  const root = datasetRoot();
  const found: string[] = [];
  // repo_ids are `<namespace>/<name>`, so exactly two levels deep.
  const namespaces = await readdirSafe(root);
  for (const ns of namespaces) {
    for (const name of await readdirSafe(path.join(root, ns))) {
      if (await exists(path.join(root, ns, name, "meta", "info.json"))) {
        found.push(`${ns}/${name}`);
      }
    }
  }
  return found.sort();
}

export async function loadInfo(repoId: string): Promise<DatasetInfo> {
  const file = path.join(resolveDataset(repoId), "meta", "info.json");
  return JSON.parse(await fs.readFile(file, "utf8")) as DatasetInfo;
}

/** Feature keys whose frames were encoded to video rather than stored inline. */
export function videoKeys(info: DatasetInfo): string[] {
  return Object.entries(info.features)
    .filter(([, f]) => f.dtype === "video")
    .map(([k]) => k);
}

/** Per-joint names for `observation.state` / `action`, in column order. */
export function jointNames(info: DatasetInfo, key: string): string[] {
  const f = info.features[key];
  if (!f) return [];
  const names = f.names;
  if (Array.isArray(names) && names.length) return names;
  return Array.from({ length: f.shape[0] ?? 0 }, (_, i) => `dim_${i}`);
}

export async function loadEpisodes(repoId: string, info: DatasetInfo): Promise<Episode[]> {
  const dir = resolveDataset(repoId);
  const tasks = await loadTasks(dir);
  const vidKeys = videoKeys(info);

  const rows: Row[] = [];
  for (const file of await parquetFiles(path.join(dir, "meta", "episodes"))) {
    rows.push(...(await readParquet(file)));
  }

  const episodes = rows.map((r) => {
    const videos: Episode["videos"] = {};
    for (const key of vidKeys) {
      const chunk = num(r[`videos/${key}/chunk_index`]);
      const fileIdx = num(r[`videos/${key}/file_index`]);
      if (!Number.isFinite(chunk) || !Number.isFinite(fileIdx)) continue;
      videos[key] = {
        file: formatPath(info.video_path ?? "", { video_key: key, chunk_index: chunk, file_index: fileIdx }),
        from: num(r[`videos/${key}/from_timestamp`]) || 0,
        to: num(r[`videos/${key}/to_timestamp`]) || 0,
      };
    }
    const length = num(r["length"]);
    const taskIndex = num(r["task_index"]);
    return {
      episode_index: num(r["episode_index"]),
      length,
      duration_s: info.fps ? length / info.fps : NaN,
      // `tasks` is a list column even when an episode has exactly one task.
      task: firstTask(r["tasks"]) ?? tasks.get(taskIndex) ?? null,
      dataset_from_index: num(r["dataset_from_index"]),
      dataset_to_index: num(r["dataset_to_index"]),
      data_file: formatPath(info.data_path, {
        chunk_index: num(r["data/chunk_index"]),
        file_index: num(r["data/file_index"]),
      }),
      videos,
    } satisfies Episode;
  });

  episodes.sort((a, b) => a.episode_index - b.episode_index);
  return episodes;
}

/**
 * Read one episode's frames. `dataset_from_index` is dataset-wide but the
 * parquet row numbers are file-local, so the offset of the file's first row has
 * to come off first -- otherwise every episode past the first data file reads
 * the wrong slice (or off the end).
 */
export async function loadEpisodeFrames(
  repoId: string,
  episode: Episode,
  episodes: Episode[],
  columns: string[],
): Promise<Row[]> {
  const fileStart = Math.min(
    ...episodes.filter((e) => e.data_file === episode.data_file).map((e) => e.dataset_from_index),
  );
  return readParquet(path.join(resolveDataset(repoId), episode.data_file), {
    columns,
    rowStart: episode.dataset_from_index - fileStart,
    rowEnd: episode.dataset_to_index - fileStart,
  });
}

export { toVector };

function firstTask(v: unknown): string | null {
  if (typeof v === "string") return v;
  if (Array.isArray(v) && v.length) return String(v[0]);
  return null;
}

async function loadTasks(dir: string): Promise<Map<number, string>> {
  const file = path.join(dir, "meta", "tasks.parquet");
  const map = new Map<number, string>();
  if (!(await exists(file))) return map;
  for (const row of await readParquet(file)) {
    // Written with `task` as the index, so the column set varies by writer version.
    const task = (row["task"] ?? row["__index_level_0__"]) as string | undefined;
    if (task !== undefined) map.set(num(row["task_index"]), task);
  }
  return map;
}

/** Expand LeRobot's `chunk-{chunk_index:03d}/file-{file_index:03d}` templates. */
function formatPath(template: string, vars: Record<string, string | number>): string {
  return template.replace(/\{(\w+)(?::0(\d+)d)?\}/g, (_, key: string, width?: string) => {
    const value = vars[key];
    if (value === undefined) return "";
    return width ? String(value).padStart(Number(width), "0") : String(value);
  });
}

async function parquetFiles(dir: string): Promise<string[]> {
  const out: string[] = [];
  for (const entry of await readdirSafe(dir)) {
    const full = path.join(dir, entry);
    const stat = await fs.stat(full);
    if (stat.isDirectory()) out.push(...(await parquetFiles(full)));
    else if (entry.endsWith(".parquet")) out.push(full);
  }
  return out.sort();
}

async function readdirSafe(dir: string): Promise<string[]> {
  try {
    return (await fs.readdir(dir)).filter((n) => !n.startsWith("."));
  } catch {
    return [];
  }
}

async function exists(p: string): Promise<boolean> {
  try {
    await fs.access(p);
    return true;
  } catch {
    return false;
  }
}
