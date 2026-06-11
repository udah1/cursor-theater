# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.2] - 2026-06-11

### Added

- **Background update check**: the extension polls GitHub releases on an interval
  (`cursorTheater.updateCheckMinutes`, default 60; `0` disables) plus once shortly
  after startup. When a newer release exists, the status bar shows
  `Agents Theater (N) → vX.Y.Z` with a warning background; clicking opens the menu
  to download it. The status bar now always shows the working count in braces.
- A "Check for updates (vX.Y.Z)" item in the status-bar menu, and a draggable-view
  hint on the "Side-bar theater" item.

## [0.1.1] - 2026-06-11

### Changed

- The side-bar view header now reads "Cursor Theater: Cursor Agents" instead of
  the duplicated/empty label.

### Fixed

- Hide the "Watch a live demo" button and demo chip inside the extension webview
  (it has no demo data source there); the empty state now points you to start an
  agent conversation. The demo still works in the browser via the Python server.
- Responsive narrow-layout fixes: move the responsive `@media` blocks after the
  base rules so they actually apply (`<360px` header/room padding, gaps, hidden
  project icon), middle-truncate long room titles, and override the webview
  host's injected `body` padding so gutters match the browser.

## [0.1.0] - 2026-06-10

First release of **Cursor Theater**, a port of
[Claude Theater](https://github.com/asafabram-ship-it/claude-theater) adapted for
Cursor.

### Added

- **Native Cursor extension** (`extension/`) — no HTTP server, no port. Reads
  `~/.cursor/projects` transcripts and Cursor's global `state.vscdb` in-process,
  watches for changes, and pushes updates into the webview. Ships an Activity Bar
  view, a dockable side-bar view, a status-bar item with a live working-agent
  count, and a full editor-tab command.
- Conversations grouped **per Cursor instance / project** (one room per project).
- Real chat **titles / status / timestamps** read from `state.vscdb` via the
  read-only `sqlite3` CLI (indexed `json_extract`, no full-file load), degrading
  gracefully to first-message + file mtime when `sqlite3` is unavailable.
- **Shared single-source UI** (`ui/theater.html`) consumed by both the extension
  and the standalone Python server. Responsive sidebar layout, RTL support,
  middle-truncated room titles, and a status/tool legend.
- **Standalone Python server** (`cursor_theater.py`) for viewing in a real browser
  and for live UI development; `build_ui.py` inlines the UI for single-file use.
- Bilingual UI: English by default, Hebrew toggle (persisted, RTL-aware), `--demo`
  mode for a zero-setup synthetic office.

[Unreleased]: https://github.com/udah1/cursor-theater/compare/cursor-v0.1.2...HEAD
[0.1.2]: https://github.com/udah1/cursor-theater/compare/cursor-v0.1.1...cursor-v0.1.2
[0.1.1]: https://github.com/udah1/cursor-theater/compare/cursor-v0.1.0...cursor-v0.1.1
[0.1.0]: https://github.com/udah1/cursor-theater/releases/tag/cursor-v0.1.0
