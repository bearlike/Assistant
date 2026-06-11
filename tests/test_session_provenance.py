"""Tests for session provenance classification."""

import pytest
from mewbo_core.session_provenance import SessionOrigin, TraceProvenance
from mewbo_core.session_runtime import SessionRuntime
from mewbo_core.session_store import SessionStore


@pytest.mark.parametrize(
    ("tags", "context", "expected"),
    [
        # Tag prefix is the primary signal (covers old wiki jobs w/ empty context).
        (["wiki:job:abc"], {}, SessionOrigin.WIKI),
        (["wiki:qa:abc"], {}, SessionOrigin.WIKI),
        (["agentic_search:scg:abc"], {}, SessionOrigin.SEARCH),
        (["agentic_search:run:abc"], {}, SessionOrigin.SEARCH),
        (["nextcloud-talk:room:tok"], {}, SessionOrigin.CHANNEL),
        (["email:thread:chan:root"], {}, SessionOrigin.CHANNEL),
        # Realtime structured/draft surfaces (#78).
        (["structured:run"], {}, SessionOrigin.STRUCTURED),
        (["structured:fast"], {}, SessionOrigin.STRUCTURED),
        (["draft:stream"], {}, SessionOrigin.DRAFT),
        # Context fallback when no tag is present.
        ([], {"client_capabilities": ["wiki"]}, SessionOrigin.WIKI),
        ([], {"client_capabilities": ["scg"]}, SessionOrigin.SEARCH),
        ([], {"source_platform": "nextcloud-talk"}, SessionOrigin.CHANNEL),
        # Manual console sessions and the empty default.
        ([], {"client_capabilities": ["stlite"]}, SessionOrigin.USER),
        ([], {}, SessionOrigin.USER),
        # Tag wins over a conflicting context capability.
        (["wiki:job:abc"], {"client_capabilities": ["stlite"]}, SessionOrigin.WIKI),
    ],
)
def test_classify(tags, context, expected):
    """classify maps tags + context to the right coarse origin."""
    assert SessionOrigin.classify(tags, context) == expected


def test_tags_for_session_round_trip(tmp_path):
    """tags_for_session is the reverse of resolve_tag and returns every match."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.tag_session(session_id, "wiki:job:1")
    store.tag_session(session_id, "extra-label")
    store.tag_session(store.create_session(), "other:room:x")
    assert sorted(store.tags_for_session(session_id)) == ["extra-label", "wiki:job:1"]


def test_summarize_session_sets_origin(tmp_path):
    """summarize_session classifies a tagged wiki session and a plain one."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)

    wiki_id = runtime.resolve_session(session_tag="wiki:job:42")
    store.append_event(wiki_id, {"type": "user", "payload": {"text": "index"}})
    assert runtime.summarize_session(wiki_id)["origin"] == "wiki"

    user_id = runtime.resolve_session()
    store.append_event(user_id, {"type": "user", "payload": {"text": "hi"}})
    assert runtime.summarize_session(user_id)["origin"] == "user"


def test_summarize_session_surfaces_capabilities_and_workspace(tmp_path):
    """The summary lifts advertised caps + workspace to top-level (transparency).

    The landing page shows WHAT a session was scoped to without parsing the
    context shape. A plain session reports an empty list / no workspace.
    """
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)

    scoped = runtime.resolve_session()
    store.append_event(
        scoped,
        {
            "type": "context",
            "payload": {
                "client_capabilities": ["scg"],
                "structured_workspace": "ws-7870f5ab",
            },
        },
    )
    store.append_event(scoped, {"type": "user", "payload": {"text": "curate"}})
    summary = runtime.summarize_session(scoped)
    assert summary["capabilities"] == ["scg"]
    assert summary["workspace"] == "ws-7870f5ab"

    plain = runtime.resolve_session()
    store.append_event(plain, {"type": "user", "payload": {"text": "hi"}})
    plain_summary = runtime.summarize_session(plain)
    assert plain_summary["capabilities"] == []
    assert plain_summary["workspace"] is None


# -- TraceProvenance.derive -------------------------------------------------


