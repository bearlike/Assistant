#!/usr/bin/env python3
"""Inline ``@<ref>`` context expansion (submit-time preprocessor).

Expands ``@<ref>`` tokens in a user message into bounded inline context blocks
*before* the run reaches the LLM — so a project file, a directory listing, a git
diff, or a web page can be put in front of the model without uploading an
attachment or burning a ``read_file`` / ``web_url_read`` tool round-trip.

It does not parse anything itself — every ref type resolves through a renderer
that already exists in a lower layer:

==================  ==========================================================
``@path/to/file``   capped text read; binary docs → ``attachments.parse_to_markdown``
``@path/to/dir/``   shallow listing (git index for repos, ``os.scandir`` otherwise)
``@diff``/``@git-diff``  ``git diff HEAD`` (subprocess)
``@https://…``      ``attachments.parse_to_markdown`` (markitdown fetches the URL)
==================  ==========================================================

This is a reusable engine, so it lives in ``mewbo_tools`` (deps core only): both
the API and the in-process CLI invoke it at their own submit seams — an app must
not import another app, so the shared part lives one layer down.

Scoping (safety): ``@file``/``@dir`` refs resolve **only** to files in the
project's git index (via :class:`FileCatalog` — tracked + new-but-not-``.gitignore``d)
or to files explicitly attached to the session. ``.gitignore``d secrets / build
artifacts are excluded by construction. A non-git project directory falls back to
cwd-confined existing files.

Guardrails (KISS — never balloon the prompt):
- Per-ref **and** aggregate character caps; on overflow we **truncate with a
  marker, never reject** (mirrors the NL-context boundary fix in #83).
- Identical refs are deduped — the first occurrence expands, later ones stay
  literal (the content is already above).
- No recursive expansion (an expanded block is never re-scanned).
- An unresolved ``@ref`` (out-of-scope path, missing file, non-repo ``@diff``,
  unreachable URL, or a bare ``email@host``) passes through as literal text.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping

from mewbo_core.attachments import is_image, parse_to_markdown
from mewbo_core.common import get_logger

from mewbo_tools.integration.file_catalog import FileCatalog

logging = get_logger(name="tools.reference_expansion")

# Binary document extensions worth routing through markitdown; everything
# else is read as text (the common case: source, config, markdown).
_DOC_EXTS: frozenset[str] = frozenset(
    {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".odp", ".ods"}
)

# Trailing characters trimmed off a ref so a token at a sentence boundary
# (``see @notes.md.``) still resolves; the trimmed tail is kept as literal.
_TRAILING_PUNCT = ".,;:!?"


class ReferenceExpander:
    """Expand ``@<ref>`` tokens in one message body into inline context blocks.

    One atomic class: holds the resolution context (``cwd`` + :class:`FileCatalog`
    scope + the session's attachment map + size caps) and the per-message
    dedupe/budget state, with private methods describing how each ref type
    renders. Construct one per message and call :meth:`expand`.
    """

    # A token is ``@`` at start-of-string or after whitespace, followed by a
    # run of non-whitespace. The leading-boundary group keeps ``bob@host.com``
    # from matching (its ``@`` follows a word character).
    _TOKEN_RE = re.compile(r"(^|\s)@(\S+)", re.MULTILINE)

    DEFAULT_PER_REF_CHARS = 16_000
    DEFAULT_TOTAL_CHARS = 64_000
    _MAX_DIR_ENTRIES = 200

    def __init__(
        self,
        cwd: str | None,
        *,
        attachments: Mapping[str, str] | None = None,
        catalog: FileCatalog | None = None,
        per_ref_chars: int = DEFAULT_PER_REF_CHARS,
        total_chars: int = DEFAULT_TOTAL_CHARS,
    ) -> None:
        """Bind the workspace, the file-scope catalog, and the size caps.

        ``attachments`` maps a referenceable name (the attachment's display
        filename) to the absolute path to render for it (the parsed-Markdown
        sidecar when present, else the raw file). ``catalog`` is injectable for
        reuse/testing; by default one is built from ``cwd``.
        """
        self._cwd = os.path.realpath(cwd) if cwd else None
        self._catalog = catalog if catalog is not None else FileCatalog(cwd)
        self._attachments = dict(attachments or {})
        self._per_ref_chars = per_ref_chars
        self._total_chars = total_chars
        self._spent = 0
        self._seen: set[str] = set()

    # -- public --------------------------------------------------------

    def expand(self, text: str) -> str:
        """Return ``text`` with each resolvable ``@<ref>`` replaced inline.

        Unresolved or duplicate refs are left as literal text. Idempotent on
        text with no ``@`` tokens (returned unchanged).
        """
        if not text or "@" not in text:
            return text
        return self._TOKEN_RE.sub(self._replace, text)

    # -- substitution --------------------------------------------------

    def _replace(self, match: re.Match[str]) -> str:
        lead, raw = match.group(1), match.group(2)
        ref, tail = self._split_trailing(raw)
        literal = f"{lead}@{raw}"
        if not ref:
            return literal

        if ref in self._seen:
            # Deduped: content already inlined above — keep the bare token.
            return literal

        block = self._render(ref)
        if block is None:
            return literal  # unresolved → pass through untouched
        self._seen.add(ref)
        return f"{lead}{block}{tail}"

    @staticmethod
    def _split_trailing(raw: str) -> tuple[str, str]:
        """Trim trailing sentence punctuation off a ref (kept as literal tail)."""
        tail = ""
        ref = raw
        while ref and ref[-1] in _TRAILING_PUNCT:
            tail = ref[-1] + tail
            ref = ref[:-1]
        return ref, tail

    # -- rendering -----------------------------------------------------

    def _render(self, ref: str) -> str | None:
        """Dispatch a ref to its renderer; ``None`` means "leave literal"."""
        if ref in ("diff", "git-diff"):
            content = self._render_diff()
        elif "://" in ref:
            content = self._render_url(ref)
        else:
            content = self._render_path(ref)
        if content is None:
            return None
        return self._block(ref, content)

    def _render_url(self, url: str) -> str | None:
        if not (url.startswith("http://") or url.startswith("https://")):
            return None
        return parse_to_markdown(url)

    def _render_diff(self) -> str | None:
        if not self._cwd or not self._catalog.is_git_repo():
            return None
        try:
            result = subprocess.run(
                ["git", "-C", self._cwd, "diff", "HEAD"],
                capture_output=True,
                text=True,
            )
        except OSError as exc:  # git missing on PATH
            logging.warning("git diff for @diff failed: %s", exc)
            return None
        if result.returncode != 0:
            return None
        return result.stdout or "(no uncommitted changes)"

    def _render_path(self, ref: str) -> str | None:
        # Session attachment by display name (allowed even though it lives
        # outside the project tree).
        att = self._attachments.get(ref) or self._attachments.get(ref.rstrip("/"))
        if att and os.path.isfile(att):
            return self._render_file(att)

        if not self._cwd:
            return None
        is_dir_ref = ref.endswith("/")
        rel = ref.rstrip("/")
        candidate = os.path.realpath(os.path.join(self._cwd, rel))
        if candidate != self._cwd and not candidate.startswith(self._cwd + os.sep):
            return None  # path traversal outside the workspace
        if is_dir_ref or os.path.isdir(candidate):
            return self._render_dir(rel)
        if not self._catalog.contains(rel):
            return None  # not a project file (git-index / cwd scope)
        return self._render_file(candidate)

    def _render_dir(self, rel: str) -> str | None:
        """Shallow listing — from the git index for repos, scandir otherwise."""
        if self._catalog.is_git_repo():
            return self._render_dir_from_index(rel)
        path = os.path.realpath(os.path.join(self._cwd or "", rel))
        if not os.path.isdir(path):
            return None
        entries: list[str] = []
        with os.scandir(path) as it:
            for entry in sorted(it, key=lambda e: e.name):
                entries.append(entry.name + "/" if entry.is_dir() else entry.name)
        return self._format_dir(entries)

    def _render_dir_from_index(self, rel: str) -> str | None:
        norm = rel.replace(os.sep, "/").strip("/")
        prefix = "" if norm in ("", ".") else norm + "/"
        children: set[str] = set()
        for tracked in self._catalog.tracked_files():
            if prefix and not tracked.startswith(prefix):
                continue
            remainder = tracked[len(prefix):]
            if not remainder:
                continue
            head, _, rest = remainder.partition("/")
            children.add(head + "/" if rest else head)
        if not children:
            return None  # not a project directory
        return self._format_dir(sorted(children))

    def _format_dir(self, entries: list[str]) -> str:
        if not entries:
            return "(empty directory)"
        shown = entries[: self._MAX_DIR_ENTRIES]
        listing = "\n".join(shown)
        if len(entries) > self._MAX_DIR_ENTRIES:
            listing += f"\n… [{len(entries) - self._MAX_DIR_ENTRIES} more entries]"
        return listing

    def _render_file(self, path: str) -> str | None:
        _, ext = os.path.splitext(path.lower())
        if is_image(_guess_image_type(ext)):
            return "[image file — not inlined as text]"
        if ext in _DOC_EXTS:
            rendered = parse_to_markdown(path)
            return rendered if rendered is not None else "[unreadable document]"
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                # Read one char past the per-ref cap so the block knows it
                # truncated without slurping a huge file into memory.
                return fh.read(self._per_ref_chars + 1)
        except OSError as exc:
            logging.warning("reading @%s failed: %s", path, exc)
            return None

    # -- block assembly ------------------------------------------------

    def _block(self, ref: str, content: str) -> str:
        """Wrap ``content`` in a delimited, budget-capped context block."""
        remaining = max(0, self._total_chars - self._spent)
        if remaining <= 0:
            return f"\n--- @{ref} ---\n[omitted: context budget reached]\n--- end @{ref} ---\n"
        cap = min(self._per_ref_chars, remaining)
        body = content
        if len(body) > cap:
            body = body[:cap] + f"\n… [truncated to {cap} of {len(content)} chars]"
        self._spent += len(body)
        return f"\n--- @{ref} ---\n{body}\n--- end @{ref} ---\n"


def _guess_image_type(ext: str) -> str:
    """Map a file extension to an image MIME so ``is_image`` can gate it."""
    mapping = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }
    return mapping.get(ext, "")


def expand_references(
    text: str | None,
    cwd: str | None,
    *,
    attachments: Mapping[str, str] | None = None,
) -> str | None:
    """Submit-seam helper: expand ``@<ref>`` tokens in ``text`` against ``cwd``.

    Thin wrapper the apps call so the expansion seam stays a one-liner at the
    call site. ``None``/empty text and text without ``@`` are returned
    unchanged. Expansion never raises into the request path — on an unexpected
    error the original text is returned so a run is never blocked by a ref.
    """
    if not text or "@" not in text:
        return text
    try:
        return ReferenceExpander(cwd, attachments=attachments).expand(text)
    except Exception as exc:  # noqa: BLE001 - never block a run on expansion
        logging.warning("reference expansion failed, using raw query: %s", exc)
        return text
