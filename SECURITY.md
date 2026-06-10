# Security Policy

Cursor Theater is a local-only visualizer. Its security posture is part of the
product, so this policy doubles as a short statement of how it protects you.

## Security & privacy posture

- **The extension opens no port** and makes no network calls. It reads your
  journals in-process and renders them in a webview.
- **The optional Python server is local-only.** It binds to `127.0.0.1`
  (loopback) — never exposed on your network — with a loopback `Host` allowlist
  (DNS-rebinding protection), a strict `Content-Security-Policy`, and
  `X-Content-Type-Options: nosniff`.
- **Read-only.** It reads the transcripts and `state.vscdb` Cursor already
  writes; it never modifies them and never starts, stops, or talks to your agents.
- **Nothing leaves your machine.** No telemetry, no analytics, no outbound calls.
- **Minimal supply-chain surface.** The Python server is a single standard-library
  file; the extension uses only build-time dev dependencies.

## Supported versions

This is a `0.x` project; only the latest published version is supported. Please
reproduce on the latest release before reporting.

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Use GitHub
**private vulnerability reporting** — the *Security → Report a vulnerability*
button on this repository.

Please include reproduction steps, the affected version, and your OS / Cursor
version. Thank you for helping keep the project safe.
