// TypeScript port of the cursor_theater.py data layer. Produces the exact JSON
// payload the shared UI (ui/theater.html) expects. Runs in the extension host
// (Node), reading ~/.cursor/projects directly - no HTTP server.
import * as fs from "fs";
import * as fsp from "fs/promises";
import * as os from "os";
import * as path from "path";
import { Agent, ComposerMeta, Payload } from "./types";
import { composerMetaBatch } from "./composer";

export const MAX_AGE_MIN = 180; // only show conversations whose file changed in the last N minutes
const RUNNING_STALE_SEC = 360; // 6 min of total silence before a chat reads as idle
const ABORTED_IDLE_SEC = 90; // a manually-stopped chat reads as idle once its checkpoint freezes this long
// Cursor exposes no "generating right now" flag, so "done" is detected from the
// transcript's last line being an assistant message with no trailing tool call.
// Mid-turn (between tool calls) the last line transiently looks finished, which
// flapped an actively-working chat running<->done every scan (replaying the finish
// confetti/chime). Debounce: only trust "done" once the conversation has been QUIET
// (no transcript / lastUpdatedAt / checkpoint write) for this long.
const DONE_DEBOUNCE_SEC = 10;

const PROJECTS_DIR = path.join(os.homedir(), ".cursor", "projects");

const PERSONA_EMOJI = [
  "\u{1F575}\u{FE0F}", "\u270D\u{FE0F}", "\u{1F3C3}", "\u{1F52C}", "\u{1F4DA}", "\u{1F9ED}", "\u{1F52D}", "\u{1F528}",
  "\u{1FA84}", "\u{1F3AF}", "\u{1F989}", "\u{1F98A}", "\u{1F41D}", "\u{1F916}", "\u{1F42F}", "\u{1F985}",
];

function personaIndex(agentId: string): number {
  let h = 0;
  for (const ch of agentId || "") {
    h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  }
  return h % PERSONA_EMOJI.length;
}

function isoToMs(s: unknown): number | null {
  if (!s || typeof s !== "string") {
    return null;
  }
  const t = Date.parse(s);
  return isNaN(t) ? null : t;
}

// ---- low-level file reads -------------------------------------------------

async function readFirstLine(p: string): Promise<string> {
  const fd = await fsp.open(p, "r");
  try {
    const buf = Buffer.alloc(65536);
    const { bytesRead } = await fd.read(buf, 0, 65536, 0);
    const s = buf.toString("utf8", 0, bytesRead);
    const nl = s.indexOf("\n");
    return nl === -1 ? s : s.slice(0, nl + 1);
  } finally {
    await fd.close();
  }
}

async function readHeadLines(p: string, maxBytes = 262144): Promise<string[]> {
  const fd = await fsp.open(p, "r");
  try {
    const buf = Buffer.alloc(maxBytes);
    const { bytesRead } = await fd.read(buf, 0, maxBytes, 0);
    return buf
      .toString("utf8", 0, bytesRead)
      .split("\n")
      .filter((l) => l.trim());
  } finally {
    await fd.close();
  }
}

async function readTailLines(p: string, maxBytes = 200000): Promise<string[]> {
  const st = await fsp.stat(p);
  const size = st.size;
  const fd = await fsp.open(p, "r");
  try {
    let start = 0;
    let length = size;
    if (size > maxBytes) {
      start = size - maxBytes;
      length = maxBytes;
    }
    const buf = Buffer.alloc(length);
    await fd.read(buf, 0, length, start);
    let data = buf.toString("utf8");
    if (size > maxBytes) {
      const nl = data.indexOf("\n");
      if (nl !== -1) {
        data = data.slice(nl + 1);
      }
    }
    return data.split("\n").filter((l) => l.trim());
  } finally {
    await fd.close();
  }
}

// ---- event model (the single place that knows the journal shape) ----------

interface Event {
  kind: string;
  text: string;
  toolUses: string[];
  stopReason: string | null;
  tsMs: number | null;
  version: string | null;
  raw: any;
}

