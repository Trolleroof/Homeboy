import { appendFile, mkdir, readFile } from "node:fs/promises";
import path from "node:path";

import {
  batchReportAgentKind, batchReportBlockedReason, batchReportHarnessEvent, batchReportRequestSource,
  batchReportRetrievalMatched, batchReportRetrievalTopScore, batchReportRunStarted, batchReportSkillSelected,
} from "@/lib/keewano-events.generated";
import { reportToKeewano } from "@/lib/keewano";

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
  // Local JSONL stays the audit of record; Keewano gets typed events only,
  // never the raw query text.
  void reportToKeewano(event.run_id, (user) => {
    const d = event.details ?? {};
    if (event.event === "request_received") {
      batchReportRunStarted(user, "dashboard_search");
      batchReportAgentKind(user, "video_retrieval");
      batchReportRequestSource(user, "dashboard");
    }
    if (event.event === "skill_search_completed" && typeof d.top_score === "number")
      batchReportRetrievalTopScore(user, Math.max(0, Math.round(d.top_score * 100)));
    if (event.event === "skill_selected") {
      if (typeof d.episode_index === "number") batchReportSkillSelected(user, `episode-${d.episode_index}`);
      batchReportRetrievalMatched(user, true);
    }
    if (event.event === "no_match") {
      batchReportBlockedReason(user, String(d.reason ?? "other"));
      batchReportRetrievalMatched(user, false);
    }
    batchReportHarnessEvent(user, event.event);
  });
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
