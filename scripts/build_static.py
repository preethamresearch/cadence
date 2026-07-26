"""Build a static export of the landing page and console for GitHub Pages.

The live console needs a backend — a WebSocket bridge to Gemini Live and an
API key. A judge opening a link has neither, and will not sign up for either.
The replay console needs nothing at all: it drives the real rendering path from
a recorded session entirely in the browser.

So the static build ships the landing page and the replay console, and is
explicit that live mode requires running the server locally. That is a better
first impression than a deployed page whose primary button fails.

Rewrites absolute paths to relative, because GitHub Pages serves the site from
a subpath (`/cadence/`) where `/static/...` would resolve to the wrong origin.

    python scripts/build_static.py
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "app" / "static"
OUT = ROOT / "site"

STATIC_FLAG = (
    '<script>window.__CADENCE_STATIC__ = true;</script>\n'
)


def rewrite(html: str, *, console_href: str) -> str:
    """Absolute -> relative, and point console links at the built filename."""
    html = html.replace('href="/static/', 'href="static/')
    html = html.replace('src="/static/', 'src="static/')
    # Order matters: the query-string variant must be replaced first, or the
    # bare "/console" rule eats its prefix and leaves a dangling query.
    html = html.replace('href="/console?replay=1"', f'href="{console_href}?replay=1"')
    html = html.replace('href="/console"', f'href="{console_href}?replay=1"')
    html = html.replace('href="/"', 'href="index.html"')
    # Mark the build so the front-end can force replay mode.
    return html.replace("</head>", STATIC_FLAG + "</head>", 1)


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    shutil.copytree(SRC / "css", OUT / "static" / "css")
    shutil.copytree(SRC / "js", OUT / "static" / "js")
    shutil.copytree(SRC / "vendor", OUT / "static" / "vendor")

    # ES module specifiers are absolute in the served app; make them relative.
    for path in (OUT / "static" / "js").rglob("*.js"):
        text = path.read_text()
        text = text.replace('from "/static/vendor/', 'from "../vendor/')
        text = text.replace('"/static/js/', '"./')
        text = re.sub(r'addModule\("/static/js/', 'addModule("static/js/', text)
        path.write_text(text)

    (OUT / "index.html").write_text(
        rewrite((SRC / "landing.html").read_text(), console_href="console.html")
    )
    (OUT / "console.html").write_text(
        rewrite((SRC / "index.html").read_text(), console_href="console.html")
    )

    # Jekyll would otherwise ignore files and folders it does not recognise.
    (OUT / ".nojekyll").write_text("")

    files = sum(1 for _ in OUT.rglob("*") if _.is_file())
    print(f"built {OUT} ({files} files)")
    print("  index.html    landing page")
    print("  console.html  replay console (no backend required)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