function parseAgentEvent(line: string): Event | null {
  if (!line || !line.trim()) {
    return null;
  }
  let rec: any;
  try {
    rec = JSON.parse(line);
  } catch {
    return null;
  }
  if (typeof rec !== "object" || rec === null || Array.isArray(rec)) {
    return null;
  }
  // Cursor stamps the speaker on "role"; Claude Code used "type". Accept either.
  const rtype = rec.role || rec.type;
  const msg = rec.message && typeof rec.message === "object" ? rec.message : {};
  const content = msg.content;
  const textParts: string[] = [];
  const toolUses: string[] = [];
  if (Array.isArray(content)) {
    for (const block of content) {
      if (!block || typeof block !== "object") {
        continue;
      }
      if (block.type === "text") {
        if (block.text) {
          textParts.push(block.text);
        }
      } else if (block.type === "tool_use") {
        toolUses.push(block.name || "");
      }
    }
  } else if (typeof content === "string") {
    if (content) {
      textParts.push(content);
    }
  }
  return {
    kind: typeof rtype === "string" && rtype ? rtype : "unknown",
    text: textParts.join(" ").trim(),
    toolUses: toolUses.filter((t) => t),
    stopReason: msg.stop_reason ?? null,
    tsMs: isoToMs(rec.timestamp),
    version: rec.version ?? null,
    raw: rec,
  };
}

function parseEvents(lines: string[]): { events: Event[]; skipped: number; versions: Set<string> } {
  const events: Event[] = [];
  let skipped = 0;
  const versions = new Set<string>();
  for (const ln of lines) {
    const ev = parseAgentEvent(ln);
    if (ev === null) {
      if (ln && ln.trim()) {
        skipped += 1;
      }
      continue;
    }
    events.push(ev);
    if (ev.version) {
      versions.add(ev.version);
    }
  }
  return { events, skipped, versions };
}

function lastToolUseName(events: Event[]): string | null {
  for (let i = events.length - 1; i >= 0; i--) {
    if (events[i].kind === "assistant" && events[i].toolUses.length) {
      return events[i].toolUses[events[i].toolUses.length - 1];
    }
  }
  return null;
}

function detectDone(events: Event[]): { done: boolean; endMs: number | null; result: string | null } {
  let last: Event | null = null;
  for (let i = events.length - 1; i >= 0; i--) {
    if (events[i].kind === "assistant" || events[i].kind === "user") {
      last = events[i];
      break;
    }
  }
  if (last === null || last.kind !== "assistant") {
    return { done: false, endMs: null, result: null };
  }
  const hasTool = last.toolUses.length > 0;
  const done =
    !hasTool &&
    (last.stopReason === null ||
      ["end_turn", "stop", "stop_sequence", "max_tokens"].includes(last.stopReason));
  if (done) {
    let full = last.text.split(/\s+/).join(" ");
    if (full.length > 4000) {
      full = full.slice(0, 4000) + "\u2026";
    }
    return { done: true, endMs: last.tsMs, result: full };
  }
  return { done: false, endMs: null, result: null };
}

// ---- project / session resolution (cached by mtime) -----------------------

const nameCache = new Map<string, { mtime: number; map: Record<string, { description: string; subagent_type: string }> }>();
const projectCache = new Map<string, { mtime: number; cwd: string }>();
const sessionCache = new Map<string, { mtime: number; topic: string; cwd: string }>();
const labelCache = new Map<string, string>();

function parentSessionFile(agentPath: string, sessionId: string): string | null {
  if (!sessionId) {
    return null;
  }
  let p = path.dirname(agentPath);
  while (p && path.basename(p) !== sessionId) {
    const nxt = path.dirname(p);
    if (nxt === p) {
      return null;
    }
    p = nxt;
  }
  return p + ".jsonl";
}

function statMtimeMs(p: string): number | null {
  try {
    return fs.statSync(p).mtimeMs;
  } catch {
    return null;
  }
}

