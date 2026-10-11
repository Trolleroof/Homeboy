import { asyncBufferFromFile, parquetReadObjects } from "hyparquet";
import { compressors } from "hyparquet-compressors";

export type Row = Record<string, unknown>;

/**
 * Read a parquet file into plain objects. `compressors` is required because
 * pyarrow writes snappy by default and hyparquet ships no codecs itself.
 * `rowStart`/`rowEnd` push the slice down to the reader, so pulling one
 * episode out of a 100 MB data file only touches that file's pages.
 */
export async function readParquet(
  file: string,
  opts: { columns?: string[]; rowStart?: number; rowEnd?: number } = {},
): Promise<Row[]> {
  const buffer = await asyncBufferFromFile(file);
  return (await parquetReadObjects({
    file: buffer,
    compressors,
    columns: opts.columns,
    rowStart: opts.rowStart,
    rowEnd: opts.rowEnd,
  })) as Row[];
}

/** Parquet ints come back as BigInt; NaN-safe coercion to a JS number. */
export function num(v: unknown): number {
  if (typeof v === "bigint") return Number(v);
  if (typeof v === "number") return v;
  if (v === null || v === undefined) return NaN;
  return Number(v);
}

/**
 * `observation.state` is stored as a fixed-size list, which surfaces as an
 * array, a typed array, or (for single-element features) a bare number
 * depending on how the column was written.
 */
export function toVector(v: unknown): number[] {
  if (v === null || v === undefined) return [];
  if (typeof v === "number") return [v];
  if (Array.isArray(v)) return v.map(num);
  if (ArrayBuffer.isView(v)) return Array.from(v as unknown as ArrayLike<number>, num);
  return [];
}
