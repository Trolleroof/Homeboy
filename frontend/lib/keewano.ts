import { readFileSync } from "node:fs";
import path from "node:path";

import { Keewano, type UserReporter } from "@keewano/node-sdk";

import { customEventSet } from "@/lib/keewano-events.generated";

// Server-only Keewano relay. One entity per dashboard request (its run_id UUID),
// sharing the event schema in ../keewano.events.json with the Python harness.
// Without KEEWANO_API_KEY every call is a no-op, so retrieval never depends on it.

function apiKey(): string | undefined {
  if (process.env.KEEWANO_API_KEY) return process.env.KEEWANO_API_KEY;
  // Same ignored secrets file the bridge reads, so the key lives in one place.
  try {
    const text = readFileSync(path.resolve(process.cwd(), "..", "bridge", ".env.local"), "utf8");
    return text.match(/^KEEWANO_API_KEY=(.+)$/m)?.[1].trim().replace(/^["']|["']$/g, "") || undefined;
  } catch {
    return undefined;
  }
}

let ready: Promise<boolean> | null = null;

function init(): Promise<boolean> {
  ready ??= (async () => {
    const key = apiKey();
    if (!key) return false;
    await Keewano.init({
      apiKey: key,
      // Private to this service; /datasets is ignored by Git.
      dataDir: path.resolve(process.cwd(), "..", "datasets", "keewano-node"),
      customEventSet,
    });
    return true;
  })().catch((error) => {
    console.warn("Keewano disabled:", (error as Error).message);
    return false;
  });
  return ready;
}

/** Ship one entity's events now. Never throws into the caller. */
export async function reportToKeewano(userId: string, build: (user: UserReporter) => void): Promise<void> {
  try {
    if (!(await init())) return;
    await Keewano.reportUserBatch({ userId, build });
  } catch (error) {
    console.warn("Keewano report failed:", (error as Error).message);
  }
}
