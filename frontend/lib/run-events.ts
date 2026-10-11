import { appendFile, mkdir, readFile } from "node:fs/promises";
import path from "node:path";

export type RunEvent = {
  run_id: string;
  event: string;
  at: string;
  details?: Record<string, string | number | boolean | null>;
};

const allowedEvents = new Set([
  "request_received",
  "skill_search_completed",
  "skill_selected",
  "no_match",
]);

function eventFile(): string {
  return path.resolve(process.cwd(), "..", "datasets", "run-events.jsonl");
}

export async function appendRunEvent(event: RunEvent): Promise<void> {
  if (!allowedEvents.has(event.event)) throw new Error("Unsupported run event");
  const file = eventFile();
  await mkdir(path.dirname(file), { recursive: true });
  await appendFile(file, `${JSON.stringify(event)}\n`, { encoding: "utf8", flag: "a" });
}

export async function readRunEvents(runId: string): Promise<RunEvent[]> {
  try {
    const text = await readFile(eventFile(), "utf8");
    return text.split("\n").filter(Boolean).slice(-5000).flatMap((line) => {
      try {
        const event = JSON.parse(line) as RunEvent;
        return event.run_id === runId ? [event] : [];
      } catch {
        return [];
      }
    });
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return [];
    throw error;
  }
}
