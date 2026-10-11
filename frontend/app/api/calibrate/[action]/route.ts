import { connection, NextResponse } from "next/server";
import { advance, cancel, start, status } from "@/lib/calibrate";
type Context = { params: Promise<{ action: string }> };
export async function GET(_request: Request, { params }: Context) {
  if ((await params).action !== "status") return NextResponse.json({ error: "Unknown action" }, { status: 404 });
  await connection();
  return NextResponse.json(status(), { headers: { "Cache-Control": "no-store" } });
}
export async function POST(request: Request, { params }: Context) {
  const origin = request.headers.get("origin");
  if (origin && new URL(origin).host !== (request.headers.get("host") ?? new URL(request.url).host)) return NextResponse.json({ error: "Cross-origin arm commands disabled" }, { status: 403 });
  try {
    const { action } = await params;
    if (action === "start") {
      const body = await request.json();
      if (body.role !== "leader" && body.role !== "follower") throw new Error("Choose Leader or Follower.");
      return NextResponse.json(await start(body.role));
    }
    if (action === "advance") return NextResponse.json(advance());
    if (action === "cancel") return NextResponse.json(await cancel());
    return NextResponse.json({ error: "Unknown action" }, { status: 404 });
  } catch (error) { return NextResponse.json({ error: (error as Error).message }, { status: 409 }); }
}
