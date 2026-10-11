import { armLease } from "@/lib/arm-lease";
import { NextResponse } from "next/server";
import { config, recorderURL, start, status, stop } from "@/lib/robot-bridge";
import { GET as skillsGET, POST as skillsPOST } from "../../skills/route";
import { GET as videoGET } from "../../video/route";
import fs from "node:fs";
import path from "node:path";
import { root } from "@/lib/robot-bridge";


const actions = new Set(["status", "record", "stop", "discard", "save", "task", "estop", "rearm", "engage", "disengage", "sync"]);
type Context = { params: Promise<{ path: string[] }> };
export async function GET(request: Request, context: Context) { return handle(request, context); }
export async function POST(request: Request, context: Context) { return handle(request, context); }
async function handle(request: Request, context: Context) {
  const parts = (await context.params).path;
  const [group, action] = parts;
  const post = request.method === "POST";
  // Next's internal Request URL can use localhost even when the browser used
  // 127.0.0.1. Compare against the incoming Host, which browsers set themselves.
  if (post && request.headers.get("origin") && new URL(request.headers.get("origin")!).host !== (request.headers.get("host") ?? new URL(request.url).host)) {
    return NextResponse.json({ error: "Cross-origin robot commands are disabled" }, { status: 403 });
  }
  try {
    const stopping = (group === "recorder" && ["stop", "estop", "disengage"].includes(action)) || (group === "harness" && action === "stop");
    if (post && armLease.held && !stopping && (group === "recorder" || group === "harness" || (group === "daemon" && action === "start"))) return NextResponse.json({ error: "Finish or cancel calibration first." }, { status: 409 });
    if (parts.length === 1 && group === "skills") return post ? skillsPOST(request) : skillsGET();
    if (parts.length === 1 && group === "video" && !post) return videoGET(request);
    if (parts.length !== 2) return NextResponse.json({ error: "Unknown bridge endpoint" }, { status: 404 });
    if (group === "config" && !post && ["arms", "cameras"].includes(action)) {
      const c = config(); return NextResponse.json(action === "arms" ? { robotPort: c.robotPort, teleopPort: c.teleopPort } : { cameras: c.cameras });
    }
    if (group === "config" && action === "cameras" && post) {
      const body = await request.json() as { overheadIndex?: number };
      if (!Number.isInteger(body.overheadIndex) || (body.overheadIndex ?? -1) < 0 || (body.overheadIndex ?? 0) > 15) {
        return NextResponse.json({ error: "Overhead camera index must be an integer from 0 to 15." }, { status: 400 });
      }
      const cameras = JSON.parse(fs.readFileSync(path.join(root, "config/cameras.json"), "utf8")) as Record<string, number>;
      if (body.overheadIndex === cameras.wrist) return NextResponse.json({ error: "Overhead and wrist cameras must use different indexes." }, { status: 400 });
      cameras.overhead = body.overheadIndex!;
      fs.writeFileSync(path.join(root, "config/cameras.json"), `${JSON.stringify(cameras, null, 2)}\n`);
      return NextResponse.json({ ok: true, cameras: Object.entries(cameras).map(([name, index]) => ({ name, index })) });
    }
    if (group === "daemon") {
      if (!post && action === "status") return NextResponse.json(status());
      if (post && action === "start") return NextResponse.json(await start(String((await request.json()).task ?? "")));
      if (post && action === "stop") return NextResponse.json(await stop());
    }
    let endpoint = "";
    if (group === "harness" && ((!post && ["status", "evidence"].includes(action)) || (post && ["plan", "run", "import", "find_episode", "do", "approve", "stop"].includes(action)))) endpoint = `harness/${action}${post ? "" : new URL(request.url).search}`;
    const stream = group === "stream" && config().cameras.some(c => c.name === action) && !post;
    if (stream) endpoint = `${new URL(request.url).searchParams.get("mode") === "snapshot" ? "snapshot" : "stream"}?camera=${encodeURIComponent(action)}`;
    if (group === "recorder" && actions.has(action) && (post ? action !== "status" : action === "status")) endpoint = action;
    if (!endpoint) return NextResponse.json({ error: "Unknown bridge endpoint" }, { status: 404 });
    let body: string | undefined;
    if (post) {
      body = await request.text();
      if (group === "recorder" && action === "sync") {
        let payload: unknown;
        try { payload = JSON.parse(body || "{}"); } catch { payload = null; }
        if (!payload || typeof payload !== "object" || (payload as { confirmed?: unknown }).confirmed !== true) {
          return NextResponse.json({ ok: false, error: "Confirm the follower movement in the dashboard before syncing arms." }, { status: 400 });
        }
      }
    }
    const response = await fetch(`${recorderURL}/${endpoint}`, { method: request.method,
      headers: { "Content-Type": "application/json" }, body,
      cache: "no-store", signal: stream ? request.signal : AbortSignal.timeout(group === "recorder" && action === "sync" ? 120_000 : post ? 35_000 : 15_000) });
    return new Response(response.body, { status: response.status, headers: { "Content-Type": response.headers.get("content-type") ?? "application/json", "Cache-Control": "no-store" } });
  } catch (error) {
    // A status poll answering "the recorder is down" is a successful check, not a server failure.
    const statusPoll = !post && action === "status" && (group === "recorder" || group === "harness");
    const failure = { ...status(), ok: false, ...(statusPoll ? { offline: true, message: "Recorder is offline. Start teaching to connect the arms." } : {}),
      error: (error as Error).name === "TimeoutError" ? `Timed out waiting for ${group}/${action}. Check live status; STOP remains available.` : (error as Error).message };
    return NextResponse.json(failure, { status: statusPoll ? 200 : 503 });
  }
}