function nameMapFor(parentFile: string | null): Record<string, { description: string; subagent_type: string }> {
  if (!parentFile || !fs.existsSync(parentFile)) {
    return {};
  }
  const mtime = statMtimeMs(parentFile);
  if (mtime === null) {
    return {};
  }
  const cached = nameCache.get(parentFile);
  if (cached && cached.mtime === mtime) {
    return cached.map;
  }
  const m: Record<string, { description: string; subagent_type: string }> = {};
  try {
    const text = fs.readFileSync(parentFile, "utf8");
    for (const ln of text.split("\n")) {
      if (!ln.includes('"type":"tool_use"') || !ln.includes('"description"')) {
        continue;
      }
      let rec: any;
      try {
        rec = JSON.parse(ln);
      } catch {
        continue;
      }
      if (rec.type !== "assistant") {
        continue;
      }
      const content = rec.message?.content;
      if (!Array.isArray(content)) {
        continue;
      }
      for (const block of content) {
        if (block && typeof block === "object" && block.type === "tool_use" && (block.name === "Task" || block.name === "Agent")) {
          const inp = block.input || {};
          const prompt = inp.prompt;
          if (prompt) {
            m[String(prompt).trim()] = {
              description: inp.description || "",
              subagent_type: inp.subagent_type || "",
            };
          }
        }
      }
    }
  } catch {
    /* degrade */
  }
  nameCache.set(parentFile, { mtime, map: m });
  return m;
}

function projectCwdFor(parentFile: string | null): string {
  if (!parentFile || !fs.existsSync(parentFile)) {
    return "";
  }
  const mtime = statMtimeMs(parentFile);
  if (mtime === null) {
    return "";
  }
  const cached = projectCache.get(parentFile);
  if (cached && cached.mtime === mtime) {
    return cached.cwd;
  }
  let cwd = "";
  try {
    const lines = fs.readFileSync(parentFile, "utf8").split("\n");
    for (let i = 0; i < lines.length && i <= 50; i++) {
      const ln = lines[i];
      if (!ln.includes('"cwd"')) {
        continue;
      }
      let rec: any;
      try {
        rec = JSON.parse(ln);
      } catch {
        continue;
      }
      if (rec.cwd) {
        cwd = rec.cwd;
        break;
      }
    }
  } catch {
    cwd = "";
  }
  projectCache.set(parentFile, { mtime, cwd });
  return cwd;
}

function firstUserText(rec: any): string {
  const c = rec.message?.content;
  if (typeof c === "string") {
    return c;
  }
  if (Array.isArray(c)) {
    for (const b of c) {
      if (b && typeof b === "object" && b.type === "text") {
        return b.text || "";
      }
      if (typeof b === "string") {
        return b;
      }
    }
  }
  return "";
}

const QUERY_RE = /<user_query>\s*([\s\S]*?)\s*<\/user_query>/;
const TAG_RE = /<[^>]+>/g;

function cleanPrompt(text: string): string {
  if (!text) {
    return "";
  }
  const m = QUERY_RE.exec(text);
  let out = m ? m[1] : text.replace(TAG_RE, " ");
  return out.split(/\s+/).filter(Boolean).join(" ");
}

function decodeProjectDir(enc: string): string | null {
  const segs = enc.split("-");
  let cur: string = path.sep;
  let i = 0;
  while (i < segs.length) {
    let matched: [string, number] | null = null;
    for (let j = segs.length; j > i; j--) {
      const cand = segs.slice(i, j).join("-");
      try {
        if (fs.statSync(path.join(cur, cand)).isDirectory()) {
          matched = [cand, j];
          break;
        }
      } catch {
        /* not a dir */
      }
    }
    if (matched === null) {
      return null;
    }
    cur = path.join(cur, matched[0]);
    i = matched[1];
  }
  return cur;
}

function projectLabel(p: string): string {
  const parts = p.replace(/\\/g, "/").split("/");
  const idx = parts.indexOf("agent-transcripts");
  if (idx <= 0) {
    return "";
  }
  const enc = parts[idx - 1];
  const cached = labelCache.get(enc);
  if (cached !== undefined) {
    return cached;
  }
  let label = "";
  const real = decodeProjectDir(enc);
  if (real) {
    label = path.basename(real.replace(/[\\/]+$/, ""));
  }
  if (!label) {
    let segs = enc.split("-");
    if (segs.length > 2 && segs[0] === "Users") {
      segs = segs.slice(2);
    }
    label = segs.join("-") || enc;
  }
  labelCache.set(enc, label);
  return label;
}

const pathCache = new Map<string, string>();

