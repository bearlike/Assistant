"""Embedder tests — ``litellm.embedding`` mocked at the client boundary."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from mewbo_graph.wiki.embedder import Embedder
from mewbo_graph.wiki.types import Embedding


def _embedding_response(vectors: list[list[float]]) -> MagicMock:
    """Return a MagicMock shaped like a ``litellm.EmbeddingResponse``.

    The real object has a ``.data`` list of ``{"embedding": [...], "index": N}``
    dicts. The Embedder only reads ``["embedding"]`` (or ``.embedding``), so a
    minimal stand-in is enough.
    """
    resp = MagicMock()
    resp.data = [{"embedding": vec, "index": i} for i, vec in enumerate(vectors)]
    return resp


@pytest.fixture(autouse=True)
def _patch_litellm_embedding():
    """Stop ``litellm.embedding`` from making real HTTP."""
    with patch("mewbo_graph.wiki.embedder.litellm.embedding") as mock_fn:
        yield mock_fn


def _build(model: str = "openai/test", batch_size: int = 8) -> Embedder:
    return Embedder(model=model, batch_size=batch_size)


# ── embed_nodes ────────────────────────────────────────────────────────


def test_embed_nodes_returns_one_record_per_input(_patch_litellm_embedding):
    items = [("n1", "alpha"), ("n2", "beta"), ("n3", "gamma")]
    _patch_litellm_embedding.return_value = _embedding_response([
        [0.1, 0.1, 0.1, 0.1],
        [0.2, 0.2, 0.2, 0.2],
        [0.3, 0.3, 0.3, 0.3],
    ])
    emb = _build("openai/test")
    results = emb.embed_nodes(items, slug="x/y")
    assert len(results) == 3
    assert all(isinstance(r, Embedding) for r in results)
    assert [r.node_id for r in results] == ["n1", "n2", "n3"]
    assert [r.slug for r in results] == ["x/y"] * 3
    assert results[0].model == "openai/test"
    # Dim is taken from the response — no manual config required
    assert results[0].dim == 4
    _patch_litellm_embedding.assert_called_once()
    kwargs = _patch_litellm_embedding.call_args.kwargs
    assert kwargs["input"] == ["alpha", "beta", "gamma"]
    assert kwargs["model"] == "openai/test"


def test_embed_nodes_handles_variable_dim(_patch_litellm_embedding):
    """Whatever dim the provider returns flows through transparently."""
    _patch_litellm_embedding.return_value = _embedding_response([[0.0] * 3072])
    emb = _build()
    [record] = emb.embed_nodes([("only", "one")])
    assert record.dim == 3072


def test_embed_nodes_empty_short_circuits(_patch_litellm_embedding):
    emb = _build()
    assert emb.embed_nodes([]) == []
    _patch_litellm_embedding.assert_not_called()


def test_embed_nodes_batches_inputs(_patch_litellm_embedding):
    """Inputs exceeding ``batch_size`` are split across multiple calls."""
    _patch_litellm_embedding.side_effect = [
        _embedding_response([[1.0], [1.0]]),  # first batch of 2
        _embedding_response([[1.0]]),          # second batch of 1
    ]
    emb = _build(batch_size=2)
    out = emb.embed_nodes([("a", "1"), ("b", "2"), ("c", "3")])
    assert len(out) == 3
    assert _patch_litellm_embedding.call_count == 2
    assert _patch_litellm_embedding.call_args_list[0].kwargs["input"] == ["1", "2"]
    assert _patch_litellm_embedding.call_args_list[1].kwargs["input"] == ["3"]


def test_embed_query_returns_single_vector(_patch_litellm_embedding):
    _patch_litellm_embedding.return_value = _embedding_response([[0.7, 0.7]])
    emb = _build()
    out = emb.embed_query("hello")
    assert out == [0.7, 0.7]


# ── constructor wiring + model normalisation ──────────────────────────


def test_constructor_passes_proxy_base_url_and_key(_patch_litellm_embedding):
    """Embedder must point at our LiteLLM proxy, not direct provider routing."""
    _patch_litellm_embedding.return_value = _embedding_response([[0.0]])
    emb = _build("openai/my-model", batch_size=32)
    emb.embed_query("ping")
    kwargs = _patch_litellm_embedding.call_args.kwargs
    assert kwargs["model"] == "openai/my-model"
    # api_base + api_key are forwarded from LLMConfig
    assert "api_base" in kwargs
    assert "api_key" in kwargs


def test_constructor_prepends_proxy_prefix_for_bare_model_names():
    """Bare names like ``gemini-embedding-001`` get the ``openai/`` proxy prefix.

    Without this, LiteLLM dispatches the request to the provider SDK
    (e.g. Google Cloud) instead of routing through our OpenAI-compatible
    proxy at ``llm.api_base``.
    """
    emb = Embedder(model="gemini-embedding-001")
    assert emb.model == "openai/gemini-embedding-001"


def test_constructor_keeps_already_prefixed_model():
    emb = Embedder(model="openai/text-embedding-3-small")
    assert emb.model == "openai/text-embedding-3-small"
    # ``foo/bar`` is also passed through unchanged — any provider prefix counts
    emb2 = Embedder(model="azure/bar")
    assert emb2.model == "azure/bar"


# ── cosine + search ───────────────────────────────────────────────────


def test_cosine_orthogonal_is_zero():
    assert abs(Embedder.cosine([1.0, 0.0, 0.0], [0.0, 1.0, 0.0]) - 0.0) < 1e-9


def test_cosine_identical_is_one():
    a = [0.5, 0.5, 0.5]
    assert abs(Embedder.cosine(a, a) - 1.0) < 1e-9


def test_cosine_handles_zero_vector():
    assert Embedder.cosine([0.0, 0.0, 0.0], [1.0, 1.0, 1.0]) == 0.0


def test_search_returns_top_k_indices_sorted_desc():
    qvec = [1.0, 0.0]
    vectors = [
        [0.0, 1.0],   # 0
        [1.0, 0.0],   # 1
        [0.7, 0.7],   # ~0.707
        [-1.0, 0.0],  # -1
    ]
    top = Embedder.search(qvec, vectors, k=2)
    assert top == [(1, pytest.approx(1.0)), (2, pytest.approx(0.7071, rel=1e-3))]


def test_search_k_larger_than_pool_returns_all():
    top = Embedder.search([1.0, 0.0], [[1.0, 0.0], [0.0, 1.0]], k=10)
    assert len(top) == 2


def test_search_empty_returns_empty():
    assert Embedder.search([1.0], [], k=5) == []
