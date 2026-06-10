#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regenerate the embedded PAGE in cursor_theater.py from the single source of
truth, ui/theater.html.

The UI is shared by two front ends:
  * the standalone Python server (cursor_theater.py), which must stay a single
    self-contained file with no runtime file reads, so the HTML is inlined into
    the PAGE string literal between sentinel comments; and
  * the Cursor/VS Code extension, which loads ui/theater.html directly.

Edit ui/theater.html, then run `python3 build_ui.py` to refresh PAGE.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent
UI = ROOT / "ui" / "theater.html"
TARGET = ROOT / "cursor_theater.py"

BEGIN = "# === BEGIN GENERATED PAGE"
END = "# === END GENERATED PAGE"
# Match the whole sentinel block (the BEGIN comment line(s), the PAGE assignment,
# and the END comment line) so we can replace it wholesale.
BLOCK_RE = re.compile(
    r"# === BEGIN GENERATED PAGE.*?# === END GENERATED PAGE[^\n]*\n",
    re.DOTALL,
)


def escape_for_triple_quoted(html: str) -> str:
    """Escape an HTML/JS/CSS document so it is safe inside a Python \"\"\"...\"\"\"
    literal: backslashes first (so JS regexes like \\b survive), then any literal
    triple-double-quote run."""
    out = html.replace("\\", "\\\\")
    out = out.replace('"""', '\\"\\"\\"')
    return out


def main() -> int:
    if not UI.is_file():
        print("!! missing %s -- run the one-time extraction first" % UI, file=sys.stderr)
        return 1
    html = UI.read_text(encoding="utf-8")
    block = (
        "# === BEGIN GENERATED PAGE (do not edit by hand; source of truth: ui/theater.html;\n"
        "# regenerate with: python3 build_ui.py) "
        "============================================\n"
        'PAGE = """' + escape_for_triple_quoted(html) + '"""\n'
        "# === END GENERATED PAGE "
        "============================================================\n"
    )
    src = TARGET.read_text(encoding="utf-8")
    if not BLOCK_RE.search(src):
        print("!! could not find the GENERATED PAGE sentinels in %s" % TARGET, file=sys.stderr)
        return 1
    new = BLOCK_RE.sub(lambda _m: block, src, count=1)
    if new != src:
        TARGET.write_text(new, encoding="utf-8")
        print("regenerated PAGE in %s from %s" % (TARGET.name, UI.relative_to(ROOT)))
    else:
        print("PAGE already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