// The conversation's real absolute working directory (for the card-title tooltip),
// reconstructed from the encoded project folder. Falls back to "" when it can't be
// resolved (e.g. the project moved). Cached by encoded folder name.
function projectPath(transcriptPath: string): string {
  const parts = transcriptPath.replace(/\\/g, "/").split("/");
  const idx = parts.indexOf("agent-transcripts");
  if (idx <= 0) {
    return "";
  }
  const enc = parts[idx - 1];
  const cached = pathCache.get(enc);
  if (cached !== undefined) {
    return cached;
  }
  const real = decodeProjectDir(enc) || "";
  pathCache.set(enc, real);
  return real;
}

async function sessionSummary(sessionFile: string): Promise<{ topic: string; cwd: string }> {
  const mtime = statMtimeMs(sessionFile);
  if (mtime === null) {
    return { topic: "", cwd: "" };
  }
  const cached = sessionCache.get(sessionFile);
  if (cached && cached.mtime === mtime) {
    return { topic: cached.topic, cwd: cached.cwd };
  }
  const cwd = projectLabel(sessionFile);
  let topic = "";
  try {
    const lines = await readHeadLines(sessionFile);
    for (let i = 0; i < lines.length && i <= 80; i++) {
      const ln = lines[i];
      if (!ln.includes('"role":"user"') && !ln.includes('"type":"user"')) {
        continue;
      }
      let rec: any;
      try {
        rec = JSON.parse(ln);
      } catch {
        continue;
      }
      const kind = rec.role || rec.type;
      if (kind === "user") {
        topic = cleanPrompt(firstUserText(rec));
        if (topic) {
          break;
        }
      }
    }
  } catch {
    /* degrade */
  }
  sessionCache.set(sessionFile, { mtime, topic, cwd });
  return { topic, cwd };
}

function shortTask(task: string): string {
  task = task.split(/\s+/).filter(Boolean).join(" ");
  for (const sep of [". ", "? ", "! ", ": "]) {
    const idx = task.indexOf(sep);
    if (idx > 0 && idx < 90) {
      return task.slice(0, idx + 1).trim();
    }
  }
  return task.length > 90 ? task.slice(0, 90).trim() + "\u2026" : task;
}

// ---- directory walking ----------------------------------------------------

async function listdir(p: string): Promise<fs.Dirent[]> {
  try {
    return await fsp.readdir(p, { withFileTypes: true });
  } catch {
    return [];
  }
}

/** projects/<proj>/agent-transcripts/<sessiondir>/<uuid>.jsonl */
async function listTranscripts(): Promise<string[]> {
  const out: string[] = [];
  for (const proj of await listdir(PROJECTS_DIR)) {
    if (!proj.isDirectory()) {
      continue;
    }
    const at = path.join(PROJECTS_DIR, proj.name, "agent-transcripts");
    for (const sess of await listdir(at)) {
      if (!sess.isDirectory()) {
        continue;
      }
      const sdir = path.join(at, sess.name);
      for (const f of await listdir(sdir)) {
        if (f.isFile() && f.name.endsWith(".jsonl")) {
          out.push(path.join(sdir, f.name));
        }
      }
    }
  }
  return out;
}

/** projects/**\/agent-*.jsonl (subagent transcripts) */
async function listAgentFiles(): Promise<string[]> {
  const out: string[] = [];
  const stack = [PROJECTS_DIR];
  while (stack.length) {
    const dir = stack.pop()!;
    for (const e of await listdir(dir)) {
      const full = path.join(dir, e.name);
      if (e.isDirectory()) {
        stack.push(full);
      } else if (e.isFile() && /^agent-.*\.jsonl$/.test(e.name)) {
        out.push(full);
      }
    }
  }
  return out;
}

// ---- conversation desks (room leads) --------------------------------------

