"""Fail the build if the frontend can be made to execute untrusted input.

Template names, field names, record values and server error strings all reach
the DOM. Any of them passing through an HTML sink is an XSS, so the ban is
enforced here rather than left to review.

Comments are stripped first: the rule is documented in prose inside app.js, and
a naive substring search would flag its own documentation.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "src/label_sheet_generator/static"

HTML_SINKS = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write")
EVAL_SINKS = (r"\beval\s*\(", r"\bnew\s+Function\s*\(")
CDN = r"https?://(?:cdn|unpkg|jsdelivr|fonts\.googleapis|ajax\.googleapis)"


def strip_js_comments(source: str) -> str:
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//.*$", "", without_block)


def strip_html_comments(source: str) -> str:
    return re.sub(r"<!--.*?-->", "", source, flags=re.DOTALL)


def main() -> int:
    failures: list[str] = []

    js_path = STATIC / "app.js"
    js = strip_js_comments(js_path.read_text(encoding="utf-8"))
    for sink in HTML_SINKS:
        if re.search(rf"\b{sink}\b", js):
            failures.append(f"app.js uses {sink}; build DOM with createElement/textContent")
    for pattern in EVAL_SINKS:
        if re.search(pattern, js):
            failures.append(f"app.js matches {pattern}; no dynamic code execution")

    html_path = STATIC / "index.html"
    html = strip_html_comments(html_path.read_text(encoding="utf-8"))
    if re.search(r"\son[a-z]+\s*=\s*[\"']", html):
        failures.append("index.html has an inline event handler; use addEventListener")

    for name in ("index.html", "app.css", "app.js"):
        if re.search(CDN, (STATIC / name).read_text(encoding="utf-8")):
            failures.append(f"{name} references a CDN; the frontend must work offline")

    for failure in failures:
        print(f"::error::{failure}")
    if failures:
        return 1
    print("frontend safety checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
