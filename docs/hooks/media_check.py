"""Fail the build when raw HTML media paths do not resolve.

MkDocs rewrites markdown image links per page depth, but raw HTML
(`<img src=...>`, `<video poster=...>`, `<source src=...>`) passes
through verbatim, so a wrong relative prefix 404s in production while
`mkdocs build --strict` stays green. This hook walks the rendered site
and resolves every relative src/poster against its page directory.
"""

from __future__ import annotations

import os
import re

_ATTR = re.compile(r'\b(?:src|poster)="([^"]+)"')
_SKIP_PREFIXES = ("http://", "https://", "//", "data:", "/")


def on_post_build(config):
    """Resolve every relative src/poster in the built HTML; raise on a miss."""
    site_dir = config["site_dir"]
    missing = []
    for root, _dirs, files in os.walk(site_dir):
        for name in files:
            if not name.endswith(".html"):
                continue
            page = os.path.join(root, name)
            with open(page, encoding="utf-8", errors="ignore") as fh:
                html = fh.read()
            for ref in _ATTR.findall(html):
                ref = ref.split("#", 1)[0].split("?", 1)[0]
                if not ref or ref.startswith(_SKIP_PREFIXES):
                    continue
                target = os.path.normpath(os.path.join(root, ref))
                if not os.path.exists(target):
                    rel_page = os.path.relpath(page, site_dir)
                    missing.append(f"{rel_page}: {ref}")
    if missing:
        listing = "\n  ".join(sorted(set(missing)))
        raise SystemExit(
            f"media_check: {len(missing)} unresolved src/poster reference(s):\n  {listing}"
        )
