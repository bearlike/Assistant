"""Search engine result card — Streamlit-native, KISS.

Assembled from `st.container(border=True)` + optional `st.columns` for
the thumbnail + `st.markdown` + `st.caption` + inline `:blue-badge[]`.
No custom HTML, no CSS injection.

Copy-paste contract
-------------------
Everything between ``# ── SearchResultCard ─`` and
``# ── end SearchResultCard ─`` is the reusable block.  Extract with::

    awk '/^# ── SearchResultCard ─/,/^# ── end SearchResultCard ─/' \\
        search_result_card.py >> app.py

Call as ``SearchResultCard(result_dict).render()``.  Stack several calls
for a results list — each call produces one atomic bordered card.

Preview standalone: ``streamlit run search_result_card.py``.
"""

from __future__ import annotations

from typing import TypedDict

import streamlit as st

# ── SearchResultCard ──────────────────────────────────────────────────────


class SearchResult(TypedDict, total=False):
    """State for :class:`SearchResultCard`.  Only ``title``/``url`` required."""

    title: str
    url: str
    snippet: str
    source: str  # e.g. "github.com"
    date: str
    favicon: str  # emoji ("📦") — URLs are out of scope in KISS mode
    thumbnail_url: str
    author: str
    badge: str


class SearchResultCard:
    """One atomic search-result card built from Streamlit primitives."""

    def __init__(self, result: SearchResult) -> None:
        self.result = result

    def render(self) -> None:
        r = self.result
        title = r.get("title", "(untitled)")
        url = r.get("url", "#")
        thumb = r.get("thumbnail_url")

        with st.container(border=True):
            # Optional thumbnail splits the card into two columns; otherwise
            # everything flows in the default full-width context.
            if thumb:
                body, img = st.columns([3, 1], vertical_alignment="top", gap="medium")
                img.image(thumb, width="stretch")
            else:
                body = st  # type: ignore[assignment]

            # Meta row above the title: favicon · source · date.
            meta: list[str] = []
            if fav := r.get("favicon"):
                meta.append(fav)
            if src := r.get("source"):
                meta.append(src)
            if date := r.get("date"):
                meta.append(date)
            if meta:
                body.caption(" · ".join(meta))

            body.markdown(f"##### [{title}]({url})")

            if snippet := r.get("snippet"):
                body.write(snippet)

            footer: list[str] = []
            if badge := r.get("badge"):
                footer.append(f":blue-badge[{badge}]")
            if author := r.get("author"):
                footer.append(f"by {author}")
            if footer:
                body.markdown(" &nbsp;·&nbsp; ".join(footer))


# ── end SearchResultCard ──────────────────────────────────────────────────


# Canonical demo payloads — outside the copy region; the agent brings its own.
STREAMLIT_HIT: SearchResult = {
    "title": "Streamlit • Build data apps in minutes",
    "url": "https://streamlit.io",
    "snippet": (
        "Streamlit is an open-source Python framework for data scientists and "
        "AI/ML engineers to deliver interactive data apps."
    ),
    "source": "streamlit.io",
    "date": "Nov 21, 2024",
    "favicon": "🎈",
    "thumbnail_url": "https://picsum.photos/seed/streamlit/320/180",
    "badge": "Docs",
}
TEXT_ONLY_HIT: SearchResult = {
    "title": "How to add custom CSS to a Streamlit app",
    "url": "https://example.com/styling",
    "snippet": "A short guide — but prefer native primitives before reaching for CSS.",
    "source": "example.com",
    "date": "Oct 3, 2024",
    "favicon": "📘",
    "author": "Ada Lovelace",
    "badge": "Tutorial",
}


if __name__ == "__main__":
    st.set_page_config(page_title="Search result card", layout="centered")
    st.title("Search result card — demo")
    SearchResultCard(STREAMLIT_HIT).render()
    st.write("")
    SearchResultCard(TEXT_ONLY_HIT).render()