async function scanSessions(nowSec: number): Promise<Agent[]> {
  const paths = await listTranscripts();
  const recent: { path: string; uuid: string; mtimeSec: number }[] = [];
  for (const p of paths) {
    const mtimeMs = statMtimeMs(p);
    if (mtimeMs === null) {
      continue;
    }
    const mtimeSec = mtimeMs / 1000;
    if ((nowSec - mtimeSec) / 60 > MAX_AGE_MIN) {
      continue;
    }
    recent.push({ path: p, uuid: path.basename(p).slice(0, -6), mtimeSec });
  }
  const metaMap = await composerMetaBatch(recent.map((r) => r.uuid));
  const entries: Agent[] = [];
  for (const { path: p, uuid, mtimeSec } of recent) {
    const { topic, cwd } = await sessionSummary(p);
    let events: Event[] = [];
    try {
      events = parseEvents(await readTailLines(p)).events;
    } catch {
      events = [];
    }
    const { done: isDone, result } = detectDone(events);
    const tool = lastToolUseName(events) || "";
    const pid = personaIndex(uuid);
    let mtimeMs = Math.floor(mtimeSec * 1000);
    const meta: ComposerMeta = metaMap.get(uuid) || {};
    const title = meta.name || topic;
    const startMs = meta.created_ms || mtimeMs;
    const lastActivityMs = Math.max(mtimeMs, meta.updated_ms || 0);
    const endMs = lastActivityMs || mtimeMs;
    const liveStatus = (meta.status || "").toLowerCase();
    const isGenerating = ["generating", "running", "streaming", "thinking"].includes(liveStatus);
    const checkpointMs = meta.checkpoint_ms || 0;
    const abortedIdle =
      liveStatus === "aborted" && !!checkpointMs && nowSec - checkpointMs / 1000 > ABORTED_IDLE_SEC;
    // Freshest "is it still working right now?" signal. The checkpoint advances every
    // few seconds mid-turn even when the transcript mtime / lastUpdatedAt lag, so a
    // chat touched within DONE_DEBOUNCE_SEC is still generating and must NOT read done.
    const freshestMs = Math.max(lastActivityMs, checkpointMs);
    const recentlyActive = !!freshestMs && nowSec - freshestMs / 1000 < DONE_DEBOUNCE_SEC;
    let status: Agent["status"];
    if (isDone && !isGenerating && !recentlyActive) {
      status = "done";
    } else if (abortedIdle) {
      status = "aborted"; // user manually stopped it (distinct from naturally idle)
    } else if (isGenerating || recentlyActive || nowSec - lastActivityMs / 1000 <= RUNNING_STALE_SEC) {
      status = "running";
    } else {
      status = "stale";
    }
    mtimeMs = lastActivityMs || mtimeMs;
    entries.push({
      id: uuid,
      persona_id: pid,
      emoji: PERSONA_EMOJI[pid],
      role: "",
      subagent_type: "",
      title,
      status,
      tool,
      task: topic,
      task_short: shortTask(topic),
      result: isDone ? result : null,
      start_ms: startMs,
      end_ms: isDone ? endMs : null,
      session: uuid.slice(0, 8),
      session_full: uuid,
      cwd,
      project: cwd,
      path: projectPath(p) || cwd,
      mtime_ms: mtimeMs,
      is_session: true,
    });
  }
  return entries;
}

// ---- subagent desks -------------------------------------------------------

interface AgentCacheEntry {
  mtime: number;
  size: number;
  adict: Agent;
  isDone: boolean;
  versions: Set<string>;
  skipped: number;
}
const agentCache = new Map<string, AgentCacheEntry>();

