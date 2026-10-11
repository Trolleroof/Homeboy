import { spawn } from "node:child_process";
import { closeSync, mkdirSync, openSync } from "node:fs";
import path from "node:path";

export const VIDEO_MODEL = "google/embeddinggemma-2";
export const VIDEO_REVISION = "914f7f89142e33e77833254d9c9b90c3cef7303b";
const base = process.env.WILLOW_VIDEO_EMBED_URL ?? "http://127.0.0.1:8622";
const shared = globalThis as typeof globalThis & { willowEmbeddingStartup?: Promise<void> };

export type NativeEmbedding = {
  embedding: number[];
  model: string;
  revision: string;
  frame_times?: number[];
  sample_sha256?: string;
  elapsed_s: number;
};

async function available(): Promise<boolean> {
  let response: Response;
  try {
    response = await fetch(`${base}/health`, { cache: "no-store", signal: AbortSignal.timeout(1500) });
  } catch { return false; }
  if (!response.ok) return false;
  const body = await response.json();
  if (body.model !== VIDEO_MODEL || body.revision !== VIDEO_REVISION) {
    throw new Error("The embedding service is running a different model version.");
  }
  return true;
}

async function ensureService(): Promise<void> {
  if (await available()) return;
  if (!shared.willowEmbeddingStartup) {
    shared.willowEmbeddingStartup = (async () => {
      if (base !== "http://127.0.0.1:8622") throw new Error("The configured video embedding service is unavailable.");
      const root = path.resolve(process.cwd(), "..");
      const logs = path.join(root, "bridge", "logs");
      mkdirSync(logs, { recursive: true });
      const log = openSync(path.join(logs, "embedding.log"), "a");
      let spawnError: Error | null = null;
      try {
        const child = spawn(path.join(root, ".embedding-venv", "bin", "python"), [path.join(root, "bridge", "scripts", "embedding_server.py")], {
          cwd: root, detached: true, stdio: ["ignore", log, log],
        });
        child.on("error", (error) => { spawnError = error; });
        child.unref();
      } finally { closeSync(log); }
      for (let attempt = 0; attempt < 40; attempt++) {
        if (spawnError) throw spawnError;
        if (await available()) return;
        await new Promise((resolve) => setTimeout(resolve, 500));
      }
      throw new Error("EmbeddingGemma 2 could not start. Check bridge/logs/embedding.log.");
    })().finally(() => { shared.willowEmbeddingStartup = undefined; });
  }
  await shared.willowEmbeddingStartup;
}

export async function nativeEmbedding(kind: "text" | "video", input: Record<string, unknown>): Promise<NativeEmbedding> {
  await ensureService();
  const response = await fetch(`${base}/embed/${kind}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input),
    signal: AbortSignal.timeout(300_000), cache: "no-store",
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail ?? "Native video embedding failed.");
  if (body.model !== VIDEO_MODEL || body.revision !== VIDEO_REVISION || !Array.isArray(body.embedding)
    || body.embedding.length !== 768 || body.embedding.some((value: unknown) => typeof value !== "number" || !Number.isFinite(value))) {
    throw new Error("Embedding service returned an invalid or incompatible vector.");
  }
  return body as NativeEmbedding;
}
