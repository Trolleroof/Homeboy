import { NextResponse } from "next/server";
import { readRunEvents } from "@/lib/run-events";

export async function GET(request: Request) {
  const runId = new URL(request.url).searchParams.get("run_id")?.trim();
  if (!runId) return NextResponse.json({ error: "run_id is required" }, { status: 400 });
  return NextResponse.json({ run_id: runId, events: await readRunEvents(runId) }, {
    headers: { "Cache-Control": "no-store" },
  });
}
