<!-- Thanks for contributing to Cursor Theater! Keep it short. -->

## What & why

<!-- What does this change, and why? Link any related issue (e.g. Closes #12). -->

## Checklist

- [ ] The extension still compiles (`cd extension && npm run compile`)
- [ ] If this touches the UI (`ui/theater.html`), I ran `python3 build_ui.py` and
      `cd extension && npm run copy-ui` so the server and extension stay in sync
- [ ] If this changes status/liveness or the emitted payload, I updated **both**
      `cursor_theater.py` and `extension/src/scan.ts`
- [ ] If this adds or changes any UI string, both `en` and `he` entries in the
      `I18N` table are updated (parity)
- [ ] No telemetry or outbound network calls added
