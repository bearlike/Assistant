#!/usr/bin/env python3
"""PromptHistory — persistent prompt history + reverse search.

One atomic class backing the input area's history: most-recent-last, capped,
persisted line-per-entry to ``~/.mewbo/cli_history`` (so it survives restarts,
matching the file-contract philosophy of the rewrite). ``ctrl+r`` reverse search
walks matches newest→oldest, like bash/zsh.

It is pure/­testable: point ``path`` at a tmp file and drive ``append`` /
``search`` / cursor navigation directly. All disk I/O is best-effort — a
read/write failure degrades to in-memory only, never raising into the prompt.
"""

from __future__ import annotations

from pathlib import Path

from mewbo_core.common import get_logger

logging = get_logger(name="cli.prompt_history")

_DEFAULT_PATH = Path.home() / ".mewbo" / "cli_history"
_DEFAULT_CAP = 1000


class PromptHistory:
    """Capped, persisted prompt history with newest→oldest reverse search."""

    def __init__(self, *, path: Path | None = None, cap: int = _DEFAULT_CAP) -> None:
        """Load history from ``path`` (defaults to ``~/.mewbo/cli_history``)."""
        self._path = path if path is not None else _DEFAULT_PATH
        self._cap = max(1, cap)
        self._entries: list[str] = self._read()

    # -- persistence ----------------------------------------------------

    def _read(self) -> list[str]:
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError:
            return []
        return [line for line in text.splitlines() if line.strip()][-self._cap :]

    def _write(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text("\n".join(self._entries) + "\n", encoding="utf-8")
        except OSError as exc:  # pragma: no cover - defensive
            logging.debug("failed to persist prompt history to {}: {}", self._path, exc)

    # -- mutation -------------------------------------------------------

    def append(self, line: str) -> None:
        """Record a submitted ``line`` (skips blanks + immediate duplicates)."""
        line = line.rstrip("\n")
        if not line.strip():
            return
        if self._entries and self._entries[-1] == line:
            return
        self._entries.append(line)
        if len(self._entries) > self._cap:
            self._entries = self._entries[-self._cap :]
        self._write()

    # -- read -----------------------------------------------------------

    def entries(self) -> list[str]:
        """All entries, oldest→newest (a copy)."""
        return list(self._entries)

    def recent(self, limit: int | None = None) -> list[str]:
        """Entries newest→oldest, optionally capped to ``limit``."""
        rev = list(reversed(self._entries))
        return rev[:limit] if limit is not None else rev

    def search(self, query: str, *, limit: int | None = None) -> list[str]:
        """History lines containing ``query`` (casefold), newest→oldest, de-duped.

        Empty ``query`` returns the recent list (the ``ctrl+r`` overlay opens
        showing history before you type).
        """
        q = query.casefold()
        seen: set[str] = set()
        out: list[str] = []
        for line in reversed(self._entries):
            if q and q not in line.casefold():
                continue
            if line in seen:
                continue
            seen.add(line)
            out.append(line)
            if limit is not None and len(out) >= limit:
                break
        return out

    def best_match(self, query: str) -> str | None:
        """The single most-recent line matching ``query`` (``None`` if none)."""
        matches = self.search(query, limit=1)
        return matches[0] if matches else None


__all__ = ["PromptHistory"]
