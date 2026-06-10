# Cursor Theater (extension)

A live, grouped office of your Cursor agent conversations, shown natively inside
Cursor in a webview panel. Unlike the standalone server, this extension runs **no
HTTP server and opens no port**: the extension host reads `~/.cursor/projects` and
Cursor's global `state.vscdb` directly, watches them for changes, and pushes data
into the panel.

Because each Cursor window runs its own extension host and nothing is shared
between windows, opening the theater in several windows is safe and independent -
closing one window never affects another.

## Usage

- Command palette -> **Cursor Theater: Open**.

## Architecture

- `src/scan.ts` - TypeScript port of the `cursor_theater.py` data layer; produces
  the exact JSON payload the UI expects.
- `src/composer.ts` - reads chat titles/status/timestamps from `state.vscdb` via
  the read-only `sqlite3` CLI (`json_extract`, indexed, no full-file load).
  Degrades gracefully (first-message + file mtime) if `sqlite3` is unavailable.
- `src/extension.ts` - the command, a singleton webview panel per window, a file
  watcher (debounced) plus a safety interval, and the `postMessage` channel.
- The UI is the shared single source of truth at `../ui/theater.html` (the same
  file the Python server inlines). `npm run copy-ui` bundles it into `media/` for
  the `.vsix`.

## Develop

```
npm install
npm run compile      # tsc -> out/
# press F5 (Run Cursor Theater Extension) to launch an Extension Development Host
```

## Package

```
npm run package      # compiles, copies the UI, and produces cursor-theater-<ver>.vsix
```

Install the `.vsix` in Cursor via the Extensions view -> "..." -> Install from VSIX.

## Standalone Python server (optional)

The extension is the primary, server-less way to use Cursor Theater. The original
single-file Python server (`../cursor_theater.py`) is **kept on purpose** for two
cases the in-editor extension can't cover:

- **View it in a real browser** - a browser tab can't read your transcripts or
  `state.vscdb`, so it needs an HTTP source. The Python server provides exactly
  that (the shared UI auto-switches to `fetch('/api/agents')` when it isn't running
  inside a webview).
- **Live UI development** - the server serves `../ui/theater.html` fresh on every
  request, so you can edit the UI and just refresh the browser - no recompile, no
  repackaging the `.vsix`.

```bash
python3 ../cursor_theater.py            # scans ~/.cursor and opens http://localhost:7333
python3 ../cursor_theater.py --demo     # synthetic office, no sessions needed
```

It binds to `127.0.0.1` only and is read-only. The extension and the server share
the **same UI** (`../ui/theater.html`); `python3 ../build_ui.py` re-inlines that file
into `cursor_theater.py` and `npm run copy-ui` bundles it into the extension - run
both after a UI change so all three stay in sync.

> Note: the server and the extension have **separate scan implementations**
> (Python vs `src/scan.ts`). If you change status/liveness logic, update both.

## Notes

- Requires the `sqlite3` CLI for real chat titles/timestamps (present by default on
  macOS; on Windows it degrades to the first user message + file mtime).
- The standalone Python server (`../cursor_theater.py`) remains fully usable; both
  share the same UI.
