#!/usr/bin/env python3
"""CompletionEngine — pure sigil-dispatched completion for the input area.

One atomic, UI-free class. Given the current line ``text`` and the ``cursor``
column it answers two questions:

- *Is the cursor inside a completion token?* (caret-anchored detection of the
  active ``@`` / ``/`` token — a ``/`` need not be at column 0; we complete a
  slash anywhere in the line).
- *What candidates rank best for that token?*

Ranking is deliberately tiered, not raw fuzzy (a file picker wants the file
whose **name** matches, not whichever path the fuzzy scorer likes):

- ``@`` files (``name_priority_tier``): exact basename/stem > basename prefix >
  path-segment exact > substring > subsequence (fuzzy) fallback. Ties break on
  shorter path then lexicographic.
- ``/`` commands+skills: exact > prefix > subsequence over the *name*, then a
  subsequence over the *description*. Matched characters are surfaced as
  ``match_indices`` so the widget can highlight them, and the source's
  ``argument_hint`` is carried through for the arg-hint affordance.

The engine holds its candidate sources by injection (a ``files_provider`` and a
``commands_provider``) so it stays trivially unit-testable: construct it with
plain lists/callables and assert on ``complete(text, cursor)``.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass

# --- candidate model ------------------------------------------------------

#: Sigils that open a completion token. ``!`` (bash) takes no completion but is
#: recognised so the widget can dispatch/treat the line as a shell command.
FILE_SIGIL = "@"
COMMAND_SIGIL = "/"
BASH_SIGIL = "!"

# Tier ranks for ``@`` file ranking (lower sorts first). Name-priority tiers so
# the file whose *name* matches beats a deep-path fuzzy hit.
_TIER_EXACT_NAME = 0  # query == basename or stem
_TIER_PREFIX_NAME = 1  # basename starts with query
_TIER_PATH_SEGMENT = 2  # query is an exact "/"-delimited path segment
_TIER_SUBSTRING = 3  # query is a substring of the path
_TIER_SUBSEQUENCE = 4  # query chars appear in order (fuzzy fallback)
_TIER_NONE = 5  # no match (filtered out)


@dataclass(frozen=True)
class CommandCandidate:
    """A ``/`` completion source: a command, skill, or custom/MCP command.

    ``name`` is the token *without* the leading slash (e.g. ``"help"``,
    ``"frontend:component"``). ``kind`` is a free-form label
    (``"command"`` / ``"skill"`` / ``"custom"`` / ``"mcp-prompt"``) used only
    for display. ``argument_hint`` (from custom-command frontmatter) surfaces
    inline once the command is selected.
    """

    name: str
    description: str = ""
    kind: str = "command"
    argument_hint: str = ""


@dataclass(frozen=True)
class Completion:
    """One ranked candidate the widget renders / can insert.

    ``replacement`` is the full token (sigil included for ``/``, bare path for
    ``@``) to splice in place of the active token. ``match_indices`` are
    positions *within* ``display`` to highlight. ``tier`` / ``score`` are the
    sort keys (already applied — results come pre-sorted).
    """

    display: str
    replacement: str
    kind: str = ""
    detail: str = ""
    argument_hint: str = ""
    match_indices: tuple[int, ...] = ()
    tier: int = 0
    score: float = 0.0


@dataclass(frozen=True)
class ActiveToken:
    """The completion token under the cursor.

    ``sigil`` is ``@``/``/``/``!``; ``query`` is the text between the sigil and
    the cursor (no sigil); ``start`` / ``end`` are column offsets into the line
    of the whole token *including* the sigil, so the widget can splice a
    replacement precisely.
    """

    sigil: str
    query: str
    start: int
    end: int


# --- the engine -----------------------------------------------------------


class CompletionEngine:
    """Sigil-dispatched, tiered completion over injected candidate sources."""

    #: Never flood the overlay — keep it cheap and readable.
    MAX_RESULTS = 50

    def __init__(
        self,
        *,
        files_provider: Callable[[], Sequence[str]] = lambda: [],
        commands_provider: Callable[[], Sequence[CommandCandidate]] = lambda: [],
    ) -> None:
        """Bind lazy candidate sources.

        Args:
            files_provider: Returns project files (relative POSIX paths) for
                ``@`` completion — typically a cached ``FileCatalog`` listing.
            commands_provider: Returns ``/`` candidates (commands + skills +
                custom + MCP prompts), rebuilt cheaply per call by the caller.
        """
        self._files_provider = files_provider
        self._commands_provider = commands_provider

    # -- token detection ------------------------------------------------

    @staticmethod
    def active_token(text: str, cursor: int) -> ActiveToken | None:
        """The completion token the ``cursor`` sits in, or ``None``.

        Scans left from the cursor to the nearest ``@`` / ``/`` / ``!`` that
        opens a token: the sigil must be at start-of-line or follow whitespace
        (so ``bob@host`` and a mid-word ``/`` in ``a/b`` never trigger), and no
        whitespace may sit between it and the cursor.
        """
        cursor = max(0, min(cursor, len(text)))
        before = text[:cursor]
        # Walk back to the start of the current whitespace-delimited word.
        word_start = cursor
        while word_start > 0 and not text[word_start - 1].isspace():
            word_start -= 1
        if word_start >= cursor:
            return None
        sigil = text[word_start]
        if sigil not in (FILE_SIGIL, COMMAND_SIGIL, BASH_SIGIL):
            return None
        query = before[word_start + 1 :]
        # Extend the token end across any non-space run to the right of the
        # cursor so a replacement overwrites the *whole* token, not just up to
        # the caret (caret-anchored insert).
        end = cursor
        while end < len(text) and not text[end].isspace():
            end += 1
        return ActiveToken(sigil=sigil, query=query, start=word_start, end=end)

    # -- public API -----------------------------------------------------

    def complete(self, text: str, cursor: int) -> list[Completion]:
        """Ranked completions for the token under ``cursor`` (``[]`` if none)."""
        token = self.active_token(text, cursor)
        if token is None:
            return []
        if token.sigil == FILE_SIGIL:
            return self._complete_files(token.query)
        if token.sigil == COMMAND_SIGIL:
            return self._complete_commands(token.query)
        return []  # bash sigil: no completion

    # -- @ file completion ----------------------------------------------

    def _complete_files(self, query: str) -> list[Completion]:
        try:
            files = list(self._files_provider())
        except Exception:
            return []
        q = query.casefold()
        scored: list[tuple[int, int, str, Completion]] = []
        for path in files:
            tier, indices = self._file_tier(path, q)
            if tier >= _TIER_NONE:
                continue
            scored.append(
                (
                    tier,
                    len(path),
                    path.casefold(),
                    Completion(
                        display=path,
                        replacement=f"{FILE_SIGIL}{path}",
                        kind="file",
                        match_indices=indices,
                        tier=tier,
                    ),
                )
            )
        scored.sort(key=lambda item: (item[0], item[1], item[2]))
        return [c for *_rest, c in scored[: self.MAX_RESULTS]]

    @staticmethod
    def _file_tier(path: str, q: str) -> tuple[int, tuple[int, ...]]:
        """Tier + highlight indices for ``path`` against casefolded query ``q``.

        Empty query lists everything at the prefix tier (the ``@`` overlay opens
        showing files before you type).
        """
        low = path.casefold()
        base = os.path.basename(low)
        stem = base.rsplit(".", 1)[0] if "." in base else base
        base_off = len(low) - len(base)
        if not q:
            return _TIER_PREFIX_NAME, ()
        if q == base or q == stem:
            start = base_off
            return _TIER_EXACT_NAME, tuple(range(start, start + len(q)))
        if base.startswith(q):
            return _TIER_PREFIX_NAME, tuple(range(base_off, base_off + len(q)))
        seg_start = _segment_start(low, q)
        if seg_start is not None:
            return _TIER_PATH_SEGMENT, tuple(range(seg_start, seg_start + len(q)))
        sub = low.find(q)
        if sub >= 0:
            return _TIER_SUBSTRING, tuple(range(sub, sub + len(q)))
        seq = _subsequence_indices(low, q)
        if seq is not None:
            return _TIER_SUBSEQUENCE, seq
        return _TIER_NONE, ()

    # -- / command completion -------------------------------------------

    def _complete_commands(self, query: str) -> list[Completion]:
        try:
            candidates = list(self._commands_provider())
        except Exception:
            return []
        q = query.casefold()
        scored: list[tuple[float, str, Completion]] = []
        for cand in candidates:
            rank = _command_rank(cand.name, q)
            if rank is None:
                continue
            tier, indices = rank
            scored.append(
                (
                    tier,
                    cand.name.casefold(),
                    Completion(
                        display=f"{COMMAND_SIGIL}{cand.name}",
                        replacement=f"{COMMAND_SIGIL}{cand.name}",
                        kind=cand.kind,
                        detail=cand.description,
                        argument_hint=cand.argument_hint,
                        # +1: indices are into ``display`` which has the leading
                        # slash, while we matched against the bare name.
                        match_indices=tuple(i + 1 for i in indices),
                        score=tier,
                    ),
                )
            )
        scored.sort(key=lambda item: (item[0], item[1]))
        return [c for _t, _n, c in scored[: self.MAX_RESULTS]]


# --- ranking helpers ------------------------------------------------------


def name_priority_tier(path: str, query: str) -> int:
    """Public tier for ``path`` vs ``query`` (lower = better). See ``_file_tier``.

    Exposed for tests and for any caller that wants the bare ranking without the
    full :class:`Completion` envelope.
    """
    return CompletionEngine._file_tier(path, query.casefold())[0]


def _command_rank(name: str, q: str) -> tuple[float, tuple[int, ...]] | None:
    """Rank a ``/`` candidate name against casefolded query ``q``.

    Returns ``(tier, match_indices)`` (indices into the bare name) or ``None``
    when it does not match. Lower tier sorts first: exact (0) > prefix (1) >
    subsequence (2). Empty query matches everything at the prefix tier so the
    palette/overlay can open listing all commands.
    """
    low = name.casefold()
    if not q:
        return 1.0, ()
    if low == q:
        return 0.0, tuple(range(len(q)))
    if low.startswith(q):
        return 1.0, tuple(range(len(q)))
    seq = _subsequence_indices(low, q)
    if seq is not None:
        # Tighter (shorter span) subsequence matches rank a touch higher.
        span = (seq[-1] - seq[0] + 1) if seq else len(low)
        return 2.0 + span / (len(low) + 1), seq
    return None


def _segment_start(low: str, q: str) -> int | None:
    """Start offset of the ``/``-delimited segment exactly equal to ``q``.

    Returns the absolute offset into ``low`` of the *matching* segment — not the
    first substring occurrence — so highlight indices land on the real segment
    even when ``q`` also appears inside an earlier component (e.g. ``q="app"`` in
    ``"xapp/app/file.py"`` resolves to the second segment, not ``x[app]``).
    ``None`` when no whole segment equals ``q``.
    """
    offset = 0
    for segment in low.split("/"):
        if segment == q:
            return offset
        offset += len(segment) + 1  # +1 for the "/" separator
    return None


def _subsequence_indices(haystack: str, needle: str) -> tuple[int, ...] | None:
    """Indices in ``haystack`` matching ``needle`` in order, or ``None``.

    A greedy left-to-right subsequence match (the classic fuzzy fallback). Both
    arguments are assumed already casefolded by the caller.
    """
    if not needle:
        return ()
    indices: list[int] = []
    pos = 0
    for ch in needle:
        pos = haystack.find(ch, pos)
        if pos < 0:
            return None
        indices.append(pos)
        pos += 1
    return tuple(indices)


__all__ = [
    "BASH_SIGIL",
    "COMMAND_SIGIL",
    "FILE_SIGIL",
    "ActiveToken",
    "CommandCandidate",
    "Completion",
    "CompletionEngine",
    "name_priority_tier",
]
