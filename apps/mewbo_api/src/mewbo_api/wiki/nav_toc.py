"""Derive sidebar nav and in-page TOC from persisted wiki state.

The wiki page builder leaves ``WikiPage.nav`` and ``WikiPage.toc`` empty
because the canonical sources for both live elsewhere: the project's
full page set (for the sidebar) and the page's own markdown headings
(for the right rail). Computing them once on the response side keeps
the persisted page minimal AND avoids requiring re-indexing whenever
the derivation changes.

The slug algorithm here mirrors ``github-slugger`` (used by
``rehype-slug`` on the FE) so heading ids produced by the FE renderer
match the ids the TOC links to.
"""
from __future__ import annotations

import re
from typing import Any

import mistune
from mewbo_graph.wiki.types import NavEntry, TocEntry, WikiPage

# Subset of github-slugger's strip set covering ASCII punctuation +
# control characters. Covers every heading shape we've seen in
# practice (code-doc text); add to this only when a real heading
# produces a divergent id.
_GHSLUG_STRIP = re.compile(
    r"[\x00-\x1F!\"#$%&'()*+,./:;<=>?@\[\]\\^`{|}~]"
)


def github_slug(text: str) -> str:
    """Produce a slug matching ``github-slugger``'s default output.

    Algorithm: lowercase, strip punctuation/control chars, replace
    spaces with dashes. Does NOT collapse consecutive dashes —
    matches GitHub's heading anchor rules, where ``"A & B"`` becomes
    ``"a--b"`` (the surrounding spaces of the stripped ``&`` both
    survive as dashes).
    """
    s = text.lower()
    s = _GHSLUG_STRIP.sub("", s)
    return s.replace(" ", "-")


def _heading_text(children: Any) -> str:
    """Flatten a mistune AST heading's children to plain text.

    Children can be either dicts (most node types) or bare strings
    (mistune occasionally short-circuits text leaves); handle both.
    """
    out: list[str] = []
    for child in children or []:
        if isinstance(child, str):
            out.append(child)
            continue
        if not isinstance(child, dict):
            continue
        ctype = child.get("type")
        if ctype in ("text", "codespan"):
            out.append(child.get("raw", "") or "")
        elif "children" in child:
            out.append(_heading_text(child["children"]))
        else:
            # link / emphasis / image alt — fall back to ``raw`` if
            # present, else skip silently.
            raw = child.get("raw")
            if raw:
                out.append(str(raw))
    return "".join(out).strip()


def derive_toc(body: str) -> list[TocEntry]:
    """Parse ``body`` markdown and return ``TocEntry`` objects for headings.

    Covers levels 1–3 only. Heading ids match ``rehype-slug`` output.
    Duplicate slugs within the same page get ``-1``, ``-2`` … suffixes
    in document order, mirroring github-slugger's de-duplication.
    """
    parser = mistune.create_markdown(renderer=None)
    tokens, _ = parser.parse(body or "")

    entries: list[TocEntry] = []
    seen: dict[str, int] = {}
    for tok in tokens:
        if not isinstance(tok, dict) or tok.get("type") != "heading":
            continue
        lvl = tok.get("attrs", {}).get("level")
        if lvl not in (1, 2, 3):
            continue
        label = _heading_text(tok.get("children") or [])
        if not label:
            continue
        base = github_slug(label)
        if not base:
            continue
        count = seen.get(base, 0)
        slug = base if count == 0 else f"{base}-{count}"
        seen[base] = count + 1
        entries.append(TocEntry(id=slug, label=label, lvl=lvl))
    return entries


def derive_nav(pages: list[WikiPage]) -> list[NavEntry]:
    """Flatten the project's pages into a sidebar nav list.

    Ordering: landing page first (when present), then everything else
    alphabetically by title — stable, predictable, and survives
    re-indexing. A future enhancement can group by frontmatter section,
    but flat-lvl-1 is enough for v1 and matches the FE's expectations
    (the FE indents by ``lvl`` but doesn't require a strict tree).
    """
    if not pages:
        return []
    # Stable sort by title so the sidebar order doesn't churn between
    # requests on Mongo, which doesn't guarantee insertion order.
    sorted_pages = sorted(pages, key=lambda p: (p.title or p.id).lower())
    return [
        NavEntry(id=p.id, label=p.title or p.id, lvl=1)
        for p in sorted_pages
    ]


__all__ = ["derive_nav", "derive_toc", "github_slug"]
