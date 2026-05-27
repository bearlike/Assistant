"""Ephemeral git-clone-token cache (security-sensitive, in-process only).

Private repos need an access token to ``git clone``. The wizard submits it,
but the token MUST NOT land in any persisted record, session transcript, or
event log — Mewbo state is Mongo/Langfuse-visible and treated as semi-public.
The plaintext token lives ONLY here, keyed by job id, for the lifetime of the
process.

This cache is the shared seam between the API (which stores the token at
submission time) and the relocated clone/finalize SessionTools (which read it
during the run). It carries zero dependencies so both layers import it
**down**, replacing the former ``mewbo_api.wiki.jobs`` module globals the
plugins used to reach up for.

Lifecycle: ``store`` at submission → ``peek`` (non-evicting, so clone +
finalize + refresh can each read it) → a single ``forget`` at end of finalize.
"""
from __future__ import annotations

from typing import ClassVar


class CloneTokenCache:
    """In-process registry of per-job clone tokens — never serialised."""

    _tokens: ClassVar[dict[str, str]] = {}

    @classmethod
    def store(cls, job_id: str, token: str) -> None:
        """Stash the plaintext token for *job_id* (called by the API wizard)."""
        cls._tokens[job_id] = token

    @classmethod
    def peek(cls, job_id: str) -> str | None:
        """Return the token for *job_id*, or ``None``. Non-evicting by design.

        Multiple consumers (clone, finalize, refresh) each read the token, so
        reading never removes it — :meth:`forget` is the only delete.
        """
        return cls._tokens.get(job_id)

    @classmethod
    def forget(cls, job_id: str) -> None:
        """Drop the token for *job_id* (called once at end of finalize)."""
        cls._tokens.pop(job_id, None)


__all__ = ["CloneTokenCache"]
