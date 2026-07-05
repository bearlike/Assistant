"""Emit HTML redirect stubs for pages that moved in the per-client tab split.

Old flat-page URLs stay reachable: each entry writes a static meta-refresh
stub into the built site at the old page's URL, pointing at the new home.
Local hook (no plugin dependency); runs after the site is rendered.
"""

from __future__ import annotations

import os
import posixpath

# old doc path (as it was under docs/) -> new doc path (current file under docs/)
REDIRECT_MAPS = {
    "clients-cli.md": "terminal/index.md",
    "clients-web-api.md": "web/index.md",
    "developer-guide.md": "api/building-a-client.md",
    "features-structured-outputs.md": "api/structured-outputs.md",
    "ci-agent-pickup.md": "api/automation.md",
    "features-web-ide.md": "web/ide.md",
    "features-widgets.md": "web/widgets.md",
}

_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Redirecting...</title>
<link rel="canonical" href="{url}">
<script>var a=window.location.hash.substr(1);location.href="{url}"+(a?"#"+a:"")</script>
<meta http-equiv="refresh" content="0; url={url}">
</head>
<body>This page moved to <a href="{url}">a new home</a>.</body>
</html>
"""


def _url_dir(doc_path: str) -> str:
    """Directory-style site URL for a docs/ markdown path ('' = site root)."""
    base, _ = posixpath.splitext(doc_path)
    if posixpath.basename(base) == "index":
        base = posixpath.dirname(base)
    return base


def on_post_build(config):
    """Write a redirect stub for every moved page into the built site."""
    site_dir = config["site_dir"]
    for old, new in REDIRECT_MAPS.items():
        old_dir = _url_dir(old)
        new_dir = _url_dir(new)
        target = posixpath.relpath(new_dir or ".", old_dir or ".") + "/"
        stub = os.path.join(site_dir, old_dir, "index.html")
        os.makedirs(os.path.dirname(stub), exist_ok=True)
        with open(stub, "w", encoding="utf-8") as fh:
            fh.write(_HTML.format(url=target))
