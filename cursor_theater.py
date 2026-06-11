# -*- coding: utf-8 -*-
"""
Cursor Theater - a live, grouped office of your Cursor agent conversations.

A Cursor port of Asaf Abramzon's Claude Theater (MIT). Reads the transcript
files Cursor writes under
  ~/.cursor/projects/<encoded-cwd>/agent-transcripts/<uuid>/<uuid>.jsonl
and serves a small web page. Each conversation is a compact character: avatar +
tiny name + live status + timer + the tool it is using right now. Click a
character to see its full task and result. Finished conversations are hidden by
default (a count stays in the room header).

Run:  python -m cursor_theater
  or: cursor-theater                (after pip/pipx install)
Then: http://localhost:7333

Pure stdlib. No pip installs needed to run.
"""
import json
import os
import sys
import glob
import time
import datetime
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
from urllib.request import pathname2url

__version__ = "0.1.4"

def _default_port():
    """Port from $CURSOR_THEATER_PORT, else 7333. The --port flag overrides this."""
    try:
        p = int(os.environ.get("CURSOR_THEATER_PORT") or os.environ.get("CLAUDE_THEATER_PORT") or 7333)
        return p if 0 < p < 65536 else 7333
    except ValueError:
        return 7333


PORT = _default_port()
DEMO = False               # --demo: serve a synthetic office, never read real journals
MAX_AGE_MIN = 180          # only show agents whose file changed in the last N minutes
# A "running" agent whose transcript has been silent this long is shown as idle.
# Cursor buffers the transcript .jsonl and only flushes lastUpdatedAt when a turn
# settles, so a single long agent turn (thinking / a slow tool / a big edit) can
# stay file-silent for minutes while genuinely working. 90s (tuned for Claude
# Code, which flushes continuously) flagged those live chats as idle, so we use a
# far more forgiving window here.
RUNNING_STALE_SEC = 360    # 6 min of total silence before a chat reads as idle
# A conversation Cursor marks status="aborted" (you manually stopped it) is read
# as idle once its checkpoint timestamp has been frozen this long. The checkpoint
# advances every few seconds while a turn is genuinely generating, so this window
# is comfortably longer than any normal between-write gap (incl. a long "thinking"
# pause or a slow tool) -- preventing a still-working chat that momentarily reads
# "aborted" from being wrongly flipped to idle, while a truly stopped agent (whose
# checkpoint freezes instantly) drops out of "working" in ~90s instead of ~6 min.
ABORTED_IDLE_SEC = 90

PROJECTS_DIR = os.path.join(os.path.expanduser("~"), ".cursor", "projects")

# Glob for Cursor conversation transcripts (one per conversation):
#   ~/.cursor/projects/<encoded-cwd>/agent-transcripts/<uuid>/<uuid>.jsonl
TRANSCRIPT_GLOB = os.path.join(PROJECTS_DIR, "*", "agent-transcripts", "*", "*.jsonl")


def cursor_state_db():
    """Cursor's global SQLite store, where each conversation's human title lives
    under composerData:<composerId>.name. OS-specific location."""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support")
    elif os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return os.path.join(base, "Cursor", "User", "globalStorage", "state.vscdb")


CURSOR_DB = cursor_state_db()

# composer_id -> (db_mtime, meta). The transcript folder UUID IS Cursor's
# composerId, so we can look up the chat's real title, timestamps and status.
_COMPOSER_CACHE = {}


def composer_meta(composer_id):
    """Title + timestamps + status for a Cursor conversation, read READ-ONLY from
    Cursor's live global store (WAL allows concurrent readers). Returns {} on any
    problem so the office degrades to file-mtime / first-message instead of crashing."""
    if not composer_id:
        return {}
    try:
        db_mtime = os.path.getmtime(CURSOR_DB)
    except OSError:
        return {}
    cached = _COMPOSER_CACHE.get(composer_id)
    if cached and cached[0] == db_mtime:
        return cached[1]
    meta = {}
    try:
        uri = "file:" + pathname2url(CURSOR_DB) + "?mode=ro&immutable=1"
        con = sqlite3.connect(uri, uri=True, timeout=0.5)
        try:
            row = con.execute(
                "SELECT value FROM cursorDiskKV WHERE key=?",
                ("composerData:" + composer_id,),
            ).fetchone()
        finally:
            con.close()
        if row and row[0]:
            d = json.loads(row[0])
            meta = {
                "name": (d.get("name") or "").strip(),
                "status": (d.get("status") or "").strip(),
                "created_ms": d.get("createdAt"),
                "updated_ms": d.get("lastUpdatedAt"),
                # Checkpoint timestamp is written far more frequently than
                # lastUpdatedAt while a turn is actively generating, so it is the
                # best "is this conversation still doing work right now?" signal.
                # It freezes the instant a turn settles (incl. a manual stop).
                "checkpoint_ms": d.get("conversationCheckpointLastUpdatedAt"),
            }
    except Exception:
        meta = {}
    _COMPOSER_CACHE[composer_id] = (db_mtime, meta)
    return meta

# Persona emojis, index-aligned with the client-side name tables (PERSONAS_EN /
# PERSONAS_HE in PAGE). The server emits a persona_id; the browser localizes the
# name. Activity labels and the "task unavailable" placeholder are also localized
# client-side -- Python emits only language-neutral data and stable keys.
PERSONA_EMOJI = ["🕵️", "✍️", "🏃", "🔬", "📚", "🧭", "🔭", "🔨",
                 "🪄", "🎯", "🦉", "🦊", "🐝", "🤖", "🐯", "🦅"]


def persona_index(agent_id):
    h = 0
    for ch in (agent_id or ""):
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return h % len(PERSONA_EMOJI)


