# Contributing to Cursor Theater

Thanks for helping! Cursor Theater reads the transcripts Cursor writes under
`~/.cursor/projects` and the chat metadata in Cursor's global `state.vscdb`. As
those formats evolve, the most valuable contribution is a **report (with a
scrubbed sample) when something renders oddly**.

## Reporting a rendering / format issue

If a conversation shows the wrong title, status, or timer — or a project room is
missing:

1. Note your Cursor version and OS.
2. If you can, attach a **scrubbed** snippet of the relevant
   `~/.cursor/projects/**/**.jsonl` line(s) — replace every prompt/result with
   placeholder text and keep only the structure (`type`, tool names, timestamps).
   **Never paste unscrubbed transcript content** — these files contain real
   conversation text.
3. Open an issue with the details.

## Adding a language

The UI is bilingual (English/Hebrew). To add a language, edit the `I18N` table
in `ui/theater.html` — keep the key set identical across languages. After a UI
change, re-sync both consumers:

```bash
python3 build_ui.py                 # inline ui/theater.html into cursor_theater.py
cd extension && npm run copy-ui     # bundle it into the extension's media/
```

## Dev setup

**Extension** (TypeScript, needs Node.js):

```bash
cd extension
npm install
npm run compile        # tsc -> out/
# press F5 for an Extension Development Host, or:
npm run package        # produces cursor-theater-<version>.vsix
```

**Python server / UI** (pure standard library):

```bash
python3 cursor_theater.py --demo    # synthetic office in the browser
python3 cursor_theater.py           # your real Cursor conversations
```

## Ground rules

- **Privacy first.** Keep everything local; never add telemetry or outbound
  calls. The extension opens no port; the Python server binds `127.0.0.1` only.
- **Read-only.** Only read the journals; never write or control agents.
- **Keep the UI a single source of truth** (`ui/theater.html`); run `build_ui.py`
  and `npm run copy-ui` so the server and extension stay in sync.
- The Python server (`cursor_theater.py`) and the extension
  (`extension/src/scan.ts`) have **separate scan implementations** — if you change
  status/liveness logic, update both.
