// Read Cursor's global SQLite store (state.vscdb) for each conversation's real
// title, status and timestamps. The DB is large (hundreds of MB) and rewritten
// constantly, so we do NOT load it into memory: we shell out to the `sqlite3`
// CLI in read-only mode and let `json_extract` pull just the fields we need via
// the primary-key index (a single batched query returns in a few milliseconds).
// Everything degrades to {} when sqlite3 is missing or errors, exactly like the
// Python server: the office then falls back to first-message + file mtime.
import { execFile } from "child_process";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { promisify } from "util";
import { ComposerMeta } from "./types";

const execFileAsync = promisify(execFile);

export function cursorStateDb(): string {
  const home = os.homedir();
  let base: string;
  if (process.platform === "darwin") {
    base = path.join(home, "Library", "Application Support");
  } else if (process.platform === "win32") {
    base = process.env.APPDATA || path.join(home, "AppData", "Roaming");
  } else {
    base = process.env.XDG_CONFIG_HOME || path.join(home, ".config");
  }
  return path.join(base, "Cursor", "User", "globalStorage", "state.vscdb");
}

const DB = cursorStateDb();

// Per-DB-mtime cache: keys we've already queried for the current DB snapshot are
// stored (even when absent -> {}) so we never re-query the same id within a scan.
let cache: { mtime: number; map: Map<string, ComposerMeta> } | null = null;
let sqliteAvailable: boolean | null = null;

function num(v: unknown): number | null {
  return typeof v === "number" && isFinite(v) ? v : null;
}

async function queryIds(ids: string[]): Promise<Map<string, ComposerMeta>> {
  const map = new Map<string, ComposerMeta>();
  if (ids.length === 0) {
    return map;
  }
  // ids are conversation UUIDs (hex + dashes) - safe to single-quote directly.
  const inList = ids.map((id) => "'composerData:" + id.replace(/'/g, "") + "'").join(",");
  const sql =
    "SELECT substr(key,14) AS id," +
    " json_extract(value,'$.name') AS name," +
    " json_extract(value,'$.status') AS status," +
    " json_extract(value,'$.createdAt') AS created," +
    " json_extract(value,'$.lastUpdatedAt') AS updated," +
    " json_extract(value,'$.conversationCheckpointLastUpdatedAt') AS checkpoint" +
    " FROM cursorDiskKV WHERE key IN (" + inList + ");";
  let stdout = "";
  try {
    const res = await execFileAsync("sqlite3", ["-readonly", "-json", DB, sql], {
      timeout: 4000,
      maxBuffer: 32 * 1024 * 1024,
    });
    stdout = res.stdout || "";
    sqliteAvailable = true;
  } catch (e: any) {
    if (e && (e.code === "ENOENT" || /not found/i.test(String(e.message)))) {
      sqliteAvailable = false; // sqlite3 CLI not installed (e.g. some Windows)
    }
    // Mark every requested id as queried-and-empty so we don't hammer a broken DB.
    for (const id of ids) {
      map.set(id, {});
    }
    return map;
  }
  let rows: any[] = [];
  const trimmed = stdout.trim();
  if (trimmed) {
    try {
      rows = JSON.parse(trimmed);
    } catch {
      rows = [];
    }
  }
  for (const id of ids) {
    map.set(id, {}); // default: present-but-empty
  }
  for (const r of rows) {
    if (r && typeof r.id === "string") {
      map.set(r.id, {
        name: (r.name == null ? "" : String(r.name)).trim(),
        status: (r.status == null ? "" : String(r.status)).trim(),
        created_ms: num(r.created),
        updated_ms: num(r.updated),
        checkpoint_ms: num(r.checkpoint),
      });
    }
  }
  return map;
}

/** Batched composer metadata for many conversation ids. Cheap and cached by DB mtime. */
export async function composerMetaBatch(ids: string[]): Promise<Map<string, ComposerMeta>> {
  const unique = Array.from(new Set(ids.filter(Boolean)));
  let mtime = 0;
  try {
    mtime = fs.statSync(DB).mtimeMs;
  } catch {
    return new Map();
  }
  if (sqliteAvailable === false) {
    return new Map(); // known-missing CLI: skip the spawn entirely
  }
  if (!cache || cache.mtime !== mtime) {
    cache = { mtime, map: new Map() };
  }
  const missing = unique.filter((id) => !cache!.map.has(id));
  if (missing.length > 0) {
    const more = await queryIds(missing);
    for (const [k, v] of more) {
      cache.map.set(k, v);
    }
  }
  return cache.map;
}
