import { armLease } from "@/lib/arm-lease";
import { NextResponse } from "next/server";
import { config, recorderURL, start, status, stop } from "@/lib/robot-bridge";
import { GET as skillsGET, POST as skillsPOST } from "../../skills/route";
import { GET as videoGET } from "../../video/route";


const actions = new Set(["status", "record", "stop", "discard", "save", "task", "estop", "rearm", "engage", "disengage"]);
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
    if (post && armLease.held && (group === "recorder" || group === "harness" || (group === "daemon" && action === "start"))) return NextResponse.json({ error: "Finish or cancel calibration first." }, { status: 409 });
    if (parts.length === 1 && group === "skills") return post ? skillsPOST(request) : skillsGET();
    if (parts.length === 1 && group === "video" && !post) return videoGET(request);
    if (parts.length !== 2) return NextResponse.json({ error: "Unknown bridge endpoint" }, { status: 404 });
    if (group === "config" && !post && ["arms", "cameras"].includes(action)) {
      const c = config(); return NextResponse.json(action === "arms" ? { robotPort: c.robotPort, teleopPort: c.teleopPort } : { cameras: c.cameras });
    }
    if (group === "daemon") {
      if (!post && action === "status") return NextResponse.json(status());
      if (post && action === "start") return NextResponse.json(await start(String((await request.json()).task ?? "")));
      if (post && action === "stop") return NextResponse.json(await stop());
    }
    let endpoint = "";
    if (group === "harness" && ((!post && ["status", "evidence"].includes(action)) || (post && ["plan", "run", "import", "approve", "stop"].includes(action)))) endpoint = `harness/${action}${post ? "" : new URL(request.url).search}`;
    const stream = group === "stream" && config().cameras.some(c => c.name === action) && !post;
    if (stream) endpoint = `${new URL(request.url).searchParams.get("mode") === "snapshot" ? "snapshot" : "stream"}?camera=${encodeURIComponent(action)}`;
    if (group === "recorder" && actions.has(action) && (post ? action !== "status" : action === "status")) endpoint = action;
    if (!endpoint) return NextResponse.json({ error: "Unknown bridge endpoint" }, { status: 404 });
    const response = await fetch(`${recorderURL}/${endpoint}`, { method: request.method,
      headers: { "Content-Type": "application/json" }, body: post ? await request.text() : undefined,
      cache: "no-store", signal: stream ? request.signal : AbortSignal.timeout(15_000) });
    return new Response(response.body, { status: response.status, headers: { "Content-Type": response.headers.get("content-type") ?? "application/json", "Cache-Control": "no-store" } });
  } catch (error) {
    return NextResponse.json({ ok: false, offline: true, error: (error as Error).message, message: "Recorder is offline. Start teaching to connect the arms.", ...status() }, { status: 503 });
  }
}