@pytest.mark.parametrize(
    ("tags", "context", "surface", "product", "session_type", "expect_meta"),
    [
        # Wiki indexing vs Q&A — product/type/id from the tag.
        (["wiki:job:abc"], {}, "api", "wiki", "wiki_index", {"wiki_id": "abc"}),
        (["wiki:qa:xyz"], {}, "console", "wiki", "wiki_qa", {"wiki_id": "xyz"}),
        # Agentic search run vs SCG map (#77): the three search-product session
        # types stay distinct — a RUN (agentic_search:run), the legacy scg tag,
        # and the MAP-source job (scg:map). A run must NOT read as a map.
        (["agentic_search:run:r1"], {}, "api", "search", "search_run", {"search_id": "r1"}),
        (["agentic_search:scg:s1"], {}, "api", "search", "scg_map", {"search_id": "s1"}),
        (["scg:map:j1"], {"client_capabilities": ["scg"]}, "api", "search", "scg_map",
         {"search_id": "j1"}),
        # Structured query, distinguished from chat by ``structured_workspace``;
        # surface separates an MCP-invoked call from a console one.
        ([], {"structured_workspace": "ws"}, "mcp", "agent", "structured", {"workspace": "ws"}),
        ([], {}, "cli", "agent", "chat", {}),
        # Realtime structured/draft tags drive product + session_type (#78); the
        # tag's session_type wins over the context-derived "structured".
        (["structured:run"], {}, "api", "structured", "structured_run", {}),
        (["structured:fast"], {"structured_workspace": "ws"}, "api", "structured",
         "structured_fast", {"workspace": "ws"}),
        (["draft:stream"], {}, "console", "draft", "draft_stream", {}),
    ],
)
def test_derive_product_and_type(tags, context, surface, product, session_type, expect_meta):
    """derive maps the most-specific tag/context signal to product + type + ids."""
    prov = TraceProvenance.derive(tags=tags, context=context, surface=surface)
    assert prov.product == product
    assert prov.session_type == session_type
    assert prov.surface == surface
    for key, value in expect_meta.items():
        assert prov.metadata[key] == value
    # The coarse filter chips are always present and mirror metadata.
    assert f"product:{product}" in prov.tags
    assert f"session_type:{session_type}" in prov.tags
    assert f"surface:{surface}" in prov.tags
    assert f"origin:{prov.origin.value}" in prov.tags


def test_derive_channel_unpacks_platform_and_ids():
    """A channel thread tag yields platform + channel + thread, surface = platform."""
    prov = TraceProvenance.derive(
        tags=["nextcloud-talk:thread:room123:thr456"],
        context={"source_platform": "nextcloud-talk"},
        surface="nextcloud-talk",
    )
    assert prov.product == "channel"
    assert prov.session_type == "channel_msg"
    assert prov.surface == "nextcloud-talk"
    assert prov.metadata["platform"] == "nextcloud-talk"
    assert prov.metadata["channel_id"] == "room123"
    assert prov.metadata["thread_id"] == "thr456"
    # High-cardinality ids stay out of the tag chips.
    assert not any(tag.startswith("channel_id:") for tag in prov.tags)


def test_derive_vcs_pickup_and_managed_worktree():
    """A vcs tag drives product; a managed project becomes a worktree, not a chip."""
    prov = TraceProvenance.derive(
        tags=["vcs:bearlike/Assistant:pull_request:72"],
        context={"project": "managed:deadbeef", "branch": "grove/x", "repo": "Assistant"},
        surface=None,
    )
    assert prov.product == "vcs"
    assert prov.session_type == "vcs_pickup"
    # Surface inferred from the vcs tag when nothing stamped it.
    assert prov.surface == "vcs"
    assert prov.metadata["vcs_kind"] == "pull_request"
    assert prov.metadata["vcs_number"] == "72"
    # The tag's owner/repo wins over the context's bare repo.
    assert prov.metadata["repo"] == "bearlike/Assistant"
    assert "repo:bearlike/Assistant" in prov.tags
    assert "branch:grove/x" in prov.tags
    # ``managed:<uuid>`` is a worktree, never a project chip.
    assert prov.metadata["worktree"] == "deadbeef"
    assert not any(tag.startswith("project:") for tag in prov.tags)


def test_derive_named_project_and_capabilities():
    """A named project is a chip; capabilities ride in metadata only."""
    prov = TraceProvenance.derive(
        tags=[],
        context={"project": "homelab", "client_capabilities": ["wiki", "scg"], "model": "m"},
        surface="api",
    )
    assert prov.metadata["project"] == "homelab"
    assert "project:homelab" in prov.tags
    assert "model:m" in prov.tags
    assert prov.metadata["capabilities"] == "wiki,scg"
    assert not any(tag.startswith("capabilities:") for tag in prov.tags)
    assert "worktree" not in prov.metadata


@pytest.mark.parametrize(
    ("surface", "context", "tags", "expected"),
    [
        # Explicit stamp wins over a context platform.
        ("cli", {"source_platform": "email"}, [], "cli"),
        # Context platform fills in when nothing was stamped.
        (None, {"source_platform": "email"}, [], "email"),
        # A vcs tag infers the forge when no other surface signal exists.
        (None, {}, ["vcs:o/r:issue:1"], "vcs"),
        # Truly unknown stays visible as a filter, not silently dropped.
        (None, {}, [], "unknown"),
    ],
)
def test_derive_surface_precedence(surface, context, tags, expected):
    """Surface resolves explicit > context platform > vcs-forge > unknown."""
    prov = TraceProvenance.derive(tags=tags, context=context, surface=surface)
    assert prov.surface == expected


def test_derive_custom_label_does_not_mask_product():
    """An unrecognised manual label is skipped; the real product tag still wins."""
    prov = TraceProvenance.derive(tags=["my-label", "wiki:qa:xyz"], context={}, surface="console")
    assert prov.product == "wiki"
    assert prov.session_type == "wiki_qa"
