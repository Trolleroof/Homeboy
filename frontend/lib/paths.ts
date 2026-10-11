import { homedir } from "node:os";
import path from "node:path";

/**
 * Root under which LeRobot caches datasets, one directory per `repo_id`
 * (e.g. `<root>/nikhi/homeboy_pick`). Override with HOMEBOY_DATASET_ROOT to
 * point at a scratch copy or an external drive.
 */
export function datasetRoot(): string {
  const override = process.env.HOMEBOY_DATASET_ROOT;
  if (override) return path.resolve(override.replace(/^~(?=$|\/)/, homedir()));
  return path.join(homedir(), ".cache", "huggingface", "lerobot");
}

/**
 * Where verdicts are written. Deliberately *outside* the dataset directory:
 * re-recording an episode rewrites the parquet files, and `lerobot-edit-dataset`
 * rewrites them too. Labels living alongside the code survive both, and diff
 * cleanly in git.
 */
export function labelsRoot(): string {
  const override = process.env.HOMEBOY_LABELS_ROOT;
  if (override) return path.resolve(override.replace(/^~(?=$|\/)/, homedir()));
  return path.resolve(process.cwd(), "..", "datasets");
}

/** `nikhi/homeboy_pick` -> `<labelsRoot>/nikhi__homeboy_pick.labels.jsonl` */
export function labelsFile(repoId: string): string {
  return path.join(labelsRoot(), `${repoId.replace(/\//g, "__")}.labels.jsonl`);
}

/**
 * Guard against `repo_id` traversing out of the dataset root. Every route takes
 * `repo_id` straight from the query string, so this runs on all of them.
 */
export function resolveDataset(repoId: string): string {
  const root = datasetRoot();
  const dir = path.resolve(root, repoId);
  if (dir !== root && !dir.startsWith(root + path.sep)) {
    throw new Error(`repo_id escapes the dataset root: ${repoId}`);
  }
  return dir;
}
