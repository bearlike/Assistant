"""GitHub repository card — Streamlit-native, KISS.

Assembled from `st.container(border=True)` + `st.columns` + `st.metric` +
inline markdown badges.  No custom HTML, no CSS injection, no
`unsafe_allow_html=True`.

Copy-paste contract
-------------------
Everything between ``# ── GitHubRepoCard ─`` and
``# ── end GitHubRepoCard ─`` is the reusable block.  Extract with::

    awk '/^# ── GitHubRepoCard ─/,/^# ── end GitHubRepoCard ─/' \\
        github_repo_card.py >> app.py

Call as ``GitHubRepoCard(repo_dict).render()``.

Preview standalone: ``streamlit run github_repo_card.py``.
"""

from __future__ import annotations

from typing import TypedDict

import streamlit as st

# ── GitHubRepoCard ────────────────────────────────────────────────────────


class GitHubRepo(TypedDict, total=False):
    """State for :class:`GitHubRepoCard`.  ``owner``/``name`` load-bearing."""

    owner: str
    name: str
    description: str
    stars: int
    forks: int
    language: str
    topics: list[str]
    updated_at: str
    url: str  # overrides the derived github.com link
    license_name: str


class GitHubRepoCard:
    """Compact repo card rendered from Streamlit primitives only."""

    def __init__(self, repo: GitHubRepo) -> None:
        self.repo = repo

    def render(self) -> None:
        r = self.repo
        full = f"{r.get('owner', '')}/{r.get('name', '')}".strip("/") or "unknown/repo"
        url = r.get("url") or f"https://github.com/{full}"

        with st.container(border=True):
            st.markdown(f"#### 📦 [{full}]({url})")
            if desc := r.get("description"):
                st.caption(desc)

            # Stats row via st.metric — only show fields that are present.
            fields: list[tuple[str, str]] = []
            if (stars := r.get("stars")) is not None:
                fields.append(("Stars", f"{stars:,}"))
            if (forks := r.get("forks")) is not None:
                fields.append(("Forks", f"{forks:,}"))
            if lang := r.get("language"):
                fields.append(("Language", lang))
            if fields:
                for col, (label, value) in zip(st.columns(len(fields)), fields, strict=True):
                    col.metric(label, value)

            # Topic pills via inline badge markdown (:blue-badge[text]).
            if topics := r.get("topics"):
                st.markdown(" ".join(f":blue-badge[{t}]" for t in topics[:6]))

            # Footer: license (gray badge) + updated timestamp.
            foot: list[str] = []
            if lic := r.get("license_name"):
                foot.append(f":gray-badge[{lic}]")
            if upd := r.get("updated_at"):
                foot.append(f"🕒 {upd}")
            if foot:
                st.markdown(" &nbsp;·&nbsp; ".join(foot))


# ── end GitHubRepoCard ────────────────────────────────────────────────────


# Canonical demo payloads — outside the copy region; the agent brings its own.
STREAMLIT_REPO: GitHubRepo = {
    "owner": "streamlit",
    "name": "streamlit",
    "description": "Streamlit — A faster way to build and share data apps.",
    "stars": 36125,
    "forks": 3300,
    "language": "Python",
    "topics": ["data-science", "python", "data-visualization", "streamlit"],
    "updated_at": "2024-12-01",
    "license_name": "Apache-2.0",
}
ANTHROPIC_SDK_REPO: GitHubRepo = {
    "owner": "anthropics",
    "name": "anthropic-sdk-python",
    "description": "The official Python SDK for Anthropic's API.",
    "stars": 2100,
    "forks": 310,
    "language": "Python",
    "topics": ["anthropic", "claude", "sdk"],
    "updated_at": "2024-11-22",
}


if __name__ == "__main__":
    st.set_page_config(page_title="GitHub repo card", layout="centered")
    st.title("GitHub repo card — demo")
    GitHubRepoCard(STREAMLIT_REPO).render()
    st.write("")
    GitHubRepoCard(ANTHROPIC_SDK_REPO).render()
