import { createReadStream } from "node:fs";
import fs from "node:fs/promises";
import path from "node:path";
import { Readable } from "node:stream";

import { NextResponse } from "next/server";

import { resolveDataset } from "@/lib/paths";



/**
 * Serve an mp4 out of the dataset directory with byte-range support. Safari and
 * Chrome both refuse to seek a video the server answers with a plain 200, and
 * seeking is the whole point here: v3 packs many episodes into one file, so the
 * player always starts partway in.
 */
export async function GET(request: Request) {
  const params = new URL(request.url).searchParams;
  const repoId = params.get("repo_id");
  const relative = params.get("path");
  if (!repoId || !relative) {
    return NextResponse.json({ error: "repo_id and path are required" }, { status: 400 });
  }

  let file: string;
  try {
    const root = resolveDataset(repoId);
    file = path.resolve(root, relative);
    if (!file.startsWith(root + path.sep)) throw new Error("path escapes the dataset");
  } catch (error) {
    return NextResponse.json({ error: (error as Error).message }, { status: 400 });
  }

  let size: number;
  try {
    size = (await fs.stat(file)).size;
  } catch {
    return NextResponse.json({ error: `not found: ${relative}` }, { status: 404 });
  }

  const headers: Record<string, string> = {
    "Content-Type": "video/mp4",
    "Accept-Ranges": "bytes",
    "Cache-Control": "no-store",
  };

  const range = request.headers.get("range");
  const match = range?.match(/^bytes=(\d*)-(\d*)$/);
  if (!match) {
    headers["Content-Length"] = String(size);
    return new Response(toWebStream(file), { status: 200, headers });
  }

  const start = match[1] ? Number(match[1]) : 0;
  const end = match[2] ? Math.min(Number(match[2]), size - 1) : size - 1;
  if (Number.isNaN(start) || start > end || start >= size) {
    return new Response(null, { status: 416, headers: { "Content-Range": `bytes */${size}` } });
  }

  headers["Content-Range"] = `bytes ${start}-${end}/${size}`;
  headers["Content-Length"] = String(end - start + 1);
  return new Response(toWebStream(file, start, end), { status: 206, headers });
}

function toWebStream(file: string, start?: number, end?: number): ReadableStream {
  return Readable.toWeb(createReadStream(file, { start, end })) as ReadableStream;
}