def iso_to_ms(s):
    if not s:
        return None
    try:
        return int(datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None


def read_first_line(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.readline()


def read_tail_lines(path, max_bytes=200_000):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
            data = f.read()
            nl = data.find(b"\n")
            if nl != -1:
                data = data[nl + 1:]
        else:
            data = f.read()
    return [ln for ln in data.decode("utf-8", errors="replace").split("\n") if ln.strip()]


class Event:
    """A normalized view of ONE raw JSONL line. The rest of the program only
    ever touches Event objects, never raw dicts -- so a Claude Code format
    change is absorbed in parse_agent_event() alone."""
    __slots__ = ("kind", "text", "tool_uses", "stop_reason", "ts_ms", "version", "raw")

    def __init__(self, kind, text, tool_uses, stop_reason, ts_ms, version, raw):
        self.kind = kind              # "user" | "assistant" | other type string | "unknown"
        self.text = text              # concatenated text blocks, "" if none
        self.tool_uses = tool_uses    # list of tool names invoked in this event
        self.stop_reason = stop_reason
        self.ts_ms = ts_ms
        self.version = version        # Claude Code version stamped on the line
        self.raw = raw                # original dict, for first-line meta only


def parse_agent_event(line):
    """The ONLY function that touches raw JSONL. Returns an Event, or None for a
    line we cannot use (blank / not JSON / not an object). Never raises:
    unknown keys are ignored, malformed lines degrade to None (the caller counts
    and skips them) rather than crashing the scan."""
    if not line or not line.strip():
        return None
    try:
        rec = json.loads(line)
    except Exception:
        return None
    if not isinstance(rec, dict):
        return None

    # Cursor stamps the speaker on "role"; Claude Code used "type". Accept either
    # so this adapter stays the single place that knows the journal shape.
    rtype = rec.get("role") or rec.get("type")
    msg = rec.get("message")
    msg = msg if isinstance(msg, dict) else {}
    content = msg.get("content")

    text_parts, tool_uses = [], []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "text":
                t = block.get("text", "")
                if t:
                    text_parts.append(t)
            elif bt == "tool_use":
                tool_uses.append(block.get("name") or "")
    elif isinstance(content, str):
        if content:
            text_parts.append(content)

    return Event(
        kind=rtype if isinstance(rtype, str) and rtype else "unknown",
        text=" ".join(text_parts).strip(),
        tool_uses=[t for t in tool_uses if t],
        stop_reason=msg.get("stop_reason"),
        ts_ms=iso_to_ms(rec.get("timestamp")),
        version=rec.get("version"),
        raw=rec,
    )


def parse_events(lines):
    """Map raw lines -> Events, returning (events, skipped_count, versions_set)."""
    events, skipped, versions = [], 0, set()
    for ln in lines:
        ev = parse_agent_event(ln)
        if ev is None:
            if ln and ln.strip():
                skipped += 1
            continue
        events.append(ev)
        if ev.version:
            versions.add(ev.version)
    return events, skipped, versions


def last_tool_use_name(events):
    for ev in reversed(events):
        if ev.kind == "assistant" and ev.tool_uses:
            return ev.tool_uses[-1]
    return None


def detect_done(events):
    last = None
    for ev in reversed(events):
        if ev.kind in ("assistant", "user"):
            last = ev
            break
    if last is None or last.kind != "assistant":
        return False, None, None
    has_tool = bool(last.tool_uses)
    # Cursor transcripts carry no stop_reason, so a finished turn is simply the
    # last event being an assistant message with no trailing tool call. (When a
    # stop_reason IS present, e.g. a Claude Code journal, still honour it.)
    done = (not has_tool) and (
        last.stop_reason is None
        or last.stop_reason in ("end_turn", "stop", "stop_sequence", "max_tokens")
    )
    if done:
        full = " ".join(last.text.split())
        if len(full) > 4000:
            full = full[:4000] + "…"
        return True, last.ts_ms, full
    return False, None, None


_NAME_CACHE = {}  # parent_file -> (mtime, {prompt: {description, subagent_type}})
_PROJECT_CACHE = {}  # parent_file -> (mtime, project_cwd)  -- the conversation's real working dir
_SESSION_CACHE = {}  # session_file -> (mtime, (topic, cwd))  -- a top-level conversation's subject + dir


def parent_session_file(agent_path, session_id):
    if not session_id:
        return None
    p = os.path.dirname(agent_path)
    while p and os.path.basename(p) != session_id:
        nxt = os.path.dirname(p)
        if nxt == p:
            return None
        p = nxt
    return p + ".jsonl"


def name_map_for(parent_file):
    if not parent_file or not os.path.isfile(parent_file):
        return {}
    try:
        mtime = os.path.getmtime(parent_file)
    except OSError:
        return {}
    cached = _NAME_CACHE.get(parent_file)
    if cached and cached[0] == mtime:
        return cached[1]
    m = {}
    try:
        with open(parent_file, "r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                if '"type":"tool_use"' not in ln or '"description"' not in ln:
                    continue
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue
                if rec.get("type") != "assistant":
                    continue
                content = rec.get("message", {}).get("content", [])
                if not isinstance(content, list):
                    continue
                for block in content:
                    if (isinstance(block, dict) and block.get("type") == "tool_use"
                            and block.get("name") in ("Task", "Agent")):
                        inp = block.get("input") or {}
                        prompt = inp.get("prompt")
                        if prompt:
                            m[prompt.strip()] = {
                                "description": inp.get("description", "") or "",
                                "subagent_type": inp.get("subagent_type", "") or "",
                            }
    except Exception:
        pass
    _NAME_CACHE[parent_file] = (mtime, m)
    return m


def project_cwd_for(parent_file):
    """The parent conversation's real working directory, read from its first event.
    Used as the room label so rooms map to projects the user recognizes (e.g.
    "Downloads", "agent-theater") instead of a deeply-nested subagent cwd."""
    if not parent_file or not os.path.isfile(parent_file):
        return ""
    try:
        mtime = os.path.getmtime(parent_file)
    except OSError:
        return ""
    cached = _PROJECT_CACHE.get(parent_file)
    if cached and cached[0] == mtime:
        return cached[1]
    # The first lines of a session file can be metadata (queue-operation) with no
    # cwd; the working dir appears on the first user/assistant record. Scan a few
    # lines for the first non-empty "cwd".
    cwd = ""
    try:
        with open(parent_file, "r", encoding="utf-8", errors="replace") as f:
            for i, ln in enumerate(f):
                if i > 50:
                    break
                if '"cwd"' not in ln:
                    continue
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue
                c = rec.get("cwd")
                if c:
                    cwd = c
                    break
    except Exception:
        cwd = ""
    _PROJECT_CACHE[parent_file] = (mtime, cwd)
    return cwd


def _first_user_text(rec):
    """The text of a user message record (string content, or the first text block)."""
    msg = rec.get("message", {})
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        for b in c:
            if isinstance(b, dict) and b.get("type") == "text":
                return b.get("text", "") or ""
            if isinstance(b, str):
                return b
    return ""


import re as _re

# Cursor wraps the real prompt in tags (<user_query>, <timestamp>, etc.) and
# prepends env/context blocks. Pull out the human prompt for a clean room title.
_QUERY_RE = _re.compile(r"<user_query>\s*(.*?)\s*</user_query>", _re.DOTALL)
_TAG_RE = _re.compile(r"<[^>]+>")


def clean_prompt(text):
    """Best-effort human prompt from a Cursor user turn: prefer the <user_query>
    body, else strip any XML-ish wrapper tags. Never raises."""
    if not text:
        return ""
    m = _QUERY_RE.search(text)
    if m:
        text = m.group(1)
    else:
        text = _TAG_RE.sub(" ", text)
    return " ".join(text.split())


_LABEL_CACHE = {}  # encoded folder name -> resolved room label


def _decode_project_dir(enc):
    """Cursor encodes a project's absolute path as one dash-joined folder name
    (slashes -> dashes), which is ambiguous because real names contain dashes.
    Reconstruct it by greedily matching the longest existing directory at each
    step against the live filesystem. Returns the real path, or None."""
    segs = enc.split("-")
    cur = os.sep
    i = 0
    while i < len(segs):
        matched = None
        for j in range(len(segs), i, -1):
            cand = "-".join(segs[i:j])
            if os.path.isdir(os.path.join(cur, cand)):
                matched = (cand, j)
                break
        if matched is None:
            return None
        cur = os.path.join(cur, matched[0])
        i = matched[1]
    return cur


def project_label(path):
    """A human room label for a transcript path: the project's real folder name.
    Resolved from the encoded folder via the filesystem and cached; falls back to
    the encoded name with the universal '/Users/<name>/' prefix stripped."""
    parts = path.replace("\\", "/").split("/")
    try:
        enc = parts[parts.index("agent-transcripts") - 1]
    except ValueError:
        return ""
    if enc in _LABEL_CACHE:
        return _LABEL_CACHE[enc]
    label = ""
    real = _decode_project_dir(enc)
    if real:
        label = os.path.basename(real.rstrip(os.sep))
    if not label:
        segs = enc.split("-")
        if len(segs) > 2 and segs[0] == "Users":
            segs = segs[2:]
        label = "-".join(segs) or enc
    _LABEL_CACHE[enc] = label
    return label


def session_summary(session_file):
    """A conversation's subject (its first user message, cleaned of Cursor's tag
    wrappers) and a room label derived from the project folder. Cached by mtime;
    scan a bounded prefix to find the first user turn."""
    try:
        mtime = os.path.getmtime(session_file)
    except OSError:
        return ("", "")
    cached = _SESSION_CACHE.get(session_file)
    if cached and cached[0] == mtime:
        return cached[1]
    cwd = project_label(session_file)
    topic = ""
    try:
        with open(session_file, "r", encoding="utf-8", errors="replace") as f:
            for i, ln in enumerate(f):
                if i > 80:
                    break
                if '"role":"user"' not in ln and '"type":"user"' not in ln:
                    continue
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue
                kind = rec.get("role") or rec.get("type")
                if kind == "user":
                    topic = clean_prompt(_first_user_text(rec))
                    if topic:
                        break
    except Exception:
        pass
    res = (topic, cwd)
    _SESSION_CACHE[session_file] = (mtime, res)
    return res


def scan_sessions(now):
    """Each Cursor conversation transcript becomes a room-leading character. Status,
    the current tool, and the finished/result text are derived from the transcript
    events (Cursor has no separate per-subagent files). start_ms is last-activity
    time so the timer reads as 'active/idle' rather than the full session age."""
    entries = []
    try:
        paths = glob.glob(TRANSCRIPT_GLOB)
    except Exception:
        paths = []
    for path in paths:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if (now - mtime) / 60.0 > MAX_AGE_MIN:
            continue
        topic, cwd = session_summary(path)
        uuid = os.path.basename(path)[:-6]
        try:
            events, _skip, _vers = parse_events(read_tail_lines(path))
        except Exception:
            events = []
        is_done, _end_ms, result = detect_done(events)
        tool = last_tool_use_name(events) or ""
        pid = persona_index(uuid)
        mtime_ms = int(mtime * 1000)
        # Cursor's own chat title + real timestamps (the desk label and timers);
        # fall back to the first message / file mtime when the store is unreadable.
        meta = composer_meta(uuid)
        title = meta.get("name") or topic
        start_ms = meta.get("created_ms") or mtime_ms
        # Liveness is decided on the FRESHEST signal we have. Cursor often
        # buffers the transcript .jsonl (its mtime can lag many seconds behind a
        # conversation that is actively streaming/thinking), which made live
        # chats wrongly read as idle. The composer store's lastUpdatedAt updates
        # far more promptly, so take whichever is newer.
        last_activity_ms = max(mtime_ms, meta.get("updated_ms") or 0)
        # end_ms (the "finished N ago" timestamp) must be the freshest activity
        # too -- using the store's lagging updated_ms alone made a just-finished
        # chat read as "~4 min ago" when the transcript mtime showed it ended now.
        end_ms = last_activity_ms or mtime_ms
        # Liveness model: recency is the PRIMARY signal, because Cursor does NOT
        # expose a reliable "generating right now" flag while a turn is in flight.
        # Its stored `status` is only the LAST settled outcome ("aborted" when you
        # stop a turn, "completed" when it finishes) and it lags reality -- an
        # actively-working chat still reads "aborted" with a stale lastUpdatedAt
        # for the whole turn, and a chat you just gave a new command to also still
        # reads "aborted" until streaming starts. So we trust an explicit
        # "generating" status when present, otherwise fall back to recency and do
        # NOT let a settled store-status force a working chat to idle.
        live_status = (meta.get("status") or "").lower()
        is_generating = live_status in ("generating", "running", "streaming", "thinking")
        # A manual stop is the one terminal state Cursor records distinctly
        # ("aborted"). We only trust it once the checkpoint has gone quiet for
        # ABORTED_IDLE_SEC, because a still-working chat can momentarily read
        # "aborted" mid-turn while its checkpoint keeps advancing.
        checkpoint_ms = meta.get("checkpoint_ms") or 0
        aborted_idle = (
            live_status == "aborted"
            and checkpoint_ms
            and (now - checkpoint_ms / 1000.0) > ABORTED_IDLE_SEC
        )
        if is_done and not is_generating:
            status = "done"
        elif aborted_idle:
            status = "aborted"   # user manually stopped it (distinct from naturally idle)
        elif is_generating or (now - last_activity_ms / 1000.0) <= RUNNING_STALE_SEC:
            status = "running"
        else:
            status = "stale"
        # surface the freshest activity time as the desk's mtime so the timer and
        # the room sort use the same live signal the status does.
        mtime_ms = last_activity_ms or mtime_ms
        entries.append({
            "id": uuid, "persona_id": pid, "emoji": PERSONA_EMOJI[pid],
            "role": "", "subagent_type": "", "title": title,
            "status": status, "tool": tool,
            "task": topic, "task_short": short_task(topic),
            "result": result if is_done else None,
            "start_ms": start_ms, "end_ms": end_ms if is_done else None,
            "session": uuid[:8], "session_full": uuid,
            "cwd": cwd, "project": cwd, "mtime_ms": mtime_ms,
            "is_session": True,
        })
    return entries


def extract_task(first_ev):
    return first_ev.text if first_ev else ""


def short_task(task):
    task = " ".join(task.split())
    for sep in (". ", "? ", "! ", ": "):
        idx = task.find(sep)
        if 0 < idx < 90:
            return task[:idx + 1].strip()
    return task[:90].strip() + "…" if len(task) > 90 else task


# path -> (mtime, size, agent_dict, is_done, file_versions, file_skipped). The
# expensive part of a scan is read_tail_lines (up to 200 KB) + parse on every
# recent file, every 1.5 s. We cache the parsed result keyed by (mtime, size)
# and only recompute the wall-clock-dependent `status` on a cache hit. Same
# discipline as _NAME_CACHE; the parser stays isolated -- the cache wraps it.
_AGENT_CACHE = {}


def scan_agents():
    now = time.time()
    pattern = os.path.join(PROJECTS_DIR, "**", "agent-*.jsonl")
    agents = []
    versions = set()
    skipped = 0
    try:
        paths = glob.glob(pattern, recursive=True)
    except Exception:
        paths = []
    seen = set()
    for path in paths:
        try:
            stt = os.stat(path)
        except OSError:
            continue
        mtime, size = stt.st_mtime, stt.st_size
        if (now - mtime) / 60.0 > MAX_AGE_MIN:
            continue
        seen.add(path)

        cached = _AGENT_CACHE.get(path)
        if cached and cached[0] == mtime and cached[1] == size:
            _, _, adict, is_done, fvers, fskip = cached
        else:
            try:
                first_line = read_first_line(path)
            except Exception:
                first_line = ""
            if not (first_line and first_line.strip()):
                continue  # file exists but first line not flushed yet -- not malformed
            first_ev = parse_agent_event(first_line)
            if first_ev is None:
                skipped += 1
                continue
            fvers = set()
            if first_ev.version:
                fvers.add(first_ev.version)

            agent_id = first_ev.raw.get("agentId") or os.path.basename(path)[6:-6]
            session = first_ev.raw.get("sessionId", "") or ""
            start_ms = first_ev.ts_ms
            task = extract_task(first_ev)

            try:
                events, n_skip, vers = parse_events(read_tail_lines(path))
            except Exception:
                events, n_skip, vers = [], 0, set()
            fskip = n_skip
            fvers |= vers

            is_done, end_ms, result = detect_done(events)
            tool = last_tool_use_name(events)
            pid = persona_index(agent_id)
            parent = parent_session_file(path, session)
            info = name_map_for(parent).get(task.strip()) if task.strip() else None
            project = project_cwd_for(parent)
            # Language-neutral payload only: the browser localizes persona name,
            # activity label and the placeholder for an agent with no readable task
            # (degrade-not-crash -- it still shows up, just with a generic label).
            # `status` is a placeholder here; it is recomputed below every scan.
            adict = {
                "id": agent_id, "persona_id": pid, "emoji": PERSONA_EMOJI[pid],
                "role": info["description"] if info else "",
                "subagent_type": info["subagent_type"] if info else "",
                "status": "running", "tool": tool or "",
                "task": task, "task_short": short_task(task), "result": result,
                "start_ms": start_ms, "end_ms": end_ms,
                "session": session[:8], "session_full": session,
                "cwd": first_ev.raw.get("cwd", ""), "project": project,
                "mtime_ms": int(mtime * 1000), "is_session": False,
            }
            _AGENT_CACHE[path] = (mtime, size, adict, is_done, fvers, fskip)

        versions |= fvers
        skipped += fskip
        # status tracks wall-clock `now`, so recompute it on every scan (even a cache hit)
        if is_done:
            status = "done"
        elif (now - mtime) > RUNNING_STALE_SEC:
            status = "stale"
        else:
            status = "running"
        a = dict(adict)
        a["status"] = status
        # role/subagent_type come from the PARENT session file, not the agent
        # file, so the agent-keyed (mtime,size) cache can't notice the parent
        # gaining its Task block later. Re-resolve every scan (name_map_for is
        # itself mtime-cached on the parent, so this is cheap) and overwrite the
        # copy; the cached adict and the isolated parser stay untouched.
        _task = a["task"].strip()
        if _task:
            _info = name_map_for(parent_session_file(path, a["session_full"])).get(_task)
            if _info:
                a["role"] = _info["description"]
                a["subagent_type"] = _info["subagent_type"]
        agents.append(a)

    # evict entries for files that aged out / vanished so the cache can't grow unbounded
    for gone in [p for p in _AGENT_CACHE if p not in seen]:
        del _AGENT_CACHE[gone]

    # Top-level conversations as room leads, so every recent conversation shows up
    # (not only those that spawned subagents). They share a room with their subagents
    # (same session id). is_session sorts first within a status so the lead shows first.
    agents.extend(scan_sessions(now))

    order = {"running": 0, "stale": 1, "aborted": 1, "done": 2}
    agents.sort(key=lambda a: (order.get(a["status"], 3), -(1 if a.get("is_session") else 0), -(a["start_ms"] or 0)))
    return {
        "agents": agents,
        # "skipped" (malformed-line count) is surfaced by the UI diagnostics footer.
        "skipped": skipped,
    }


# ---------------------------------------------------------------------------
# Demo mode (--demo): a synthetic, populated office for screenshots / the Hero
# GIF and for a "try it instantly" first run. Builds the payload in memory and
# NEVER reads or writes the real ~/.claude/projects journals.
# ---------------------------------------------------------------------------
def _demo_agent(aid, session, cwd, status, tool, task, role="", subagent_type="", start_offset=60,
                result=None, is_session=False, mtime_offset=0):
    now = time.time()
    pid = persona_index(aid)
    return {
        "id": aid, "persona_id": pid, "emoji": PERSONA_EMOJI[pid],
        "role": role, "subagent_type": subagent_type,
        "status": status, "tool": tool or "",
        "task": task, "task_short": short_task(task),
        "result": result if status == "done" else None,
        "start_ms": int((now - start_offset) * 1000),
        "end_ms": int((now - 2) * 1000) if status == "done" else None,
        "session": session[:8], "session_full": session,
        "cwd": cwd, "project": cwd, "mtime_ms": int((now - mtime_offset) * 1000),
        "is_session": is_session,
    }


def _demo_payload_envelope(agents):
    order = {"running": 0, "stale": 1, "aborted": 1, "done": 2}
    agents.sort(key=lambda x: (order.get(x["status"], 3), -(1 if x.get("is_session") else 0), -(x["start_ms"] or 0)))
    return {
        "agents": agents,
        "skipped": 0,
    }


def _demo_rooms4():
    """A 4-instance office: two rooms are actively working, two are fully
    finished (every conversation + subagent done). Lets you see how the grid
    handles several rooms at once and how an all-done room reads."""
    a = []
    # --- Room 1: acme-web — actively working ---
    p = "/home/dev/acme-web"; s = "demo4-acme-web-1111"
    a += [
        _demo_agent("d4-aw-conv", s, p, "running", "",
                    "Ship the v2 config migration and clean up the auth middleware.",
                    start_offset=420, is_session=True, mtime_offset=5),
        _demo_agent("d4-aw-read", s, p, "running", "Read",
                    "Read the auth middleware and map every place the session token is validated.",
                    role="map session-token validation", subagent_type="Explore", start_offset=44),
        _demo_agent("d4-aw-edit", s, p, "running", "Edit",
                    "Apply the review fixes to the config loader and re-run the type checker.",
                    role="apply the review fixes", subagent_type="general-purpose", start_offset=9),
        _demo_agent("d4-aw-test", s, p, "aborted", "Bash",
                    "Run the full test suite and report any failures.", start_offset=300),
    ]
    # --- Room 2: payments-api — actively working ---
    p = "/home/dev/payments-api"; s = "demo4-payments-2222"
    a += [
        _demo_agent("d4-pa-conv", s, p, "running", "",
                    "Add idempotency keys to the charge endpoint and tighten retries.",
                    start_offset=260, is_session=True, mtime_offset=11),
        _demo_agent("d4-pa-search", s, p, "running", "WebSearch",
                    "Research idempotency-key patterns used by Stripe and PayPal.",
                    role="research idempotency patterns", subagent_type="general-purpose", start_offset=70),
        _demo_agent("d4-pa-mcp", s, p, "running", "mcp__github__search_issues",
                    "Pull open issues labeled 'payments' and cluster them by component.",
                    role="triage payment bugs", subagent_type="general-purpose", start_offset=33),
    ]
    # --- Room 3: design-system — fully finished ---
    p = "/home/dev/design-system"; s = "demo4-design-3333"
    a += [
        _demo_agent("d4-ds-conv", s, p, "done", "",
                    "Tokenize the spacing scale and migrate the Button component.",
                    start_offset=900, is_session=True, mtime_offset=180,
                    result="Done. Spacing tokens extracted to tokens.css and Button migrated to use them; "
                           "visual diff is clean across all stories."),
        _demo_agent("d4-ds-write", s, p, "done", "Write",
                    "Generate the spacing-token CSS variables from the Figma export.",
                    role="generate spacing tokens", subagent_type="general-purpose", start_offset=760,
                    result="Wrote tokens.css with 8 spacing steps (4-64px) and wired them into the Tailwind config."),
        _demo_agent("d4-ds-edit", s, p, "done", "Edit",
                    "Migrate the Button component to the new spacing tokens.",
                    role="migrate Button", subagent_type="general-purpose", start_offset=540,
                    result="Replaced 14 hardcoded paddings/margins in Button with token references. Snapshots updated."),
    ]
    # --- Room 4: marketing-site — fully finished ---
    p = "/home/dev/marketing-site"; s = "demo4-marketing-4444"
    a += [
        _demo_agent("d4-ms-conv", s, p, "done", "",
                    "Fix the broken pricing-page links and audit the SEO meta tags.",
                    start_offset=700, is_session=True, mtime_offset=240,
                    result="Done. Fixed 6 broken links and filled in missing meta descriptions on 12 pages."),
        _demo_agent("d4-ms-grep", s, p, "done", "Grep",
                    "Find every internal link on the pricing page and check each target exists.",
                    role="audit pricing links", subagent_type="Explore", start_offset=620,
                    result="Found 6 links pointing at removed anchors; listed each with its source line."),
    ]
    return _demo_payload_envelope(a)


def demo_payload(phase=None, scene=""):
    if scene == "rooms4":
        return _demo_rooms4()
    now = time.time()
    cwd = "/home/dev/acme-web"
    s1 = "demo-session-frontend-1111"
    s2 = "demo-session-research-2222"
    # A ~12 s scripted loop so a single short GIF captures every beat in one pass:
    #   phase 3  -> a new agent walks in   (entering animation)
    #   phase 6  -> the finisher completes (confetti + chime)
    # int(now) % 12 drives both; the rest of the office stays steady.
    phase = (phase % 12) if isinstance(phase, int) else int(now) % 12
    finishing = 6 <= phase < 10
    walked_in = phase >= 3
    agents = [
        _demo_agent("demo-research-aa", s2, cwd, "running", "WebSearch",
                    "Research incremental static regeneration approaches and summarize the trade-offs.",
                    role="research the ISR landscape", subagent_type="general-purpose", start_offset=95),
        _demo_agent("demo-reader-bb", s1, cwd, "running", "Read",
                    "Read the auth middleware and map every place the session token is validated.",
                    role="map session-token validation", subagent_type="Explore", start_offset=42),
        _demo_agent("demo-grep-cc", s1, cwd, "running", "Grep",
                    "Find all TODO and FIXME comments across the repo and group them by file.",
                    start_offset=18),
        _demo_agent("demo-mcp-dd", s2, cwd, "running", "mcp__github__search_issues",
                    "Pull the open issues labeled 'bug' and cluster them by component.",
                    role="triage open bugs", subagent_type="general-purpose", start_offset=63),
        _demo_agent("demo-build-ee", s1, cwd, "stale", "Bash",
                    "Run the full test suite and report any failures.", start_offset=320),
        _demo_agent("demo-writer-ff", s2, cwd, "done", "Write",
                    "Draft the migration guide for the v2 config format.",
                    role="draft the v2 migration guide", subagent_type="general-purpose", start_offset=150,
                    result="Done. Wrote migration-v2.md: a step-by-step guide covering the renamed keys, the "
                           "deprecation timeline, and a codemod snippet. Flagged two breaking changes for manual review."),
        _demo_agent("demo-finisher-gg", s1, cwd, "done" if finishing else "running", "StructuredOutput",
                    "Summarize the security review findings into a prioritized list.",
                    role="summarize the security review", subagent_type="code-reviewer", start_offset=51,
                    result="Summary: 3 high, 5 medium, 11 low. Top item: the password-reset token is not "
                           "compared in constant time."),
    ]
    if walked_in:  # appears mid-loop so the browser plays its walk-in animation
        agents.append(_demo_agent("demo-newcomer-hh", s2, cwd, "running", "Edit",
                      "Apply the review fixes to the config loader and re-run the type checker.",
                      role="apply the review fixes", subagent_type="general-purpose", start_offset=3))
    # the two conversations themselves -> each leads its room with the topic as the title
    agents.append(_demo_agent("demo-conv-frontend", s1, cwd, "running", "",
                  "Ship the v2 config migration and clean up the auth middleware.",
                  start_offset=380, is_session=True, mtime_offset=7))
    agents.append(_demo_agent("demo-conv-research", s2, cwd, "running", "",
                  "Plan the static-regeneration rollout and triage the bug backlog.",
                  start_offset=300, is_session=True, mtime_offset=14))
    return _demo_payload_envelope(agents)


# === BEGIN GENERATED PAGE (do not edit by hand; source of truth: ui/theater.html;
# regenerate with: python3 build_ui.py) ============================================
PAGE = """<!DOCTYPE html>
<html lang="en" dir="ltr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cursor Theater</title>
<script>try{var Q=(location.search.match(/[?&]lang=(he|en)\\b/)||[])[1];
  var L=Q||localStorage.getItem("ct_lang")||"en";if(L!=="en"&&L!=="he")L="en";
  document.documentElement.lang=L; document.documentElement.dir=(L==="he")?"rtl":"ltr";
  // theme: explicit choice wins; else follow the OS. Applied before first paint to avoid a flash.
  var TQ=(location.search.match(/[?&]theme=(light|dark)\\b/)||[])[1];
  var TH=TQ||localStorage.getItem("ct_theme");
  if(TH!=="light"&&TH!=="dark"){ TH=(window.matchMedia&&window.matchMedia("(prefers-color-scheme: light)").matches)?"light":"dark"; }
  document.documentElement.setAttribute("data-theme",TH);}catch(e){}</script>
<style>
  :root{
    color-scheme:dark;
    /* ---- color tokens (one place to reskin / theme; see ROADMAP) ----
       OLED-dark "real-time monitoring" palette: near-black base, slate
       surfaces, status colors carry all the meaning (run=green, idle=amber,
       done=indigo). */
    --bg:#070a12; --bg-glow:#0d1424;
    --surface:#10151f; --surface-2:#0c111b; --surface-3:#0a0e17; --surface-hi:#161d2b;
    --ink:#f2f5fc; --ink-2:#d6dcec; --ink-dim:#9aa6c2; --ink-dimmer:#6f7b97;
    --ok:#34d27b; --ok-soft:rgba(52,210,123,.14); --ok-line:rgba(52,210,123,.4);
    --idle:#e7b65a; --idle-soft:rgba(231,182,90,.14);
    --abort:#ef6b6b; --abort-soft:rgba(239,107,107,.14);
    --done:#7c8cf0; --done-soft:rgba(124,140,240,.16);
    --accent:#6b7cff;
    --line:#1c2335; --line-soft:#161c2b; --line-head:#1a2132; --line-drawer:#222b42;
    --chip-bg:#161d2b; --chip-line:#28324a; --chip-ink:#c4cce4;
    /* tool-family accents (the activity dot + monitor cue) */
    --fam-search:#33c7e0; --fam-read:#6b9bff; --fam-write:#e7a93a; --fam-cmd:#34d27b; --fam-agent:#c477e0;
    /* ---- type scale ---- */
    --fs-xl:19px; --fs-lg:14.5px; --fs-md:13px; --fs-sm:11.5px; --fs-xs:10.5px;
    --mono:ui-monospace,"SF Mono","JetBrains Mono","Cascadia Code","Roboto Mono",Menlo,Consolas,monospace;
    /* ---- radii / focus ring ---- */
    --r-sm:9px; --r-md:12px; --r-lg:16px; --r-pill:999px;
    --ring:0 0 0 2px var(--bg),0 0 0 4px var(--accent);
    /* themeable bits the dark base also uses (overridden in light below) */
    --header-bg:rgba(8,11,18,.78); --header-bg-solid:#0a0e17;
    --glow-1:var(--bg-glow); --glow-2:rgba(108,124,255,.07);
    --avatar-grad-1:var(--surface-hi); --avatar-grad-2:var(--surface-3);
    --shadow-room:0 8px 30px rgba(0,0,0,.28);
    --shadow-drawer:-16px 0 50px rgba(0,0,0,.55); --shadow-drawer-rtl:16px 0 50px rgba(0,0,0,.55);
    --shadow-toast:0 10px 30px rgba(0,0,0,.55);
    --backdrop:rgba(3,5,12,.62);
  }
  /* ---------- light theme: same tokens, daylight values. The skill's
     contrast rules drive these (slate-900 ink, slate-600 muted, visible
     borders, status colors darkened for AA on white). ---------- */
  html[data-theme="light"]{
    color-scheme:light;
    --bg:#f3f5fa; --bg-glow:#e7ecf7;
    --surface:#ffffff; --surface-2:#f7f9fd; --surface-3:#eef1f7; --surface-hi:#eef2fb;
    --ink:#0f172a; --ink-2:#1e293b; --ink-dim:#475569; --ink-dimmer:#64748b;
    --ok:#0f9d58; --ok-soft:rgba(15,157,88,.12); --ok-line:rgba(15,157,88,.42);
    --idle:#b8791b; --idle-soft:rgba(184,121,27,.14);
    --abort:#cf3a3a; --abort-soft:rgba(207,58,58,.12);
    --done:#4f5bd5; --done-soft:rgba(79,91,213,.14);
    --accent:#4f5bd5;
    --line:#dbe2ee; --line-soft:#e6ebf3; --line-head:#dde3ee; --line-drawer:#d3dbe8;
    --chip-bg:#eef2fb; --chip-line:#d3dbe8; --chip-ink:#334155;
    --fam-search:#0e93aa; --fam-read:#3b6fd6; --fam-write:#c07d12; --fam-cmd:#0f9d58; --fam-agent:#9b4ec4;
    --header-bg:rgba(255,255,255,.82); --header-bg-solid:#ffffff;
    --glow-1:#e7ecf7; --glow-2:rgba(79,91,213,.06);
    --avatar-grad-1:#ffffff; --avatar-grad-2:#eef1f7;
    --shadow-room:0 4px 18px rgba(15,23,42,.07);
    --shadow-drawer:-16px 0 50px rgba(15,23,42,.18); --shadow-drawer-rtl:16px 0 50px rgba(15,23,42,.18);
    --shadow-toast:0 10px 30px rgba(15,23,42,.18);
    --backdrop:rgba(15,23,42,.35);
  }
  *{ box-sizing:border-box; }
  body{ margin:0; padding:0; color:var(--ink); font-size:var(--fs-md);   /* override the webview host's injected body padding (0 20px) so the side gutters match the browser */
        font-family:"Inter","Segoe UI","Arial Hebrew",system-ui,-apple-system,sans-serif;
        background:
          radial-gradient(900px 480px at 18% -8%,var(--glow-1),transparent 60%),
          radial-gradient(900px 480px at 100% 0%,var(--glow-2),transparent 55%),
          var(--bg);
        background-attachment:fixed; }
  :focus-visible{ outline:none; box-shadow:var(--ring); border-radius:var(--r-sm); }

  /* ---------- header ---------- */
  header{ display:flex; align-items:center; gap:12px 16px; flex-wrap:wrap; padding:13px 22px;
          border-bottom:1px solid var(--line-head); position:sticky; top:0; z-index:40;
          background:var(--header-bg); backdrop-filter:blur(14px) saturate(1.2); }
  @supports not ((backdrop-filter:blur(1px)) or (-webkit-backdrop-filter:blur(1px))){ header{ background:var(--header-bg-solid); } }
  header h1{ font-size:var(--fs-xl); margin:0; font-weight:750; letter-spacing:-.2px; display:inline-flex; align-items:center; gap:9px; }
  .counts{ display:inline-flex; gap:7px; }
  .counts span{ display:inline-flex; align-items:center; gap:5px; padding:4px 11px; border-radius:var(--r-pill);
        font-size:var(--fs-sm); font-weight:600; line-height:1; border:1px solid transparent; }
  .counts .ic{ width:13px; height:13px; display:block; flex:none; }
  .counts b{ font-family:var(--mono); font-weight:600; line-height:1; display:inline-flex; align-items:center; }
  .counts em{ font-style:normal; line-height:1; display:inline-flex; align-items:center; }
  .c-run{ background:var(--ok-soft); color:var(--ok); border-color:var(--ok-line); }
  .c-idle{ background:var(--idle-soft); color:var(--idle); border-color:rgba(231,182,90,.32); }
  .c-abort{ background:var(--abort-soft); color:var(--abort); border-color:rgba(239,107,107,.32); }
  .c-done{ background:var(--done-soft); color:var(--done); border-color:rgba(124,140,240,.32); }
  .spacer{ flex:1; }
  .tools{ display:inline-flex; align-items:center; gap:10px 12px; flex-wrap:wrap; }
  header .htoggle{ display:none; }   /* collapse control: only shown on narrow widths (specificity beats .iconbtn) */
  header label{ font-size:var(--fs-md); color:var(--ink-dim); display:inline-flex; align-items:center; gap:7px; cursor:pointer;
        user-select:none; padding:6px 10px; border-radius:var(--r-sm); transition:background .15s,color .15s; }
  header label:hover{ background:var(--surface-hi); color:var(--ink-2); }
  header label input{ accent-color:var(--accent); width:15px; height:15px; cursor:pointer; }
  /* "Show finished" rendered as a sliding toggle switch */
  .switch{ appearance:none; -webkit-appearance:none; margin:0; position:relative; flex:none; cursor:pointer;
        width:34px; height:20px; border-radius:999px; background:var(--chip-bg); border:1px solid var(--chip-line);
        transition:background .18s,border-color .18s; }
  .switch::after{ content:""; position:absolute; top:50%; inset-inline-start:2px; transform:translateY(-50%);
        width:14px; height:14px; border-radius:50%; background:var(--ink-dim); transition:inset-inline-start .18s,background .18s,transform .18s; }
  .switch:checked{ background:var(--accent); border-color:var(--accent); }
  .switch:checked::after{ inset-inline-start:calc(100% - 16px); background:#fff; }
  .switch:focus-visible{ box-shadow:0 0 0 2px rgba(107,124,255,.4); }
  .diag{ text-align:center; color:var(--ink-dimmer); font-size:var(--fs-sm); padding:0 18px 30px; }
  .diag[hidden]{ display:none; }
  .reconnect{ margin:0; padding:7px 16px; font-size:var(--fs-md); text-align:center;
              background:#2c1416; color:#f0a9a9; border-bottom:1px solid #4a2024; }
  .reconnect[hidden]{ display:none; }

  /* ---------- header control buttons ---------- */
  /* a fixed 32px square/pill where the glyph is centred by both axes; emoji and
     SVG alike are wrapped so their baseline can't push them off-centre. */
  .iconbtn{ font:inherit; font-size:var(--fs-md); background:var(--chip-bg); border:1px solid var(--chip-line); color:var(--ink-2);
            border-radius:var(--r-sm); padding:0 11px; cursor:pointer; line-height:1; height:32px; min-width:32px;
            display:inline-flex; align-items:center; justify-content:center; gap:6px; transition:background .15s,border-color .15s; }
  .iconbtn .ic{ display:block; width:17px; height:17px; }
  /* square icon-only buttons (bell, theme) */
  #muteBtn, #themeBtn{ padding:0; width:32px; }
  .iconbtn:hover{ background:var(--surface-hi); border-color:var(--line-drawer); }
  #langBtn{ font-size:var(--fs-sm); font-weight:600; }
  .sr-only{ position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; }
  .toasts{ position:fixed; bottom:18px; inset-inline-end:18px; z-index:80; display:flex; flex-direction:column; gap:8px; pointer-events:none; }
  .toast{ background:var(--surface-hi); border:1px solid var(--line-drawer); border-inline-start:3px solid var(--ok); color:var(--ink);
          border-radius:var(--r-sm); padding:10px 15px; font-size:var(--fs-md); box-shadow:var(--shadow-toast);
          max-width:300px; opacity:0; transform:translateY(8px); transition:opacity .25s,transform .25s; }
  .toast.show{ opacity:1; transform:translateY(0); }
  #search{ font:inherit; font-size:var(--fs-md); background:var(--surface-3); border:1px solid var(--chip-line);
           color:var(--ink); border-radius:var(--r-sm); padding:0 12px; width:210px; max-width:42vw; height:32px; line-height:30px; }
  #search::-webkit-search-cancel-button{ align-self:center; }
  #search::placeholder{ color:var(--ink-dimmer); }
  #search:focus-visible{ outline:none; border-color:var(--accent); box-shadow:0 0 0 2px rgba(107,124,255,.28); }

  /* Rooms tile into as many columns as the width allows. */
  #app{ padding:20px 16px 64px; display:grid; gap:18px; align-items:start;
        grid-template-columns:repeat(auto-fit,minmax(min(380px,100%),1fr)); }
  #app > .empty, #app > .boot{ grid-column:1/-1; }
  /* A busy room (many chats) earns extra width by spanning two grid tracks,
     then flows its conversation list into multiple columns -- instead of
     eating a full-width row with dead space on the side. On a single-column
     (mobile) grid the span is clamped to 1 so nothing overflows. */
  .room.wide{ grid-column:span 2; }
  @media (max-width:820px){ .room.wide{ grid-column:span 1; } }

  /* Narrow / docked-sidebar responsive rules live at the END of this stylesheet
     (after all base component rules) so equal-specificity overrides win by source
     order -- see the @media blocks at the bottom, just before the closing tag. */
  .empty{ text-align:center; color:var(--ink-dimmer); font-size:var(--fs-lg); padding:64px 10px; }
  .empty .e-scene{ width:60px; height:60px; margin:0 auto 16px; opacity:.5; }
  .empty .e-scene svg{ width:100%; height:100%; }
  .empty .e-title{ font-size:17px; color:var(--ink-2); font-weight:700; margin-bottom:7px; }
  .empty .e-sub{ font-size:var(--fs-md); color:var(--ink-dim); margin-bottom:20px; max-width:420px; margin-inline:auto; line-height:1.55; }
  .btn-demo{ font:inherit; font-size:var(--fs-md); font-weight:650; color:#fff; cursor:pointer;
             background:linear-gradient(180deg,#6b7cff,#5563e6); border:1px solid #7886ff; border-radius:var(--r-sm);
             padding:10px 20px; box-shadow:0 6px 20px rgba(85,99,230,.4); transition:filter .15s,transform .15s; }
  .btn-demo:hover{ filter:brightness(1.08); transform:translateY(-1px); }
  .demo-chip{ display:inline-flex; align-items:center; gap:9px; font-size:var(--fs-sm); color:var(--accent); font-weight:600;
              background:rgba(107,124,255,.12); border:1px solid rgba(107,124,255,.4); border-radius:var(--r-pill); padding:3px 5px 3px 13px; }
  html[dir="rtl"] .demo-chip{ padding:3px 13px 3px 5px; }
  .demo-chip button{ font:inherit; font-size:var(--fs-sm); background:var(--chip-bg); border:1px solid var(--chip-line);
                     color:var(--ink-2); border-radius:var(--r-pill); padding:3px 11px; cursor:pointer; }
  .demo-chip[hidden]{ display:none; }
  /* Embedded (Cursor webview): no demo data source, so hide its affordances. */
  html.embedded .btn-demo, html.embedded #demoChip{ display:none !important; }

  /* ---------- room (one Cursor instance / project) ---------- */
  .room{ position:relative; background:linear-gradient(180deg,var(--surface),var(--surface-2));
         border:1px solid var(--line); border-radius:var(--r-lg); overflow:hidden;
         box-shadow:var(--shadow-room);
         transition:border-color .2s,box-shadow .2s; }
  .rh{ display:flex; align-items:center; gap:10px; padding:13px 16px;
       border-bottom:1px solid var(--line-soft); font-size:var(--fs-md); }
  .rt{ margin:0; font-size:var(--fs-lg); font-weight:700; color:var(--ink); display:inline-flex; align-items:center; gap:9px; min-width:0; }
  .rt .rt-name{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .rt small{ color:var(--ink-dimmer); font-weight:500; font-size:var(--fs-sm); font-family:var(--mono); flex:none; }
  .rt small .rc-pre, .rt small .rc-post{ display:none; }   /* "N chats" by default; the narrow breakpoint shows "(N)" instead */
  .rc{ color:var(--ink-dim); display:inline-flex; align-items:center; gap:9px; font-size:var(--fs-sm); }
  .rc .stat{ display:inline-flex; align-items:center; gap:4px; font-family:var(--mono); }
  .rc b{ color:var(--ok); font-weight:600; } .rc i{ font-style:normal; color:var(--idle); } .rc u{ text-decoration:none; color:var(--done); } .rc s{ text-decoration:none; color:var(--abort); }

  /* the room's project icon tile */
  .rt .rt-ico{ width:30px; height:30px; border-radius:9px; flex:none; display:inline-flex; align-items:center; justify-content:center;
        background:color-mix(in srgb,var(--room-accent,var(--accent)) 18%,transparent);
        color:var(--room-accent,var(--accent)); }
  .rt .rt-ico .ic{ width:17px; height:17px; }

  /* ---- inline SVG chrome icons ---- */
  .ic{ width:1em; height:1em; flex:none; vertical-align:-0.14em; }
  .rc .ic{ width:14px; height:14px; vertical-align:-0.2em; }
  h1 .ic{ color:var(--accent); width:24px; height:24px; }
  .rdone .ic, .c-done .ic{ color:inherit; }

  /* ---- live / connection indicator (header) ---- */
  .livestat{ display:inline-flex; align-items:center; gap:7px; font-size:var(--fs-sm); color:var(--ink-dim); white-space:nowrap;
        background:var(--surface-hi); border:1px solid var(--line); border-radius:var(--r-pill); padding:5px 12px 5px 10px; }
  .livedot{ width:8px; height:8px; border-radius:50%; background:var(--ok); flex:none; }
  .livestat.on .livedot{ animation:livepulse 2s ease-out infinite; }
  .livestat.off .livedot{ background:#e0655b; animation:none; }
  .livestat.off{ color:#e0a59f; }
  .livestat.boot .livedot{ background:var(--idle); animation:none; }
  @keyframes livepulse{ 0%{box-shadow:0 0 0 0 rgba(52,210,123,.55)} 70%{box-shadow:0 0 0 6px rgba(52,210,123,0)} 100%{box-shadow:0 0 0 0 rgba(52,210,123,0)} }

  /* ---- a room with running work draws the eye ---- */
  .room.active{ border-color:var(--ok-line); box-shadow:0 0 0 1px rgba(52,210,123,.12),0 8px 30px rgba(0,0,0,.3); }
  .room.active .rh{ background:linear-gradient(90deg,var(--ok-soft),transparent 70%); }

  /* ---- boot skeleton (until the first scan lands) ---- */
  .boot{ padding:8px 0 30px; }
  .boot .sk-title{ color:var(--ink-dimmer); text-align:center; font-size:var(--fs-md); margin-bottom:18px;
        display:flex; align-items:center; justify-content:center; gap:8px; }
  .boot .sk-grid{ display:grid; gap:18px; grid-template-columns:repeat(auto-fit,minmax(min(380px,100%),1fr)); }
  .boot .sk-room{ background:linear-gradient(180deg,var(--surface),var(--surface-2)); border:1px solid var(--line);
        border-radius:var(--r-lg); overflow:hidden; }
  .boot .sk-head{ height:52px; background:var(--surface-3); border-bottom:1px solid var(--line-soft); }
  .boot .sk-floor{ display:flex; flex-direction:column; gap:10px; padding:14px; }
  .boot .sk-row{ height:56px; border-radius:var(--r-md); background:var(--surface-3); }
  .boot .shimmer{ position:relative; overflow:hidden; }
  .boot .shimmer::after{ content:""; position:absolute; inset:0; transform:translateX(-100%);
        background:linear-gradient(90deg,transparent,rgba(255,255,255,.05),transparent); animation:shimmer 1.3s infinite; }
  @keyframes shimmer{ 100%{transform:translateX(100%)} }

  /* ---- hidden (fully-finished) instances ---- */
  .hiddenrooms{ padding:2px 16px 10px; }
  .hiddenrooms[hidden]{ display:none; }
  .hr-title{ color:var(--ink-dimmer); font-size:var(--fs-sm); text-align:center; margin-bottom:9px; }
  .hr-chips{ display:flex; flex-wrap:wrap; gap:8px; justify-content:center; }
  .hr-chip{ font:inherit; font-size:var(--fs-sm); display:inline-flex; align-items:center; gap:7px; cursor:pointer;
        color:var(--ink-dim); background:var(--surface-2); border:1px solid var(--line); border-radius:var(--r-pill);
        padding:5px 13px; transition:background .15s,border-color .15s,color .15s; }
  .hr-chip:hover{ background:var(--surface-hi); color:var(--ink-2); border-color:var(--line-drawer); }
  .hr-chip:focus-visible{ outline:2px solid var(--accent); outline-offset:1px; }
  .hr-chip .ic{ color:var(--ink-dimmer); width:14px; height:14px; }
  .hr-chip span{ display:inline-flex; align-items:center; gap:3px; color:var(--done); font-family:var(--mono); }
  .hr-chip span.hr-ab{ color:var(--abort); }

  /* ---- tool / status legend ---- */
  .legend{ display:flex; flex-wrap:wrap; gap:7px 16px; justify-content:center; align-items:center;
        padding:8px 18px 30px; color:var(--ink-dimmer); font-size:var(--fs-sm); }
  .legend .lg{ display:inline-flex; align-items:center; gap:6px; }
  .legend .lg-ic{ display:inline-flex; align-items:center; }
  .legend .lg-cap{ color:var(--ink-dim); font-weight:600; }
  .legend .lg-sep{ color:var(--ink-dimmer); opacity:.35; margin:0 2px; }
  .legend .sw{ width:9px; height:9px; border-radius:var(--r-pill); background:currentColor; box-shadow:0 0 7px currentColor; flex:none; }
  .legend[hidden]{ display:none; }

  /* per-conversation "show finished" toggle in the room header */
  .rdone{ font:inherit; font-family:var(--mono); font-size:var(--fs-sm); background:none; border:1px solid transparent; color:var(--done);
          cursor:pointer; padding:2px 7px; border-radius:var(--r-pill); opacity:.7; display:inline-flex; align-items:center; gap:3px; transition:background .15s,opacity .15s; }
  .rdone:hover{ background:var(--surface-hi); opacity:1; }
  .rdone.on{ opacity:1; border-color:var(--done-soft); background:var(--done-soft); }
  .rdone:focus-visible{ outline:2px solid var(--done); outline-offset:1px; }
  .rabort{ color:var(--abort); }
  .rabort.on{ border-color:var(--abort-soft); background:var(--abort-soft); }
  .rabort:focus-visible{ outline-color:var(--abort); }

  /* ---------- conversation list inside a room ---------- */
  /* auto-fill grid: a narrow room shows one column; once a room is wide
     enough (the .wide two-track span, or simply a roomy viewport) the rows
     flow into 2+ columns automatically -- no JS, fully responsive. */
  .floor{ display:grid; grid-template-columns:repeat(auto-fill,minmax(min(300px,100%),1fr)); gap:6px; padding:10px; align-content:start; }

  /* a single conversation / subagent row */
  .ws{ position:relative; display:flex; align-items:center; gap:12px; padding:9px 12px;
       border:1px solid transparent; border-radius:var(--r-md); cursor:pointer;
       transition:background .15s,border-color .15s; }
  .ws:hover{ background:var(--surface-hi); border-color:var(--line); }
  .ws:focus-visible{ background:var(--surface-hi); }

  /* avatar disc with a status ring */
  .avatar{ position:relative; width:42px; height:42px; flex:none; border-radius:50%;
           display:flex; align-items:center; justify-content:center; font-size:22px; line-height:1;
           background:radial-gradient(circle at 50% 35%,var(--avatar-grad-1),var(--avatar-grad-2));
           border:1px solid var(--line); }
  /* Centre the emoji on BOTH axes. The span fills the disc and flex-centres its
     glyph; line-height:normal (not 1) is required -- a tight line box clips the
     emoji's asymmetric ascent/descent and biases it off-centre. */
  .avatar .head{ position:absolute; inset:0; display:flex; align-items:center; justify-content:center;
           line-height:normal; font-style:normal; }
  .avatar::before{ content:""; position:absolute; inset:-3px; border-radius:50%;
           border:2px solid var(--row-stat,var(--ink-dimmer)); opacity:.85; }
  .ws.running .avatar::before{ border-color:var(--row-fam,var(--ok)); }
  .ws.running .avatar::after{ content:""; position:absolute; inset:-3px; border-radius:50%;
           border:2px solid var(--row-fam,var(--ok)); animation:ring 1.8s ease-out infinite; }
  @keyframes ring{ 0%{transform:scale(1); opacity:.7} 100%{transform:scale(1.45); opacity:0} }
  .ws.stale .avatar{ filter:grayscale(.5) brightness(.85); }
  .ws.stale .avatar::before{ border-color:var(--idle); border-style:dashed; }
  .ws.aborted .avatar{ filter:grayscale(.5) brightness(.85); }
  .ws.aborted .avatar::before{ border-color:var(--abort); border-style:dashed; }
  .ws.done .avatar::before{ border-color:var(--done); }

  /* a tiny corner badge marks the conversation lead vs a subagent */
  .ws.is-session .avatar .lead{ position:absolute; bottom:-2px; inset-inline-end:-2px; width:15px; height:15px;
           border-radius:50%; background:var(--accent); color:#fff; display:flex; align-items:center; justify-content:center;
           border:2px solid var(--surface); }
  .ws.is-session .avatar .lead .ic{ width:9px; height:9px; }
  .ws:not(.is-session) .avatar .lead{ display:none; }

  .ws-main{ min-width:0; flex:1; display:flex; flex-direction:column; gap:2px; }
  .name{ font-size:var(--fs-md); color:var(--ink-2); font-weight:600; line-height:1.3;
         overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .ws.is-session .name{ color:var(--ink); }
  .act{ font-size:var(--fs-sm); color:var(--ink-dim); display:inline-flex; align-items:center; gap:6px;
        overflow:hidden; line-height:1.3; }
  .act .fam-dot{ width:7px; height:7px; border-radius:50%; flex:none; background:var(--row-fam,var(--ink-dimmer)); }
  .act .act-txt{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .ws.done .act{ color:var(--ok); } .ws.stale .act{ color:var(--idle); } .ws.aborted .act{ color:var(--abort); }
  .ws.done .act .fam-dot, .ws.stale .act .fam-dot, .ws.aborted .act .fam-dot{ display:none; }

  /* right-aligned timer + status pill */
  .ws-meta{ flex:none; display:flex; flex-direction:column; align-items:flex-end; gap:3px; }
  .timer{ font-size:var(--fs-sm); color:var(--ink-dim); direction:ltr; font-family:var(--mono); font-weight:500; }
  .ws.running .timer{ color:var(--ink-2); }
  .badge{ font-size:var(--fs-xs); font-weight:700; letter-spacing:.4px; text-transform:uppercase;
          padding:2px 7px; border-radius:var(--r-pill); line-height:1.4; }
  .ws.running .badge{ background:var(--ok-soft); color:var(--ok); }
  .ws.stale .badge{ background:var(--idle-soft); color:var(--idle); }
  .ws.aborted .badge{ background:var(--abort-soft); color:var(--abort); }
  .ws.done .badge{ background:var(--done-soft); color:var(--done); }

  /* entrance + finish beats (kept subtle) */
  .ws.entering{ animation:slidein .45s cubic-bezier(.2,.8,.3,1); }
  @keyframes slidein{ 0%{opacity:0; transform:translateY(-8px)} 100%{opacity:1; transform:translateY(0)} }
  .ws.justdone{ animation:flashdone .9s ease; }
  @keyframes flashdone{ 0%{background:var(--done-soft)} 100%{background:transparent} }
  .burst{ position:absolute; inset:0; pointer-events:none; overflow:visible; z-index:3; }
  .confetti{ position:absolute; top:50%; left:30px; font-size:14px; animation:fall .9s ease-out forwards; }
  @keyframes fall{ from{transform:translateY(-4px) scale(.6); opacity:1} to{transform:translateY(40px) rotate(200deg); opacity:0} }

  /* ---------- detail drawer ---------- */
  #backdrop{ position:fixed; inset:0; background:var(--backdrop); z-index:60; opacity:0; pointer-events:none; transition:opacity .2s;
        backdrop-filter:blur(2px); }
  #backdrop.show{ opacity:1; pointer-events:auto; }
  #drawer{ position:fixed; top:0; right:0; height:100%; width:min(460px,94vw); z-index:70; background:var(--surface-2);
           border-left:1px solid var(--line-drawer); box-shadow:var(--shadow-drawer); transform:translateX(105%);
           transition:transform .26s cubic-bezier(.3,.9,.3,1); display:flex; flex-direction:column; }
  #drawer.open{ transform:translateX(0); }
  html[dir="rtl"] #drawer{ right:auto; left:0; border-left:0; border-right:1px solid var(--line-drawer);
                           box-shadow:var(--shadow-drawer-rtl); transform:translateX(-105%); }
  html[dir="rtl"] #drawer.open{ transform:translateX(0); }
  .dhead{ display:flex; align-items:center; gap:13px; padding:18px 18px 14px; border-bottom:1px solid var(--line-head); }
  .dhead .av{ width:46px; height:46px; flex:none; border-radius:50%; font-size:25px; line-height:normal;
        display:flex; align-items:center; justify-content:center;
        background:radial-gradient(circle at 50% 35%,var(--avatar-grad-1),var(--avatar-grad-2)); border:1px solid var(--line); }
  .dhead .nm{ font-size:16px; font-weight:700; line-height:1.3; }
  .dhead .ro{ font-size:var(--fs-md); color:var(--ink-dim); margin-top:3px; }
  #dclose{ margin-inline-start:auto; align-self:flex-start; background:var(--chip-bg); border:1px solid var(--chip-line); color:var(--ink-2);
        border-radius:var(--r-sm); width:32px; height:32px; cursor:pointer; transition:background .15s; }
  #dclose:hover{ background:var(--surface-hi); }
  #dbody{ padding:16px 18px; overflow:auto; }
  #dbody .row{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:12px; }
  #dbody .chip{ font-size:var(--fs-sm); padding:4px 11px; border-radius:var(--r-pill); background:var(--chip-bg); color:var(--chip-ink);
        border:1px solid var(--chip-line); display:inline-flex; align-items:center; gap:5px; }
  #dbody .chip.mono{ font-family:var(--mono); }
  #dbody h3{ font-size:var(--fs-xs); text-transform:uppercase; letter-spacing:.7px; color:var(--ink-dimmer); margin:18px 0 7px; font-weight:700; }
  #dbody .box{ background:var(--surface-3); border:1px solid var(--line-soft); border-radius:var(--r-md); padding:12px 13px; font-size:var(--fs-md);
               line-height:1.65; color:var(--ink-2); white-space:pre-wrap; max-height:42vh; overflow:auto; }
  #dbody .box[dir]{ text-align:start; }

  /* ---- prefers-reduced-motion: keep state-by-color, drop loops ---- */
  @media (prefers-reduced-motion: reduce){
    .ws.running .avatar::after, .ws.entering, .ws.justdone, .confetti,
    .livestat .livedot, .boot .shimmer::after{ animation:none !important; }
    #drawer, #backdrop, .toast{ transition:none; }
  }

  /* ---------- narrow / docked-sidebar layout ----------
     Placed last (after every base rule above) on purpose: media queries do NOT
     add specificity, so these overrides must come later in source order to win.
     In a side-bar dock the panel can be very narrow. Keep the header compact so
     the agents stay visible: the title + counts + live dot stay, and the tools
     (search, theme, language, "show finished") fold behind the toggle button. */
  @media (max-width:640px){
    header{ padding:9px 12px; gap:4px 10px; }
    header h1{ font-size:var(--fs-lg); flex:1 1 auto; }   /* grow so the toggle sits at the end of the title row */
    h1 .ic{ width:20px; height:20px; }
    header .htoggle{ display:inline-flex; order:2; }
    header > .spacer{ order:3; flex-basis:100%; height:0; }   /* full-width break in the PAGE header only (not the room header): chips + live flag drop to the next row */
    .counts{ order:4; }                          /* counts and the live flag share that row, wrapping only if there's no room */
    #livestat{ order:5; }
    #demoChip{ order:6; }
    .tools{ display:none; order:9; flex-basis:100%; width:100%; gap:8px;
            margin-top:6px; padding-top:8px; border-top:1px solid var(--line-soft); }
    header.tools-open .tools{ display:flex; }
    #search{ flex:1 1 120px; width:auto; max-width:none; }
    .tools label{ margin-inline-start:auto; }
    #app{ padding:12px 10px 48px; gap:12px; }
    .hiddenrooms{ padding:2px 10px 10px; }
    .legend{ padding:8px 10px 22px; }
    .reconnect{ padding:7px 12px; }
    .floor{ padding:8px; }
    .rh{ padding:11px 12px; }
    .rh .rt{ flex:1 1 auto; }                  /* the room title takes the leftover space... */
    .rh .spacer{ display:none; }               /* ...rather than an expanding spacer, so it isn't truncated */
    .rc{ flex:none; }                          /* keep the status counts intact at the end */
  }
  /* Very narrow: drop the count-chip labels (keep icon + number); the live flag stays. */
  @media (max-width:360px){
    header{ padding:7px 10px; gap:2px 8px; }       /* tighter header rows + a smaller title/hamburger */
    header h1{ font-size:var(--fs-md); gap:7px; }
    h1 .ic{ width:18px; height:18px; }
    header .htoggle{ height:28px; min-width:28px; padding:0 8px; }
    header .htoggle .ic{ width:15px; height:15px; }
    .counts{ gap:5px; }
    .counts span{ padding:4px 8px; gap:4px; }
    .counts em{ display:none; }
    /* room header: drop the project icon, shrink the name, "(N)" instead of "N chats" */
    .rt .rt-ico{ display:none; }
    .rt{ font-size:var(--fs-md); gap:6px; }
    .rt small .rc-word{ display:none; }
    .rt small .rc-pre, .rt small .rc-post{ display:inline; }
    /* trim the box's horizontal padding everywhere inside the room */
    .rh{ padding:13px 6px; gap:6px; }
    .floor{ padding:0; }
    .ws{ padding:9px 8px; }
    #app{ padding:10px 6px 40px; gap:10px; }                  /* widen the content: trim the app's side gutters */
  }
</style>
</head>
<body>
<header>
  <h1 id="h1">Cursor Theater</h1>
  <div class="counts" id="counts"></div>
  <span id="livestat" class="livestat boot"><i class="livedot" aria-hidden="true"></i><span id="liveago"></span></span>
  <span id="demoChip" class="demo-chip" hidden>▶ <span id="demoChipLbl">Demo</span> <button id="exitDemoBtn" type="button">Exit</button></span>
  <div class="spacer"></div>
  <button id="hToggle" class="iconbtn htoggle" type="button" aria-expanded="false" aria-controls="htools" aria-label="Tools"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 6h16M4 12h16M4 18h16"/></svg></button>
  <div class="tools" id="htools">
    <input id="search" type="search" autocomplete="off" placeholder="Search agents…" aria-label="Search agents">
    <button id="muteBtn" class="iconbtn" type="button"></button>
    <button id="themeBtn" class="iconbtn" type="button" aria-label="Theme"></button>
    <button id="langBtn" class="iconbtn" type="button">עברית</button>
    <label><input type="checkbox" id="showDone" class="switch" role="switch"> <span id="showDoneLbl">Show finished</span></label>
  </div>
</header>
<div id="reconnect" class="reconnect" role="status" hidden></div>
<div id="app"><div class="boot" data-boot="1"></div></div>
<div id="hidden" class="hiddenrooms" hidden></div>
<div id="legend" class="legend" hidden></div>
<div id="diag" class="diag" hidden></div>

<div id="toasts" class="toasts" aria-hidden="true"></div>
<div id="live" class="sr-only" role="status" aria-live="polite"></div>

<div id="backdrop"></div>
<aside id="drawer" role="dialog" aria-modal="true" aria-labelledby="dnm dro">
  <div class="dhead"><div class="av" id="dav" aria-hidden="true"></div>
    <div><div class="nm" id="dnm"></div><div class="ro" id="dro"></div></div>
    <button id="dclose">✕</button></div>
  <div id="dbody"></div>
</aside>

<script>
const POLL_MS=1500;
// "" in a normal browser (same-origin). The VS Code extension injects an
// absolute http://127.0.0.1:<port> base so the embedded webview can reach the API.
const API_BASE=(typeof window!=="undefined"&&window.__CT_API_BASE__)||"";
const rooms={};   // session_full -> {section, floor, rt, rc}
const els={};     // id -> {root, refs, data, status}
const prevStatus={};
let audioCtx=null, openId=null, openData=null;
let showDone=(function(){ try{ if(/[?&]show=done\\b/.test(location.search)) return true;
  if(/[?&]demo=1(?:&|$)/.test(location.search)) return true;   // demo must show the finish beat
  const v=localStorage.getItem("ct_showDone"); return v===null ? true : v==="1"; }catch(e){ return true; } })();   // default ON; respects an explicit toggle
// per-conversation "show finished" overrides; falls back to the global showDone default
let roomDone=(function(){ try{ return JSON.parse(localStorage.getItem("ct_roomDone")||"{}")||{}; }catch(e){ return {}; } })();
function roomShowsDone(s){ return (s in roomDone) ? !!roomDone[s] : showDone; }
function saveRoomDone(){ try{ localStorage.setItem("ct_roomDone",JSON.stringify(roomDone)); }catch(e){} }
function toggleRoomDone(s){ roomDone[s]=!roomShowsDone(s); saveRoomDone(); render(); }
function revealRoom(s){ roomDone[s]=true; roomAborted[s]=true; saveRoomDone(); saveRoomAborted(); render(); }   // un-hide a fully-settled (finished/stopped) instance
// parallel per-conversation "show stopped" overrides, same shape as roomDone
let roomAborted=(function(){ try{ return JSON.parse(localStorage.getItem("ct_roomAborted")||"{}")||{}; }catch(e){ return {}; } })();
function roomShowsAborted(s){ return (s in roomAborted) ? !!roomAborted[s] : showDone; }
function saveRoomAborted(){ try{ localStorage.setItem("ct_roomAborted",JSON.stringify(roomAborted)); }catch(e){} }
function toggleRoomAborted(s){ roomAborted[s]=!roomShowsAborted(s); saveRoomAborted(); render(); }
// The global "Show done/stopped" is the master switch: toggling it wipes every
// per-room override (both dimensions), so a room can never stay stuck hidden.
function setShowDone(v){ showDone=v; try{ localStorage.setItem("ct_showDone",v?"1":"0"); }catch(e){}
  roomDone={}; try{ localStorage.removeItem("ct_roomDone"); }catch(e){}
  roomAborted={}; try{ localStorage.removeItem("ct_roomAborted"); }catch(e){}
  const cb=document.getElementById("showDone"); if(cb) cb.checked=v; render(); }
let demoMode=(function(){ try{ return /[?&]demo=1(?:&|$)/.test(location.search); }catch(e){ return false; } })();
let searchQuery="", lastPayload=null, searchT=null, lastDing=0, lastOrderKey="";
let muted=(function(){ try{ return localStorage.getItem("ct_muted")==="1"; }catch(e){ return false; } })();
// Theme: read whatever the early head script resolved onto <html data-theme>.
let theme=(function(){ try{ const t=document.documentElement.getAttribute("data-theme"); return (t==="light")?"light":"dark"; }catch(e){ return "dark"; } })();
let themeExplicit=(function(){ try{ const t=localStorage.getItem("ct_theme"); return t==="light"||t==="dark"; }catch(e){ return false; } })();
function applyThemeBtn(){ const b=document.getElementById("themeBtn"); if(!b) return;
  const nextDark=(theme==="light");           // button shows the icon for the mode it switches TO
  b.innerHTML=nextDark?ICON.moon:ICON.sun;
  const lbl=t(nextDark?"toDark":"toLight"); b.title=lbl; b.setAttribute("aria-label",lbl); }
function setTheme(v){ theme=(v==="light")?"light":"dark"; themeExplicit=true;
  document.documentElement.setAttribute("data-theme",theme);
  try{ localStorage.setItem("ct_theme",theme); }catch(e){} applyThemeBtn(); }

// ---- i18n: the browser owns every display string in both languages. ----
// To add a language, add an entry here (and personas/tools tables) -- nothing
// in Python needs to change. Persona names are index-aligned with PERSONA_EMOJI.
const PERSONAS_EN=["The Detective","The Writer","The Courier","The Researcher","The Librarian","The Navigator","The Scout","The Builder","The Wizard","The Marksman","The Owl","The Fox","The Bee","The Robot","The Tiger","The Eagle"];
const PERSONAS_HE=["הבלש","הסופר","השליח","החוקר","הספרן","הנווט","הצופה","הבנאי","הקוסם","הצייד","הינשוף","השועל","הדבורה","הרובוט","הנמר","הנשר"];
const TOOLS_EN={WebSearch:"🔍 Searching",WebFetch:"🌐 Reading page",Read:"📖 Reading",Edit:"✏️ Editing",MultiEdit:"✏️ Editing",Write:"✏️ Writing",NotebookEdit:"✏️ Notebook",Bash:"⚙️ Command",PowerShell:"⚙️ Command",BashOutput:"⚙️ Output",KillShell:"⚙️ Command",SlashCommand:"⌨️ Slash command",Grep:"🔎 Searching code",Glob:"🔎 Files",Task:"👥 Subagent",Agent:"👥 Subagent",TodoWrite:"📝 Todos",Skill:"🧩 Skill",ExitPlanMode:"📋 Plan",StructuredOutput:"🧾 Summarizing"};
const TOOLS_HE={WebSearch:"🔍 מחפש",WebFetch:"🌐 קורא דף",Read:"📖 קורא",Edit:"✏️ עורך",MultiEdit:"✏️ עורך",Write:"✏️ כותב",NotebookEdit:"✏️ מחברת",Bash:"⚙️ פקודה",PowerShell:"⚙️ פקודה",BashOutput:"⚙️ פלט",KillShell:"⚙️ פקודה",SlashCommand:"⌨️ פקודת סלאש",Grep:"🔎 מחפש קוד",Glob:"🔎 קבצים",Task:"👥 סוכן",Agent:"👥 סוכן",TodoWrite:"📝 משימות",Skill:"🧩 מיומנות",ExitPlanMode:"📋 תכנון",StructuredOutput:"🧾 מסכם"};
const I18N={
  en:{ appTitle:"Cursor Theater", docTitle:"Cursor Theater", showDone:"Show done/stopped", toggleFinished:"Show/hide finished in this conversation", toggleStopped:"Show/hide stopped in this conversation", switchTo:"עברית",
       emptyOffice:"The office is empty",
       emptySub:"Start an agent conversation in Cursor — or see what a busy office looks like:",
       emptySubEmbedded:"Start an agent conversation in Cursor — it'll appear here as it works.",
       watchDemo:"▶ Watch a live demo", demoLabel:"Demo", exitDemo:"Exit",
       langHint:"Switch language (Hebrew / English)", close:"Close",
       toLight:"Switch to light mode", toDark:"Switch to dark mode",
       reconnecting:"⚠ Lost connection to the server — retrying…",
       searchPlaceholder:"Search agents…", emptyNoMatch:"No agents match your search.",
       mute:"Mute chime", unmute:"Unmute chime", finishedToast:"finished",
       skippedN:function(n){ return n+" malformed line"+(n===1?"":"s")+" skipped"; },
       emptyNoActive:'No active agents. Tick "Show finished" to see history.',
       emptyNoneInWindow:"No agents in the time window.",
       connecting:"Connecting to your sessions…",
       liveAgo:function(s,short){ var u=short?"":"updated "; return "live · "+u+(s<2?"just now":(s<60?(s+"s ago"):(Math.floor(s/60)+"m ago"))); }, liveDisc:"disconnected",
       legStatus:"Worker:", legTool:"Tool in use:",
       tSec:"s", tMin:" min", tHour:" h",
       legSearch:"search", legRead:"read", legEdit:"edit", legCmd:"command", legAgent:"subagent",
       hiddenTitle:function(n){ return (n===1?"1 finished instance hidden":n+" finished instances hidden")+" — click to show"; },
       working:"working", idleN:"idle", abortedN:"stopped", finished:"finished",
       dWorking:"Working", dDone:"Done", dStale:"Idle", dAborted:"Stopped",
       dDuration:"Duration ", dElapsed:"Elapsed ",
       dAction:"Activity", dTask:"Task", dResult:"Result",
       taskUnavailable:"working — details unavailable",
       actDone:"✅ Done", actStale:"💤 Idle", actAborted:"⏹ Stopped", actThinking:"🤔 Thinking", actMcp:"🔌 MCP tool",
       legAborted:"stopped",
       personas:PERSONAS_EN, tools:TOOLS_EN },
  he:{ appTitle:"משרד הסוכנים", docTitle:"משרד הסוכנים", showDone:"הצג שהושלמו/נעצרו", toggleFinished:"הצג/הסתר שהושלמו בשיחה זו", toggleStopped:"הצג/הסתר שנעצרו בשיחה זו", switchTo:"English",
       emptyOffice:"המשרד ריק",
       emptySub:"הפעילו שיחת סוכן ב-Cursor - או הציצו איך נראה משרד עמוס:",
       emptySubEmbedded:"הפעילו שיחת סוכן ב-Cursor - היא תופיע כאן בזמן העבודה.",
       watchDemo:"▶ צפו בדמו חי", demoLabel:"דמו", exitDemo:"יציאה",
       langHint:"החלפת שפה (עברית / אנגלית)", close:"סגירה",
       toLight:"מעבר למצב בהיר", toDark:"מעבר למצב כהה",
       reconnecting:"⚠ אבד החיבור לשרת - מנסה שוב…",
       searchPlaceholder:"חיפוש סוכנים…", emptyNoMatch:"אין סוכנים שתואמים לחיפוש.",
       mute:"השתק צליל", unmute:"בטל השתקה", finishedToast:"סיים",
       skippedN:function(n){ return n+" שורות פגומות דולגו"; },
       emptyNoActive:'אין סוכנים פעילים. סמנו "הצג שהושלמו" כדי לראות היסטוריה.',
       emptyNoneInWindow:"אין סוכנים בחלון הזמן.",
       connecting:"מתחבר לשיחות שלך…",
       liveAgo:function(s,short){ var u=short?"":"עודכן "; return "חי · "+u+(s<2?"הרגע":(s<60?("לפני "+s+" שנ׳"):("לפני "+Math.floor(s/60)+" דק׳"))); }, liveDisc:"מנותק",
       legStatus:"עובד:", legTool:"הכלי בשימוש:",
       tSec:" שנ׳", tMin:" דק׳", tHour:" שע׳",
       legSearch:"חיפוש", legRead:"קריאה", legEdit:"עריכה", legCmd:"פקודה", legAgent:"סוכן",
       hiddenTitle:function(n){ return (n===1?"מופע שהושלם מוסתר":n+" מופעים שהושלמו מוסתרים")+" — לחצו כדי להציג"; },
       working:"עובדים", idleN:"ממתינים", abortedN:"נעצרו", finished:"סיימו",
       dWorking:"עובד", dDone:"סיים", dStale:"ממתין", dAborted:"נעצר",
       dDuration:"משך ", dElapsed:"זמן ",
       dAction:"פעולה", dTask:"משימה", dResult:"תוצאה",
       taskUnavailable:"עובד — פרטים לא זמינים",
       actDone:"✅ סיים", actStale:"💤 ממתין", actAborted:"⏹ נעצר", actThinking:"🤔 חושב", actMcp:"🔌 כלי MCP",
       legAborted:"נעצר",
       personas:PERSONAS_HE, tools:TOOLS_HE }
};
let lang=(function(){ try{ var Q=(location.search.match(/[?&]lang=(he|en)\\b/)||[])[1]; if(Q) return Q;
  var L=localStorage.getItem("ct_lang"); return (L==="he"||L==="en")?L:"en"; }catch(e){ return "en"; } })();
function t(k){ const v=I18N[lang][k]; return (v!==undefined&&v!==null)?v:((I18N.en[k]!==undefined)?I18N.en[k]:k); }
function personaName(a){ const p=I18N[lang].personas; return (a&&typeof a.persona_id==="number"&&p[a.persona_id])||(lang==="he"?"סוכן":"Agent"); }
function mcpServer(tool){ const p=(tool||"").split("__"); return p.length>=3?p[1]:""; }  // mcp__<server>__<tool>
function activityLabel(a){ const L=I18N[lang];
  if(a.status==="done") return L.actDone;
  if(a.status==="aborted") return L.actAborted;
  if(a.status==="stale") return L.actStale;
  if(a.tool&&a.tool.indexOf("mcp__")===0){ const s=mcpServer(a.tool); return s?("🔌 "+s):L.actMcp; }
  return L.tools[a.tool]||L.actThinking; }
function applyLang(){ const el=document.documentElement; el.lang=lang; el.dir=(lang==="he")?"rtl":"ltr";
  document.getElementById("h1").innerHTML=ICON.building+esc(t("appTitle"));
  document.getElementById("showDoneLbl").textContent=t("showDone");
  document.getElementById("langBtn").textContent=t("switchTo");
  document.getElementById("demoChipLbl").textContent=t("demoLabel");
  document.getElementById("exitDemoBtn").textContent=t("exitDemo");
  document.getElementById("demoChip").hidden=!demoMode;
  document.getElementById("showDone").checked=showDone;
  document.getElementById("langBtn").title=t("langHint");
  document.getElementById("search").placeholder=t("searchPlaceholder");
  document.getElementById("search").setAttribute("aria-label",t("searchPlaceholder"));
  const mb=document.getElementById("muteBtn"); mb.innerHTML=muted?ICON.bellOff:ICON.bell;
  mb.title=t(muted?"unmute":"mute"); mb.setAttribute("aria-label",t(muted?"unmute":"mute"));
  applyThemeBtn();
  document.getElementById("dclose").title=t("close");
  document.getElementById("dclose").setAttribute("aria-label",t("close"));
  const rc=document.getElementById("reconnect"); if(!rc.hidden) rc.textContent=t("reconnecting");
  document.title=t("docTitle");
  const boot=document.querySelector('#app .boot[data-boot]'); if(boot) boot.innerHTML=skeletonHTML();
  renderLegend();
  if(lastLiveMs) tickLive();
  // The drawer is the one surface render() may not refresh (an open 'done' agent
  // can be filtered out of the floor), so re-translate it directly from cached data.
  if(openId&&openData) fillDrawer(openData); }
function setLang(l){ lang=(l==="he")?"he":"en"; try{ localStorage.setItem("ct_lang",lang); }catch(e){}
  try{ const u=new URL(location.href); if(u.searchParams.has("lang")){ u.searchParams.set("lang",lang); history.replaceState(null,"",u.pathname+u.search); } }catch(e){}
  applyLang(); setTimeout(retruncateRooms,0); poll(); }

// Inline SVG for the structural chrome (room/chat/status); persona avatars stay
// emoji on purpose. fill=currentColor so the existing color classes still drive hue.
const ICON={
  building:'<svg class="ic" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M3 21V5l8-2v18zM13 21V9l8 3v9zM6 8h2v2H6zm0 4h2v2H6zm0 4h2v2H6zm10-3h2v2h-2zm0 4h2v2h-2z"/></svg>',
  chat:'<svg class="ic" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M4 4h16a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H9l-5 4v-4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1z"/></svg>',
  run:'<svg class="ic" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="12" cy="12" r="6"/></svg>',
  idle:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="8"/><path d="M12 8v4.5l3 1.8"/></svg>',
  done:'<svg class="ic" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm-1.1 14.3-3.6-3.6 1.4-1.4 2.2 2.2 4.9-4.9 1.4 1.4z"/></svg>',
  stop:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><rect x="9" y="9" width="6" height="6" rx="1" fill="currentColor" stroke="none"/></svg>',
  lead:'<svg class="ic" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M4 4h16a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H9l-5 4v-4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1z"/></svg>',
  sun:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2.2M12 19.3v2.2M21.5 12h-2.2M4.7 12H2.5M18.4 5.6l-1.6 1.6M7.2 16.8l-1.6 1.6M18.4 18.4l-1.6-1.6M7.2 7.2 5.6 5.6"/></svg>',
  moon:'<svg class="ic" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M21 12.8A8.5 8.5 0 1 1 11.2 3a6.8 6.8 0 0 0 9.8 9.8z"/></svg>',
  bell:'<svg class="ic" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 2a6 6 0 0 0-6 6v3.6L4.3 15a1 1 0 0 0 .9 1.5h13.6a1 1 0 0 0 .9-1.5L18 11.6V8a6 6 0 0 0-6-6zm0 20a2.6 2.6 0 0 0 2.5-2h-5A2.6 2.6 0 0 0 12 22z"/></svg>',
  bellOff:'<svg class="ic" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 2a6 6 0 0 0-6 6v3.6L4.3 15a1 1 0 0 0 .9 1.5h13.6a1 1 0 0 0 .9-1.5L18 11.6V8a6 6 0 0 0-6-6zm0 20a2.6 2.6 0 0 0 2.5-2h-5A2.6 2.6 0 0 0 12 22z"/><path d="M3.2 2.5 21.5 20.8" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>'
};
let booted=false;            // becomes true after the first successful scan
function skeletonHTML(){ const rows=n=>Array(n).fill('<div class="sk-row shimmer"></div>').join("");
  const room=n=>'<div class="sk-room"><div class="sk-head shimmer"></div><div class="sk-floor">'+rows(n)+'</div></div>';
  return '<div class="sk-title">'+ICON.run+esc(t("connecting"))+'</div>'
    +'<div class="sk-grid">'+room(3)+room(2)+'</div>'; }
function esc(s){ return (s==null?"":String(s)).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function fmt(ms){ if(ms==null||ms<0) return "--:--"; const s=Math.floor(ms/1000),m=Math.floor(s/60),x=s%60;
  return String(m).padStart(2,"0")+":"+String(x).padStart(2,"0"); }
// "time since" for settled (done/idle) desks: live seconds for the first minute,
// then coarsen to ~Nmin / ~Nh so a finished card stops twitching every second.
function fmtAgo(ms){ if(ms==null||ms<0) return "--:--"; const s=Math.floor(ms/1000);
  if(s<60) return s+t("tSec");
  const m=Math.floor(s/60); if(m<60) return "~"+m+t("tMin");
  return "~"+Math.floor(m/60)+t("tHour"); }
function baseName(p){ return (p||"").replace(/[\\\\/]+$/,"").split(/[\\\\/]/).pop()||"—"; }
function roomLabel(a){ return baseName(a.project||a.cwd); }   // the conversation's project, not a nested subagent cwd
function roomKey(a){ return a.project||a.cwd||a.session_full; }  // a room = one Cursor instance (project); desks inside = its conversations
function deskName(a){ return a.title||a.role||personaName(a); }  // the conversation's real Cursor title labels its desk
const COLORS=["#5b6ee0","#e07a5b","#3fae74","#c45bd0","#e0b84a","#4ab3c4","#d05b7a","#7a86b8"];
function colorFor(id){ let h=0; for(let i=0;i<id.length;i++) h=(h*31+id.charCodeAt(i))>>>0; return COLORS[h%COLORS.length]; }

function ding(){ if(muted) return; const nw=Date.now(); if(nw-lastDing<400) return; lastDing=nw;  // mute + storm guard
  try{ audioCtx=audioCtx||new (window.AudioContext||window.webkitAudioContext)();
  const t=audioCtx.currentTime;
  [880,1320].forEach((f,i)=>{ const o=audioCtx.createOscillator(),g=audioCtx.createGain();
    o.type="sine"; o.frequency.value=f; o.connect(g); g.connect(audioCtx.destination);
    const s=t+i*0.12; g.gain.setValueAtTime(0.0001,s); g.gain.exponentialRampToValueAtTime(0.25,s+0.02);
    g.gain.exponentialRampToValueAtTime(0.0001,s+0.35); o.start(s); o.stop(s+0.4); }); }catch(e){} }

function confetti(root){ const b=document.createElement("div"); b.className="burst";
  const em=["🎉","✨","🎊","⭐","✅"];
  for(let i=0;i<10;i++){ const c=document.createElement("div"); c.className="confetti"; c.textContent=em[i%em.length];
    c.style.left=(8+Math.random()*78)+"%"; c.style.animationDelay=(Math.random()*0.15)+"s"; b.appendChild(c); }
  root.appendChild(b); setTimeout(()=>b.remove(),1050); }

let announceT=null;
function announce(msg){ const el=document.getElementById("live"); if(!el) return;  // SR-only live region
  el.textContent = el.textContent ? (el.textContent+" · "+msg) : msg;        // append so same-tick finishes aren't lost
  clearTimeout(announceT); announceT=setTimeout(()=>{ el.textContent=""; },4000); }
function toast(msg){ const c=document.getElementById("toasts"); if(!c) return;
  const d=document.createElement("div"); d.className="toast"; d.dir="auto"; d.textContent=msg; c.appendChild(d);
  requestAnimationFrame(()=>d.classList.add("show"));
  setTimeout(()=>{ d.classList.remove("show"); setTimeout(()=>d.remove(),300); },3200); }
function setMuted(m){ muted=m; try{ localStorage.setItem("ct_muted",m?"1":"0"); }catch(e){}
  const b=document.getElementById("muteBtn"); if(!b) return;
  b.innerHTML=m?ICON.bellOff:ICON.bell; b.title=t(m?"unmute":"mute"); b.setAttribute("aria-label",t(m?"unmute":"mute")); }

function createWS(a){ const root=document.createElement("div"); root.className="ws "+a.status+(a.is_session?" is-session":"");
  root.tabIndex=0; root.setAttribute("role","button");           // keyboard-reachable card
  root.innerHTML=
    '<div class="avatar" aria-hidden="true"><span class="head"></span>'+
      '<span class="lead">'+ICON.lead+'</span></div>'+
    '<div class="ws-main"><div class="name"></div>'+
      '<div class="act"><span class="fam-dot"></span><span class="act-txt"></span></div></div>'+
    '<div class="ws-meta"><span class="timer"></span><span class="badge"></span></div>';
  const refs={ head:root.querySelector(".head"), name:root.querySelector(".name"),
               act:root.querySelector(".act-txt"), timer:root.querySelector(".timer"),
               badge:root.querySelector(".badge") };
  root.addEventListener("click",()=>openDrawer(a.id));
  root.addEventListener("keydown",e=>{ if(e.key==="Enter"||e.key===" "){ e.preventDefault(); openDrawer(a.id); } });
  root.classList.add("entering"); setTimeout(()=>root.classList.remove("entering"),750);
  // status MUST start as a.status (not null): the very next updateWS() runs in the
  // same synchronous render task, and on a status mismatch it rewrites className --
  // which would strip "entering" before the browser ever paints, killing the walk-in.
  // (className already carries status + is-session above, so nothing is lost.)
  return { root, refs, data:a, status:a.status }; }

// Tool -> color family for the monitor glow (and a coarse grouping). Kept in
// sync with the .ws.running[data-fam=...] rules and the TOOLS_* label tables.
function toolFamily(tool){ if(!tool) return "";
  if(tool.indexOf("mcp__")===0) return "agent";
  if(/^(WebSearch|Grep|Glob|rg|SemanticSearch)$/i.test(tool)) return "search";
  if(/^(Read|ReadFile|ReadLints|WebFetch|FetchMcpResource|ListMcpResources)$/i.test(tool)) return "read";
  if(/^(Edit|Write|StrReplace|MultiEdit|ApplyPatch|NotebookEdit|EditNotebook|Delete)$/i.test(tool)) return "write";
  if(/^(Bash|Shell|PowerShell|BashOutput|KillShell|Await)$/i.test(tool)) return "cmd";
  if(/^(Task|Agent|CallMcpTool)$/i.test(tool)) return "agent";
  return ""; }

const FAM_COLOR={search:"var(--fam-search)",read:"var(--fam-read)",write:"var(--fam-write)",cmd:"var(--fam-cmd)",agent:"var(--fam-agent)"};
function badgeText(a){ const L=I18N[lang];
  return a.status==="done"?L.dDone:a.status==="aborted"?L.dAborted:a.status==="stale"?L.dStale:L.dWorking; }
function updateWS(e,a){ e.data=a;
  if(e.status!==a.status) e.root.className="ws "+a.status+(a.is_session?" is-session":"");
  const fam=toolFamily(a.tool); e.root.dataset.fam=fam;
  e.root.style.setProperty("--row-fam", FAM_COLOR[fam]||"var(--ok)");
  e.refs.head.textContent=a.emoji;
  const nm=deskName(a); e.refs.name.textContent=nm; e.refs.name.title=nm; e.refs.name.dir="auto";
  const actLbl=activityLabel(a); e.refs.act.textContent=actLbl; e.refs.act.parentElement.title=actLbl;
  e.refs.badge.textContent=badgeText(a);
  e.root.setAttribute("aria-label", nm+" — "+actLbl);
  e.refs.timer.dataset.start=a.start_ms||0; e.refs.timer.dataset.end=a.end_ms||0;
  e.refs.timer.dataset.mtime=a.mtime_ms||0; e.refs.timer.dataset.status=a.status;
  e.refs.timer.dataset.session=a.is_session?"1":"";
  if(prevStatus[a.id] && prevStatus[a.id]!=="done" && a.status==="done"){
    e.root.classList.add("justdone"); confetti(e.root); ding();
    const fmsg=nm+" — "+t("finishedToast"); toast(fmsg); announce(fmsg);  // visual + SR cue, survives a muted tab
    setTimeout(()=>e.root.classList.remove("justdone"),900); }
  prevStatus[a.id]=a.status; e.status=a.status;
  if(openId===a.id){ openData=a; fillDrawer(a); } }

function ensureRoom(sess){ let r=rooms[sess]; if(r) return r;
  const section=document.createElement("section"); section.className="room";
  section.style.setProperty("--room-accent", colorFor(sess));  // a stable hue per conversation
  section.setAttribute("role","group");
  section.innerHTML='<div class="rh"><h2 class="rt"></h2><span class="spacer"></span><span class="rc"></span></div><div class="floor"></div>';
  section.querySelector(".rt").style.setProperty("--room-accent", colorFor(sess));
  r={ section, floor:section.querySelector(".floor"), rt:section.querySelector(".rt"), rc:section.querySelector(".rc") };
  rooms[sess]=r; return r; }

// Middle truncation for room titles: when the name doesn't fit, keep the start
// AND the end (e.g. "Integrat…heater") instead of CSS end-ellipsis -- which in an
// RTL page would otherwise drop the start of a Latin project name. The full text
// lives in data-full so we can re-measure on resize / language flips.
function midTruncate(el){ if(!el) return;
  const full=el.dataset.full!=null?el.dataset.full:(el.dataset.full=el.textContent);
  el.textContent=full;
  if(el.scrollWidth<=el.clientWidth+1) return;            // fits whole -> plain text
  const fits=s=>{ el.textContent=s; return el.scrollWidth<=el.clientWidth+1; };
  let lo=1, hi=full.length-1, best="…";
  while(lo<=hi){ const keep=(lo+hi)>>1, head=Math.ceil(keep/2), tail=keep-head;
    const cand=full.slice(0,head)+"…"+(tail?full.slice(full.length-tail):"");
    if(fits(cand)){ best=cand; lo=keep+1; } else hi=keep-1; }
  el.textContent=best; }
function retruncateRooms(){ document.querySelectorAll(".rt .rt-name").forEach(midTruncate); }

function emptyHTML(kind){
  // kind: "office" (first run / nothing at all) | "noactive" | "nonewindow" | "nomatch"
  const msg = kind==="noactive" ? t("emptyNoActive") : kind==="nonewindow" ? t("emptyNoneInWindow")
            : kind==="nomatch" ? t("emptyNoMatch") : t("emptyOffice");
  let h='<div class="e-scene" aria-hidden="true">'+ICON.building+'</div><div class="e-title">'+esc(msg)+'</div>';
  // The demo's synthetic office comes from the Python server (?demo=1); a push-driven
  // webview has no demo data source, so embedded we drop the demo CTA and adjust copy.
  if(kind==="office") h+='<div class="e-sub">'+esc(EMBEDDED?t("emptySubEmbedded"):t("emptySub"))+'</div>'
                        +(EMBEDDED?'':'<button class="btn-demo" type="button">'+esc(t("watchDemo"))+'</button>');
  return h;
}
function matchesSearch(a,q){ return (
  (a.title||"").toLowerCase().indexOf(q)>=0 ||
  (a.role||"").toLowerCase().indexOf(q)>=0 ||
  (a.task||"").toLowerCase().indexOf(q)>=0 ||
  (a.tool||"").toLowerCase().indexOf(q)>=0 ||
  personaName(a).toLowerCase().indexOf(q)>=0 ||
  roomLabel(a).toLowerCase().indexOf(q)>=0 ); }
function setDemo(on){ demoMode=on; document.getElementById("demoChip").hidden=!on;
  // The demo's showpiece is the running->done finish beat (confetti+chime), which
  // only fires on a visible card -- so demo mode must show finished agents. On
  // exit, restore the user's saved preference. (Not persisted: a demo shouldn't
  // overwrite the real toggle. prevStatus is empty for fresh cards, so no phantom confetti.)
  const cb=document.getElementById("showDone");
  showDone = on ? true : (function(){ try{ return localStorage.getItem("ct_showDone")==="1"; }catch(e){ return false; } })();
  cb.checked=showDone;
  try{ const u=new URL(location.href); if(on) u.searchParams.set("demo","1"); else u.searchParams.delete("demo");
    history.replaceState(null,"",u.pathname+u.search); }catch(e){}
  poll(); }

function render(payload){
  if(payload) lastPayload=payload;       // cache so search/filter can re-render without a fetch
  const all=(lastPayload&&lastPayload.agents)||[];
  const app=document.getElementById("app");
  app.querySelectorAll(".empty,.boot").forEach(el=>el.remove());

  // per-room (per-project) stats from ALL agents (so a room can show ✓done even when hidden)
  const stat={};
  for(const a of all){ const s=roomKey(a); const v=stat[s]||(stat[s]={running:0,stale:0,aborted:0,done:0,label:roomLabel(a),topic:"",sid:"",mtime:0,convs:0});
    if(v[a.status]!==undefined) v[a.status]++; v.mtime=Math.max(v.mtime,a.mtime_ms||0); v.convs++;
    if(!v.label) v.label=roomLabel(a); }

  const q=(searchQuery||"").toLowerCase().trim();
  const searched = q ? all.filter(a=>matchesSearch(a,q)) : all;
  // "Show done/stopped" is per-room (per-project): each instance controls its own
  // done and stopped visibility independently via the two room buttons.
  const visible = searched.filter(a=>
    (a.status!=="done" || roomShowsDone(roomKey(a))) &&
    (a.status!=="aborted" || roomShowsAborted(roomKey(a))));
  const sess=[...new Set(visible.map(a=>roomKey(a)))];
  sess.sort((x,y)=>((stat[y].running>0)-(stat[x].running>0))||(stat[y].mtime-stat[x].mtime));

  // drop workers no longer visible
  const need=new Set(visible.map(a=>a.id));
  for(const id in els){ if(!need.has(id)){ els[id].root.remove(); delete els[id]; } }
  // Reconcile prevStatus for EVERY agent (not just visible ones): record the
  // status of hidden/filtered agents so a later toggle can't replay a stale
  // running->done as a fresh finish (confetti/ding/toast), and prune ids that
  // left the payload so the map can't grow unbounded.
  { const live=new Set(all.map(a=>a.id));
    for(const a of all){ if(!need.has(a.id)) prevStatus[a.id]=a.status; }
    for(const id in prevStatus){ if(!live.has(id)) delete prevStatus[id]; } }

  // only re-append sections when the room order actually changed (avoids layout
  // churn + animation interrupts every 1.5 s); only rewrite header strings on change.
  const orderKey=sess.join("|"); const reorder=(orderKey!==lastOrderKey); lastOrderKey=orderKey;
  for(const s of sess){ const r=ensureRoom(s); if(reorder) app.appendChild(r.section);
    const st=stat[s];
    r.section.classList.toggle("active", st.running>0);   // a room with running work draws the eye
    r.section.classList.toggle("wide", st.convs>=4);       // busy instances span two grid tracks; their floor then flows into multiple columns
    // room title = the Cursor instance (project); small = how many conversations
    const rtHTML='<span class="rt-ico">'+ICON.building+'</span><span class="rt-name">'+esc(st.label)+'</span>'
      +'<small><span class="rc-pre">(</span>'+st.convs+'<span class="rc-word">'+(st.convs===1?' chat':' chats')+'</span><span class="rc-post">)</span></small>';
    if(r._rt!==rtHTML){ r.rt.innerHTML=rtHTML; r._rt=rtHTML; r._lbl=null; }
    { const nm=r.rt.querySelector(".rt-name"); if(nm&&r._lbl!==st.label){ nm.dataset.full=st.label; midTruncate(nm); r._lbl=st.label; } }
    const showing=roomShowsDone(s);
    const showingAb=roomShowsAborted(s);
    const abortBtn=st.aborted?('<button class="rdone rabort'+(showingAb?' on':'')+'" data-s="'+esc(s)+'" type="button" aria-pressed="'+(showingAb?'true':'false')+'" title="'+esc(t("toggleStopped"))+'">'+ICON.stop+st.aborted+'</button>'):'';
    const doneBtn=st.done?('<button class="rdone'+(showing?' on':'')+'" data-s="'+esc(s)+'" type="button" aria-pressed="'+(showing?'true':'false')+'" title="'+esc(t("toggleFinished"))+'">'+ICON.done+st.done+'</button>'):'';
    const rcHTML='<span class="stat">'+ICON.run+' <b>'+st.running+'</b></span>'+(st.stale?'<span class="stat"><i>'+ICON.idle+st.stale+'</i></span>':'')+abortBtn+doneBtn;
    if(r._rc!==rcHTML){ r.rc.innerHTML=rcHTML; r._rc=rcHTML; }
    // Desks within a room: running work stays pinned on top, then EVERYONE else
    // (idle + done together) by most-recent activity -- so a chat that finished a
    // few minutes ago ranks above one that's been idle far longer ("last used"
    // order). The conversation lead only breaks exact ties. Sort EVERY render
    // (and re-append only when the order changed) so a desk flipping to running
    // rises to the top instead of being frozen in its creation slot.
    const inRoom=visible.filter(a=>roomKey(a)===s);
    inRoom.sort((x,y)=>((y.status==="running"?1:0)-(x.status==="running"?1:0))
      ||((y.mtime_ms||0)-(x.mtime_ms||0))
      ||((y.is_session?1:0)-(x.is_session?1:0)));
    // include status in the key so a desk flipping running<->idle<->done
    // forces a re-append (id-only key stays stable across status changes).
    const deskKey=inRoom.map(a=>a.id+":"+a.status).join("|");
    const needReorder=(reorder||r._deskKey!==deskKey); r._deskKey=deskKey;
    for(const a of inRoom){
      let e=els[a.id]; if(!e){ e=createWS(a); els[a.id]=e; r.floor.appendChild(e.root); }
      else if(needReorder||e.root.parentElement!==r.floor){ r.floor.appendChild(e.root); }
      updateWS(e,a); } }

  for(const s in rooms){ if(!sess.includes(s)){ rooms[s].section.remove(); delete rooms[s]; } }
  if(!visible.length){ const d=document.createElement("div");
    if(!booted){ d.className="boot"; d.innerHTML=skeletonHTML(); }      // still waiting on the first scan
    else { d.className="empty";
      const kind = q ? "nomatch" : (all.length===0 ? "office" : (showDone?"nonewindow":"noactive"));
      d.innerHTML=emptyHTML(kind); }
    app.appendChild(d); }
  document.getElementById("legend").hidden = !all.length;

  // Fully-finished instances hidden by their per-room toggle don't vanish: list
  // them at the bottom so they're visible and recoverable with one click.
  const hr=document.getElementById("hidden");
  const hiddenRooms = q ? [] : Object.keys(stat).filter(s=>!sess.includes(s));
  if(hiddenRooms.length){ hr.hidden=false;
    hr.innerHTML='<div class="hr-title">'+esc(t("hiddenTitle")(hiddenRooms.length))+'</div><div class="hr-chips">'
      +hiddenRooms.map(s=>'<button class="hr-chip" data-s="'+esc(s)+'" type="button" title="'+esc(t("toggleFinished"))+'">'
        +ICON.building+esc(stat[s].label)
        +(stat[s].aborted?' <span class="hr-ab">'+ICON.stop+stat[s].aborted+'</span>':'')
        +(stat[s].done?' <span>'+ICON.done+stat[s].done+'</span>':'')+'</button>').join('')+'</div>';
  } else { hr.hidden=true; hr.innerHTML=''; }

  const run=all.filter(a=>a.status==="running").length, idle=all.filter(a=>a.status==="stale").length, aborted=all.filter(a=>a.status==="aborted").length, done=all.filter(a=>a.status==="done").length;
  document.getElementById("counts").innerHTML='<span class="c-run">'+ICON.run+'<b>'+run+'</b><em>'+esc(t("working"))+'</em></span>'
    +(idle?'<span class="c-idle">'+ICON.idle+'<b>'+idle+'</b><em>'+esc(t("idleN"))+'</em></span>':'')
    +(aborted?'<span class="c-abort">'+ICON.stop+'<b>'+aborted+'</b><em>'+esc(t("abortedN"))+'</em></span>':'')
    +'<span class="c-done">'+ICON.done+'<b>'+done+'</b><em>'+esc(t("finished"))+'</em></span>';

  const dg=document.getElementById("diag"); const sk=(lastPayload&&lastPayload.skipped)||0;
  if(sk>0){ dg.textContent=t("skippedN")(sk); dg.hidden=false; } else dg.hidden=true;
}

function fillDrawer(a){ document.getElementById("dav").textContent=a.emoji;
  document.getElementById("dnm").textContent=deskName(a);
  document.getElementById("dro").textContent=a.title?(roomLabel(a)+" · "+personaName(a)):(a.role||a.task_short||"");
  const now=Date.now();
  const settled=(a.status==="done"||a.status==="stale"||a.status==="aborted");
  const dur=settled?(((a.end_ms||a.mtime_ms||0)-(a.start_ms||0))||null):(now-(a.start_ms||now));  // settled: freeze elapsed at last activity; running: live
  const stx=a.status==="running"?t("dWorking"):a.status==="done"?t("dDone"):a.status==="aborted"?t("dAborted"):t("dStale");
  let h='<div class="row"><span class="chip">'+esc(stx)+'</span>'+
    (a.subagent_type?'<span class="chip" dir="auto">'+esc(a.subagent_type)+'</span>':'')+
    '<span class="chip" dir="auto">'+(a.status==="done"?t("dDuration"):t("dElapsed"))+fmt(dur)+'</span>'+
    (a.tool?'<span class="chip" dir="auto">'+esc(a.tool)+'</span>':'')+'</div>';
  h+='<h3>'+esc(t("dAction"))+'</h3><div class="box">'+esc(activityLabel(a))+'</div>';
  // task & result are journal content (could be English or Hebrew) -> dir=auto so
  // each adapts to its own text instead of being forced LTR (broke Hebrew tasks).
  h+='<h3>'+esc(t("dTask"))+'</h3><div class="box" dir="auto" tabindex="0" role="region" aria-label="'+esc(t("dTask"))+'">'+esc(a.task||t("taskUnavailable"))+'</div>';
  if(a.result) h+='<h3>'+esc(t("dResult"))+'</h3><div class="box" dir="auto" tabindex="0" role="region" aria-label="'+esc(t("dResult"))+'">'+esc(a.result)+'</div>';
  document.getElementById("dbody").innerHTML=h; }
let lastFocused=null;
function openDrawer(id){ const e=els[id]; if(!e) return; lastFocused=document.activeElement;
  openId=id; openData=e.data; fillDrawer(e.data);
  document.getElementById("drawer").classList.add("open"); document.getElementById("backdrop").classList.add("show");
  document.querySelectorAll("header,#app").forEach(el=>{ el.inert=true; el.setAttribute("aria-hidden","true"); });  // take background out of the a11y tree
  document.getElementById("dclose").focus(); }                       // move focus into the dialog
function closeDrawer(){ if(!openId) return; openId=null; openData=null;
  document.getElementById("drawer").classList.remove("open"); document.getElementById("backdrop").classList.remove("show");
  document.querySelectorAll("header,#app").forEach(el=>{ el.inert=false; el.removeAttribute("aria-hidden"); });  // return background to the a11y tree
  if(lastFocused&&lastFocused.focus){ try{ lastFocused.focus(); }catch(_){} } lastFocused=null; }  // restore focus
document.getElementById("dclose").addEventListener("click",closeDrawer);
document.getElementById("backdrop").addEventListener("click",closeDrawer);
// trap Tab within the open drawer (modal dialog behavior)
document.getElementById("drawer").addEventListener("keydown",e=>{ if(e.key!=="Tab"||!openId) return;
  const f=document.getElementById("drawer").querySelectorAll('button,[href],input,[tabindex]:not([tabindex="-1"])');
  if(!f.length) return; const first=f[0], last=f[f.length-1];
  if(e.shiftKey && document.activeElement===first){ e.preventDefault(); last.focus(); }
  else if(!e.shiftKey && document.activeElement===last){ e.preventDefault(); first.focus(); } });
// arrow-key movement across cards (RTL mirrors the horizontal direction)
function moveCardFocus(e){ const cards=[].slice.call(document.querySelectorAll(".ws")); if(!cards.length) return;
  const i=cards.indexOf(document.activeElement);
  if(i<0) return;            // no card focused -> let the browser scroll the page (Tab/click enters the grid)
  e.preventDefault();
  let d=(e.key==="ArrowRight"||e.key==="ArrowDown")?1:-1;
  if((e.key==="ArrowLeft"||e.key==="ArrowRight") && document.documentElement.dir==="rtl") d=-d;
  cards[Math.max(0,Math.min(cards.length-1,i+d))].focus(); }
// global shortcuts: Esc closes; "/" focuses search; "f" toggles finished; arrows move card focus
document.addEventListener("keydown",e=>{
  if(e.key==="Escape"){ if(openId) closeDrawer(); return; }
  if(openId) return;
  const tag=((document.activeElement&&document.activeElement.tagName)||"").toLowerCase();
  if(tag==="input"||tag==="textarea") return;            // don't hijack typing
  if(e.key==="/"||e.code==="Slash"){ e.preventDefault(); document.getElementById("search").focus(); }
  else if(e.key==="f"||e.key==="F"||e.code==="KeyF"){ setShowDone(!document.getElementById("showDone").checked); }
  else if(e.key.indexOf("Arrow")===0){ moveCardFocus(e); } });
document.getElementById("showDone").addEventListener("change",e=>{ setShowDone(e.target.checked); });
document.getElementById("langBtn").addEventListener("click",()=>setLang(lang==="en"?"he":"en"));
document.getElementById("themeBtn").addEventListener("click",()=>setTheme(theme==="dark"?"light":"dark"));
(function(){ const ht=document.getElementById("hToggle"); if(!ht) return;   // null-safe: never let a missing control abort the whole script
  ht.addEventListener("click",()=>{ const open=document.querySelector("header").classList.toggle("tools-open"); ht.setAttribute("aria-expanded",open?"true":"false"); }); })();
// follow OS theme changes only while the user hasn't made an explicit choice
try{ const mq=window.matchMedia("(prefers-color-scheme: light)");
  (mq.addEventListener?mq.addEventListener.bind(mq,"change"):mq.addListener.bind(mq))(e=>{
    if(!themeExplicit){ theme=e.matches?"light":"dark"; document.documentElement.setAttribute("data-theme",theme); applyThemeBtn(); } }); }catch(e){}
document.getElementById("app").addEventListener("click",e=>{ if(e.target.closest(".btn-demo")) setDemo(true);
  const rd=e.target.closest(".rdone"); if(rd){ e.stopPropagation();
    if(rd.classList.contains("rabort")) toggleRoomAborted(rd.dataset.s); else toggleRoomDone(rd.dataset.s); } });
document.getElementById("hidden").addEventListener("click",e=>{ const c=e.target.closest(".hr-chip"); if(c) revealRoom(c.dataset.s); });
document.getElementById("exitDemoBtn").addEventListener("click",()=>setDemo(false));
document.getElementById("search").addEventListener("input",e=>{ searchQuery=e.target.value;
  clearTimeout(searchT); searchT=setTimeout(()=>render(),120); });   // client-side filter over the cached payload
document.getElementById("muteBtn").addEventListener("click",()=>setMuted(!muted));

setInterval(()=>{ const now=Date.now(); document.querySelectorAll(".timer").forEach(el=>{
  const s=+el.dataset.start,en=+el.dataset.end,mt=+el.dataset.mtime,st=el.dataset.status,sess=el.dataset.session;
  let v;
  if(sess==="1"){                                   // conversation desk: time since last activity
    if(st==="done"||st==="stale"||st==="aborted"){ const b=en||mt;  // SETTLED desk (finished/idle/stopped): coarsen to "~Nmin/~Nh ago" and stop ticking, so it doesn't look like it's still working with a runaway MM:SS
      el.textContent=b?fmtAgo(now-b):fmt(null); return; }
    v=mt?(now-mt):null;                             // running only: live MM:SS since last activity (always show seconds)
  }
  else if(!s){ el.textContent=fmt(null); return; }  // unknown start -> "--:--" instead of blank
  else if(st==="done"&&en) v=en-s;                  // finished: final duration
  else if(st==="stale"||st==="aborted") v=(mt&&mt>s)?(mt-s):null;   // idle/stopped: freeze at last activity, not a runaway count to now
  else v=now-s;                                     // running: live
  el.textContent=fmt(v); }); },1000);

// Tool-family legend: a stable swatch + label for each running-screen color
// (matches the .ws.running[data-fam=...] glow colors in the CSS).
const LEG_FAMS=[["#16c0dd","legSearch"],["#5b8def","legRead"],["#e6a92e","legEdit"],["#1fc25a","legCmd"],["#c45bd0","legAgent"]];
function renderLegend(){ const el=document.getElementById("legend"); if(!el) return;
  const sw=(c,label)=>'<span class="lg"><span class="sw" style="color:'+c+'"></span>'+esc(label)+'</span>';
  const ico=(svg,c,label)=>'<span class="lg"><span class="lg-ic" style="color:'+c+'">'+svg+'</span>'+esc(label)+'</span>';
  const cap=label=>'<span class="lg-cap">'+esc(label)+'</span>';
  el.innerHTML=cap(t("legStatus"))+ico(ICON.run,"#7ee29a",t("working"))+ico(ICON.idle,"#e6c07e",t("idleN"))+ico(ICON.stop,"#ef8a8a",t("legAborted"))+ico(ICON.done,"#9fb0e6",t("finished"))
    +'<span class="lg-sep">|</span>'
    +cap(t("legTool"))+LEG_FAMS.map(f=>sw(f[0],t(f[1]))).join(""); }

// Positive liveness: a pulsing dot + "updated Xs ago", so the office visibly
// proves it is polling (not just a banner when the connection drops).
let lastLiveMs=0, connected=false;
function tickLive(){ const ls=document.getElementById("livestat"), ago=document.getElementById("liveago");
  if(!ls||!ago) return;
  if(!connected){ ls.className="livestat "+(booted?"off":"boot");      // neutral "connecting" before the first scan
    ago.textContent=t(booted?"liveDisc":"connecting"); return; }
  ls.className="livestat on";
  ago.textContent=t("liveAgo")(Math.max(0,Math.round((Date.now()-lastLiveMs)/1000)), window.innerWidth<=360); }
setInterval(tickLive,1000);
let __rtz; window.addEventListener("resize",function(){ clearTimeout(__rtz); __rtz=setTimeout(retruncateRooms,120); });

let polling=false, failStreak=0;
function setConnected(ok){ connected=ok; const el=document.getElementById("reconnect");
  el.hidden=ok; if(!ok) el.textContent=t("reconnecting"); tickLive(); }
// Data source adapter. Two front ends share this exact UI:
//  * Cursor/VS Code extension webview -> the extension host reads the journals
//    and PUSHES payloads via postMessage (no HTTP server, no port).
//  * Standalone browser/Python server -> the page POLLS /api/agents over HTTP.
// acquireVsCodeApi only exists inside a webview, so its presence picks the mode.
const __CT_VS=(typeof acquireVsCodeApi==="function")?acquireVsCodeApi():null;
// Embedded = running inside the Cursor/VS Code webview (push-driven, no Python
// server). The synthetic demo has no data source here, so we hide its affordances.
const EMBEDDED=!!__CT_VS;
if(EMBEDDED) document.documentElement.classList.add("embedded");
async function poll(){ if(__CT_VS) return;           // webview is push-driven; never fetch
  if(polling) return;          // in-flight guard: never stack scans
  polling=true;
  try{ const ph=(location.search.match(/[?&]phase=(\\d{1,4})\\b/)||[])[1];   // forward a page-level ?phase to freeze a frame (screenshots)
    const sc=(location.search.match(/[?&]scene=([a-z0-9]{1,16})\\b/)||[])[1]; // forward ?scene= to pick a demo office variant
    const q=demoMode?("?demo=1"+(ph?("&phase="+ph):"")+(sc?("&scene="+sc):"")):"";
    const r=await fetch(API_BASE+"/api/agents"+q,{cache:"no-store"});
    if(!r.ok) throw new Error("http "+r.status);
    booted=true; lastLiveMs=Date.now(); render(await r.json()); failStreak=0; setConnected(true);
  }catch(e){ if(++failStreak>=2) setConnected(false); }   // show the banner only after a couple of misses
  finally{ polling=false; } }
applyLang();
if(__CT_VS){
  window.addEventListener("message",function(ev){ const d=ev.data||{};
    if(d&&d.type==="agents"){ booted=true; lastLiveMs=Date.now(); render(d.payload); failStreak=0; setConnected(true); } });
  __CT_VS.postMessage({type:"ready"});      // ask the host for the first scan
} else { poll(); setInterval(poll,POLL_MS); }
function unlock(){ try{ audioCtx=audioCtx||new (window.AudioContext||window.webkitAudioContext)(); audioCtx.resume(); }catch(e){} }
window.addEventListener("click",unlock,{once:true}); window.addEventListener("keydown",unlock,{once:true});
</script>
</body>
</html>"""
# === END GENERATED PAGE ============================================================


def page_html():
    """The page HTML to serve. Dev convenience: when running from a source
    checkout (ui/theater.html sits next to this script) serve it LIVE, so UI edits
    show on a plain browser refresh with no `build_ui.py` and no server restart.
    Packaged single-file installs have no ui/ dir and fall back to the inlined
    PAGE, so distribution stays a self-contained file."""
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui", "theater.html")
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
    except Exception:
        pass
    return PAGE


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        data = body.encode("utf-8")
        try:
            self._write_response(code, ctype, data)
        except ConnectionError:
            # The client (browser) hung up mid-response -- e.g. it navigated away
            # or cancelled an in-flight poll. There's nothing left to write to;
            # swallow it quietly instead of dumping a traceback (degrade-not-crash).
            pass

    def _write_response(self, code, ctype, data):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        # CORS: allow ONLY a VS Code webview origin to read the API, so the
        # journals stay unreadable to an ordinary web page (the server is
        # loopback-only regardless). The VS Code extension embeds this page in a
        # WebviewPanel and fetches from here.
        origin = self.headers.get("Origin", "")
        if origin.startswith("vscode-webview://"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        # Defense-in-depth: even though every sink is escaped and we bind to
        # loopback, a restrictive CSP keeps a future regression from exfiltrating
        # journal text or loading remote code. 'unsafe-inline' is unavoidable
        # because the single-file design inlines all script/style in PAGE.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; "
            "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # Identity header: lets the VS Code extension confirm it is talking to the
        # real server (not some other process squatting the port) before it embeds
        # the page in a scripts-enabled webview.
        self.send_header("X-Cursor-Theater", __version__)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        # DNS-rebinding guard: a loopback bind + CORS still let a malicious page
        # rebind its own hostname to 127.0.0.1 and read /api/agents *same-origin*
        # (no CORS check applies). Rejecting any non-loopback Host closes that --
        # the rebound request carries the attacker's hostname in Host. An empty
        # Host (HTTP/1.0 local clients) is allowed; browsers always send one.
        host = (urlsplit("//" + self.headers.get("Host", "")).hostname or "").lower()
        if host and host not in ("localhost", "127.0.0.1", "::1"):
            self._send(403, "forbidden", "text/plain; charset=utf-8")
            return
        path = self.path.split("?", 1)[0]   # route on the path; query (?demo=1) is read separately
        if path == "/api/agents":
            try:
                # --demo forces demo for the whole process; ?demo=1 lets the
                # empty-office "Watch a demo" button (and a shareable URL) pull
                # the synthetic office on demand. Both stay read-only and local:
                # demo_payload() never touches the real journals either way.
                _q = parse_qs(urlsplit(self.path).query)
                want_demo = DEMO or _q.get("demo", [""])[0] == "1"
                if want_demo:
                    _ph = _q.get("phase", [""])[0]
                    # ?scene= picks a synthetic office variant (default = the
                    # original 2-instance loop used by the hero GIF). Whitelisted
                    # so an arbitrary value can't reach unexpected code paths.
                    _scene = _q.get("scene", [""])[0]
                    if _scene not in ("rooms4",):
                        _scene = ""
                    # bound the length so a huge digit string can't force an O(n^2)
                    # int parse on Python < 3.11 (no int-str conversion limit there)
                    payload = demo_payload(int(_ph) if (_ph.isdigit() and len(_ph) <= 4) else None, scene=_scene)
                else:
                    payload = scan_agents()
                body = json.dumps(payload, ensure_ascii=False)
            except Exception as e:
                # Keep detail server-side only; the response can reach a local
                # process or a pasted screenshot, and str(e) may embed the home path.
                print("!! scan error:", repr(e))
                body = json.dumps({"error": "scan failed"})
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/" or path.startswith("/index"):
            self._send(200, page_html(), "text/html; charset=utf-8")
        elif path == "/favicon.ico":
            # Browsers auto-request this; answer 204 so it isn't a console 404 on every load.
            self._send(204, "", "image/x-icon")
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")


USAGE = """Cursor Theater %s - a live office of your Cursor agent conversations.

Usage: cursor-theater [options]

  --demo         show a synthetic, populated office (reads no real transcripts)
  --no-browser   do not open the browser on start
  --port N       listen on port N (default %d; or set CURSOR_THEATER_PORT)
  --version,-V   print version and exit
  --help,-h      show this help and exit

Then open http://localhost:%d
"""


class TheaterServer(ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR lets a second process silently bind a port that's
    # already in use (and the OS load-balances between them) -- so a stray second
    # copy would serve different data with no error. Disabling reuse there makes a
    # duplicate bind fail loudly instead; POSIX keeps it on to avoid TIME_WAIT
    # restart pain.
    allow_reuse_address = (os.name != "nt")


def _arg_port(args):
    """--port N overrides CLAUDE_THEATER_PORT / the default; falls back on bad input."""
    if "--port" in args:
        try:
            v = int(args[args.index("--port") + 1])
            if 0 < v < 65536:
                return v
        except (IndexError, ValueError):
            pass
        print("!! --port needs a number 1-65535; using %d" % PORT, flush=True)
    return PORT


def main():
    global DEMO
    args = sys.argv[1:]
    if "--version" in args or "-V" in args:
        print("cursor-theater %s" % __version__)
        return
    if "--help" in args or "-h" in args:
        print(USAGE % (__version__, PORT, PORT))
        return

    DEMO = "--demo" in args
    no_browser = "--no-browser" in args or bool(
        os.environ.get("CURSOR_THEATER_NO_BROWSER") or os.environ.get("CLAUDE_THEATER_NO_BROWSER"))
    port = _arg_port(args)

    if not DEMO:
        has_journals = os.path.isdir(PROJECTS_DIR) and any(glob.glob(TRANSCRIPT_GLOB))
        if not has_journals:
            print("!! No Cursor transcripts found under %s." % PROJECTS_DIR, flush=True)
            print("   Try `cursor-theater --demo` for a synthetic office, or start a "
                  "Cursor agent conversation first.", flush=True)

    try:
        srv = TheaterServer(("127.0.0.1", port), Handler)
    except OSError as e:
        print("!! Could not start on 127.0.0.1:%d (%s)." % (port, getattr(e, "strerror", None) or e), flush=True)
        print("   Another copy may already be running. Start it on another port:", flush=True)
        print("   cursor-theater --port %d   (or set CURSOR_THEATER_PORT)" % (port + 1), flush=True)
        sys.exit(1)

    url = "http://localhost:%d" % port
    print("Cursor Theater %s%s -> %s   (Ctrl+C to stop)" % (__version__, " [demo]" if DEMO else "", url), flush=True)
    print("Demo mode: synthetic office, no real transcripts are read." if DEMO else "Watching: " + PROJECTS_DIR, flush=True)
    # Open the browser only after the socket is bound (no first-load race), and
    # so pipx/pip users get the same one-click UX as start.cmd.
    if not no_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
