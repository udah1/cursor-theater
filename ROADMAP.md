# Roadmap

Directional, not a promise. Issues and PRs welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md). The guiding constraints don't change:
**local-only, read-only, no telemetry.**

## Now — 0.1.x

- Native, server-less Cursor extension: editor tab, dockable side-bar view, and a
  status-bar item with a live working-agent count.
- Conversations grouped per Cursor instance / project, with real chat titles and
  status from `state.vscdb`.
- Shared single-source UI (`ui/theater.html`) for both the extension and the
  standalone Python server; bilingual EN/HE with RTL.

## Next

- **Open VSX / Marketplace** publishing of the extension.
- Resilience to Cursor's transcript / `state.vscdb` format changes as they evolve.
- More languages — each is a single edit to the `I18N` table in `ui/theater.html`.
- Broader tool labels / coverage.

## Ideas (unscheduled)

- Native finish notifications.
- Themes / skins.
- Per-conversation timing stats (durations, tool histograms).

## Non-goals

- No telemetry, no network calls; the extension opens no port and the Python
  server stays on `127.0.0.1`.
- No control over agents — Cursor Theater only *watches* the journals Cursor
  already writes.
