"""Durable repository-credential store — the per-slug source of truth.

Atomic class: all state lives in the injected ``WikiStoreBase``; this class is
the single read/write chokepoint with an identity ``_encode``/``_decode`` seam
so encryption-at-rest is a one-line swap later. Keyed by **slug** (durable
project identity) — NOT job_id — so re-index reaches the same credential the
process that died never saw.

SECURITY: credentials are plaintext-at-rest in their own isolated store
(mode 0600 on the JSON driver, dedicated collection on Mongo) but MUST be
redacted in-flight — never log ``RepoCredential.value``, never echo it into an
SSE event, a session transcript, or a tool result.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mewbo_core.common import get_logger

from mewbo_graph.wiki.types import RepoCredential

if TYPE_CHECKING:
    from mewbo_graph.wiki.store import WikiStoreBase

logging = get_logger(name="mewbo_graph.wiki.credentials")


class CredentialStore:
    """Static façade over the store's credential primitives, keyed by slug."""

    @staticmethod
    def _encode(cred: RepoCredential) -> dict[str, Any]:
        """Serialise a credential for at-rest storage. Identity today.

        The ONE place a future cipher lands: encrypt the returned blob here and
        decrypt in :meth:`_decode`; nothing else in the codebase changes.
        """
        return cred.model_dump(mode="json")

    @staticmethod
    def _decode(blob: dict[str, Any]) -> RepoCredential | None:
        """Deserialise an at-rest blob back into a credential (None if malformed)."""
        try:
            return RepoCredential.model_validate(blob)
        except Exception:
            logging.warning("skipping malformed credential blob")
            return None

    @classmethod
    def save(cls, store: WikiStoreBase, slug: str, cred: RepoCredential) -> None:
        """Persist *cred* for *slug* (overwrites any prior credential)."""
        store.save_credentials(slug, cls._encode(cred))

    @classmethod
    def load(cls, store: WikiStoreBase, slug: str) -> RepoCredential | None:
        """Return the durable credential for *slug*, or None if absent/malformed."""
        blob = store.get_credentials(slug)
        if blob is None:
            return None
        return cls._decode(blob)

    @staticmethod
    def delete(store: WikiStoreBase, slug: str) -> bool:
        """Delete *slug*'s credential; return True if one was removed."""
        return store.delete_credentials(slug)


__all__ = ["CredentialStore"]
