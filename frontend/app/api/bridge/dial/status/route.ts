import { NextResponse } from "next/server";
import fs from "node:fs";
import path from "node:path";

const root = path.resolve(process.cwd(), "..", "bridge");
function localEnv() {
  const file = path.join(root, ".env.local");
  const values: Record<string, string> = {};
  try {
    for (const raw of fs.readFileSync(file, "utf8").split(/\r?\n/)) {
      const line = raw.trim();
      if (!line || line.startsWith("#")) continue;
      const i = line.indexOf("=");
      if (i > 0) values[line.slice(0, i)] = line.slice(i + 1).trim().replace(/^['"]|['"]$/g, "");
    }
  } catch { /* local setup file has not been created */ }
  return values;
}

export async function GET() {
  const env = localEnv();
  const key = env.DIAL_API_KEY;
  const number = env.DIAL_NUMBER ?? "";
  let context = { connected: false, tools: 0, error: "Dial status unavailable" };
  if (key) {
    try {
      const response = await fetch("https://api.getdial.ai/api/v1/context-mcps", {
        headers: { Authorization: `Bearer ${key}`, "User-Agent": "homeboy-dashboard/0.1" }, cache: "no-store", signal: AbortSignal.timeout(7000),
      });
      if (response.ok) {
        const data = await response.json();
        const item = data.contextMcps?.find((entry: { name: string; url: string }) =>
          entry.url === env.DIAL_MCP_URL || entry.name === "Willow robot harness");
        context = item ? { connected: item.status === "connected", tools: item.toolCount ?? 0, error: item.lastError ?? "" }
          : { connected: false, tools: 0, error: "Context MCP not registered" };
      } else context.error = `Dial returned HTTP ${response.status}`;
    } catch { context.error = "Cannot reach Dial's API"; }
  } else context.error = "Dial key not configured";

  let bridge = { online: false, active: false, approvedSkills: 0, motorsFresh: false };
  try {
    const response = await fetch("http://127.0.0.1:8621/harness/status", { cache: "no-store", signal: AbortSignal.timeout(1500) });
    if (response.ok) {
      const data = await response.json();
      bridge = { online: true, active: data.active === true,
        approvedSkills: (data.skills ?? []).filter((s: { approved?: boolean }) => s.approved).length,
        motorsFresh: (data.motors?.age_s ?? 999) < .5 };
    }
  } catch { /* bridge is currently offline */ }

  let toolCalls: { event: string; caller: string; request_id?: string; accepted?: boolean; at?: string; score?: number }[] = [];
  try {
    const log = path.join(root, "..", "datasets", "dial-voice", "tools.jsonl");
    if (fs.existsSync(log)) toolCalls = fs.readFileSync(log, "utf8").trim().split("\n").slice(-10).map(line => {
      const event = JSON.parse(line);
      return { event: event.event, caller: (event.caller_hash as string | undefined)?.slice(0, 10) ?? "", request_id: event.request_id,
        accepted: event.accepted, at: event.at, score: typeof event.score === "number" ? event.score : undefined };
    }).reverse();
  } catch { /* malformed local audit entry is skipped as a whole */ }
  return NextResponse.json({ configured: !!key, number, context, bridge, toolCalls }, { headers: { "Cache-Control": "no-store" } });
}
