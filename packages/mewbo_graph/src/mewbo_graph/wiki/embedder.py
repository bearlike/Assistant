"""Embedder — thin wrapper around ``litellm.embedding``.

LiteLLM is the project's canonical LLM client (chat completions already
route through it), so embeddings ride the same proxy plumbing for free.
This module's job is to:

1. Read the configured embedding model from ``wiki.embedding.model``.
2. Normalise it with the proxy prefix (``openai/<model>``) so LiteLLM
   sends the request to our OpenAI-compatible LiteLLM proxy instead of
   trying to dispatch directly to a provider SDK.
3. Wrap ``litellm.embedding`` so its return value materialises into our
   typed ``Embedding`` records (with slug + node_id + dim).
4. Provide ``cosine`` and ``search`` static helpers.

KISS: no third-party LangChain abstraction layer, no batching wrappers —
LiteLLM already handles batching, retries, and provider routing.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

import litellm
from mewbo_core.common import get_logger
from mewbo_core.config import get_config, get_config_value

from mewbo_graph._util import cosine as _cosine

from .types import Embedding

logging = get_logger(name="api.wiki.embedder")


@runtime_checkable
class EmbedderProtocol(Protocol):
    """The duck-typed embedder surface retriever/ingestor depend on.

    ``Embedder`` (litellm-backed) and ``_NullEmbedder`` (BM25-fallback null
    object) both satisfy this; typing against it instead of ``Any`` catches
    wiring errors at definition.
    """

    def embed_nodes(
        self, items: list[tuple[str, str]], *, slug: str = ""
    ) -> list[Embedding]:
        """Embed ``(node_id, text)`` pairs into ``Embedding`` records."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query string into a vector."""
        ...


def make_embedder() -> Embedder:
    """Construct the wiki Embedder using the configured proxy + model."""
    return Embedder()


def make_embedder_or_none() -> Embedder | None:
    """Build an Embedder, or None if it can't be constructed (BM25-only).

    The single construction path for callers that must degrade gracefully
    when no embedding backend is configured — used by insight ingestion so a
    missing proxy never fails a write.
    """
    try:
        return Embedder()
    except Exception:
        return None


class Embedder:
    """Thin facade: ``litellm.embedding`` + typed ``Embedding`` records."""

    # Project convention: chat models go through the LiteLLM proxy as
    # ``openai/<model>`` so LiteLLM uses its OpenAI-compatible client
    # against ``llm.api_base`` instead of routing to a provider SDK.
    # Same rule applies to embedding model names.
    _PROXY_PREFIX = "openai/"

    def __init__(
        self,
        *,
        model: str | None = None,
        batch_size: int | None = None,
    ) -> None:
        """Construct the Embedder from config + kwargs."""
        cfg = get_config()
        raw_model = model or get_config_value(
            "wiki", "embedding", "model", default="openai/text-embedding-3-small"
        )
        self.model = self._normalise_model(raw_model)
        self.batch_size = batch_size or int(
            get_config_value("wiki", "embedding", "batch_size", default=64)
        )
        self._api_base = cfg.llm.api_base or None
        self._api_key = cfg.llm.api_key or "missing"

    @classmethod
    def _normalise_model(cls, model: str) -> str:
        """Ensure the model name carries a provider prefix LiteLLM understands.

        Bare names like ``gemini-embedding-001`` route directly to a
        provider SDK and bypass our proxy. Prepending ``openai/`` forces
        the OpenAI-compatible path against ``api_base``.
        """
        return model if "/" in model else f"{cls._PROXY_PREFIX}{model}"

    def embed_nodes(
        self,
        items: list[tuple[str, str]],
        *,
        slug: str = "",
    ) -> list[Embedding]:
        """Embed ``(node_id, text)`` pairs and return ``Embedding`` records."""
        if not items:
            return []
        texts = [text for _, text in items]
        vectors = self._embed(texts)
        return [
            Embedding(
                slug=slug,
                node_id=nid,
                vector=list(vec),
                model=self.model,
                dim=len(vec),
            )
            for (nid, _), vec in zip(items, vectors)
        ]

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query string."""
        vectors = self._embed([text])
        return vectors[0] if vectors else []

    def _embed(self, texts: list[str]) -> list[list[float]]:
        """Issue one or more embedding calls, batching to ``batch_size``.

        LiteLLM accepts a list ``input`` and returns one vector per item,
        in order. We batch to keep request bodies under provider limits.
        """
        out: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            resp = litellm.embedding(
                model=self.model,
                input=batch,
                api_base=self._api_base,
                api_key=self._api_key,
            )
            for row in resp.data:
                # litellm returns either a dict ({'embedding': [...], 'index': N})
                # or an EmbeddingResponse pydantic object — handle both.
                vec = row["embedding"] if isinstance(row, dict) else row.embedding
                out.append(list(vec))
        return out

    # ── Vector math (provider-agnostic, no embedder state needed) ──────

    @staticmethod
    def cosine(a: list[float], b: list[float]) -> float:
        """Cosine similarity. Returns 0.0 if either vector is zero-length.

        Delegates to the shared, dependency-free ``mewbo_graph._util.cosine`` so
        the wiki vector math and the entity resolution ladder can never desync.
        """
        return _cosine(a, b)

    @staticmethod
    def search(
        qvec: list[float],
        vectors: list[list[float]],
        k: int = 10,
    ) -> list[tuple[int, float]]:
        """Return ``(index, cosine_score)`` for the top-k matches, sorted desc."""
        if not vectors:
            return []
        scored = [(i, Embedder.cosine(qvec, v)) for i, v in enumerate(vectors)]
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored[:k]