async function scanSubAgents(
  nowSec: number,
  versions: Set<string>
): Promise<{ agents: Agent[]; skipped: number }> {
  const paths = await listAgentFiles();
  const agents: Agent[] = [];
  let skipped = 0;
  const seen = new Set<string>();
  for (const p of paths) {
    let st: fs.Stats;
    try {
      st = fs.statSync(p);
    } catch {
      continue;
    }
    const mtimeSec = st.mtimeMs / 1000;
    const size = st.size;
    if ((nowSec - mtimeSec) / 60 > MAX_AGE_MIN) {
      continue;
    }
    seen.add(p);

    let cached = agentCache.get(p);
    let adict: Agent;
    let isDone: boolean;
    if (cached && cached.mtime === st.mtimeMs && cached.size === size) {
      adict = cached.adict;
      isDone = cached.isDone;
      for (const v of cached.versions) {
        versions.add(v);
      }
      skipped += cached.skipped;
    } else {
      let firstLine = "";
      try {
        firstLine = await readFirstLine(p);
      } catch {
        firstLine = "";
      }
      if (!(firstLine && firstLine.trim())) {
        continue;
      }
      const firstEv = parseAgentEvent(firstLine);
      if (firstEv === null) {
        skipped += 1;
        continue;
      }
      const fvers = new Set<string>();
      if (firstEv.version) {
        fvers.add(firstEv.version);
      }
      const agentId = firstEv.raw.agentId || path.basename(p).slice(6, -6);
      const session = firstEv.raw.sessionId || "";
      const startMs = firstEv.tsMs;
      const task = firstEv.text || "";

      let events: Event[] = [];
      let nSkip = 0;
      try {
        const parsed = parseEvents(await readTailLines(p));
        events = parsed.events;
        nSkip = parsed.skipped;
        for (const v of parsed.versions) {
          fvers.add(v);
        }
      } catch {
        events = [];
      }
      const det = detectDone(events);
      isDone = det.done;
      const tool = lastToolUseName(events);
      const pid = personaIndex(agentId);
      const parent = parentSessionFile(p, session);
      const info = task.trim() ? nameMapFor(parent)[task.trim()] : undefined;
      const project = projectCwdFor(parent);
      adict = {
        id: agentId,
        persona_id: pid,
        emoji: PERSONA_EMOJI[pid],
        role: info ? info.description : "",
        subagent_type: info ? info.subagent_type : "",
        status: "running",
        tool: tool || "",
        task,
        task_short: shortTask(task),
        result: det.result,
        start_ms: startMs,
        end_ms: det.endMs,
        session: session.slice(0, 8),
        session_full: session,
        cwd: firstEv.raw.cwd || "",
        project,
        path: (firstEv.raw.cwd as string) || project,
        mtime_ms: Math.floor(st.mtimeMs),
        is_session: false,
      };
      cached = { mtime: st.mtimeMs, size, adict, isDone, versions: fvers, skipped: nSkip };
      agentCache.set(p, cached);
      for (const v of fvers) {
        versions.add(v);
      }
      skipped += nSkip;
    }

    // status tracks wall-clock now, so recompute every scan (even on a cache hit).
    // Debounce "done" like top-level chats: a subagent whose file was written within
    // DONE_DEBOUNCE_SEC is mid-turn (its last line only transiently looks finished
    // between tool calls), so keep it "running" until it goes quiet.
    let status: Agent["status"];
    if (isDone && nowSec - adict.mtime_ms / 1000 > DONE_DEBOUNCE_SEC) {
      status = "done";
    } else if (nowSec - adict.mtime_ms / 1000 > RUNNING_STALE_SEC) {
      status = "stale";
    } else {
      status = "running";
    }
    const a: Agent = { ...adict, status };
    // role/subagent_type live in the PARENT file, which the agent-keyed cache
    // can't notice changing - re-resolve each scan (nameMapFor is mtime-cached).
    const t = a.task.trim();
    if (t) {
      const info = nameMapFor(parentSessionFile(p, a.session_full))[t];
      if (info) {
        a.role = info.description;
        a.subagent_type = info.subagent_type;
      }
    }
    agents.push(a);
  }
  // evict aged-out / vanished files
  for (const gone of Array.from(agentCache.keys())) {
    if (!seen.has(gone)) {
      agentCache.delete(gone);
    }
  }
  return { agents, skipped };
}

// ---- public entry ---------------------------------------------------------

export async function scan(): Promise<Payload> {
  const nowSec = Date.now() / 1000;
  const versions = new Set<string>();
  const sub = await scanSubAgents(nowSec, versions);
  const sessions = await scanSessions(nowSec);
  const agents = [...sub.agents, ...sessions];
  const order: Record<string, number> = { running: 0, stale: 1, aborted: 1, done: 2 };
  agents.sort((a, b) => {
    const oa = order[a.status] ?? 3;
    const ob = order[b.status] ?? 3;
    if (oa !== ob) {
      return oa - ob;
    }
    const sa = a.is_session ? 1 : 0;
    const sb = b.is_session ? 1 : 0;
    if (sa !== sb) {
      return sb - sa;
    }
    return (b.start_ms || 0) - (a.start_ms || 0);
  });
  return {
    agents,
    versions: Array.from(versions).sort(),
    tested_version: "",
    unknown_versions: [],
    skipped: sub.skipped,
  };
}

export { PROJECTS_DIR };
