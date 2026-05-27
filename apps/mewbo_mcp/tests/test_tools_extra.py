"""Extra unit tests for tools.py — covering the uncovered branches.

Targets the specific missing lines identified by coverage analysis:

- tools.py line 287-288, 293:  ``SessionTools._current_branch`` — RestError path
  and the no-``current_branch`` fallback that returns ``None``.
- tools.py line 309: ``SessionTools._provision_worktree`` — no ``project_id``
  in the response raises RestError.
- tools.py line 322-323: ``SessionTools._safe_agent_tree`` — RestError returns
  ``None``; non-dict result also returns ``None``.
- tools.py line 593-594: ``WikiTools._start_qa`` — invalid JSON in a ``data:``
  line is skipped without raising.
- tools.py line 613: ``WikiTools._render_qa_blocks`` — non-list ``blocks``
  returns ``("", [])``.
- tools.py line 618: ``WikiTools._render_qa_blocks`` — non-dict entry in the
  blocks list is skipped.
- tools.py lines 636-644: ``WikiTools._block_text`` — ``ul`` and ``accordion``
  block kinds.
- tools.py lines 651-657: ``WikiTools._inline_text`` — list and dict inline
  nodes (rich text).
- tools.py line 891: ``SearchTools._shape_run`` — ``error`` field present in
  payload is forwarded.

We stub ONLY the HTTP boundary (FakeRest / MockTransport), following the
repo house-style.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from mewbo_mcp import tools
from mewbo_mcp.rest import RestClient, RestError


def run(coro):
    """Run an async coroutine in a fresh event loop (repo convention)."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# SessionTools._current_branch — RestError path (lines 287-288) + no-value
# fallback (line 293)
# ---------------------------------------------------------------------------


def test_current_branch_returns_none_on_rest_error():
    """_current_branch swallows RestError and returns None (graceful fallback)."""

    def _dispatch(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "internal error"})

    transport = httpx.MockTransport(_dispatch)
    client = RestClient("http://api.test", "mk_test", transport=transport)
    result = run(tools.SessionTools(client)._current_branch("MyProject"))
    assert result is None


def test_current_branch_returns_none_when_response_missing_current_branch():
    """_current_branch returns None when the response carries no current_branch."""

    def _dispatch(req: httpx.Request) -> httpx.Response:
        # Valid 200 but the key is absent.
        return httpx.Response(200, json={"branches": ["main", "dev"]})

    transport = httpx.MockTransport(_dispatch)
    client = RestClient("http://api.test", "mk_test", transport=transport)
    result = run(tools.SessionTools(client)._current_branch("MyProject"))
    assert result is None


def test_current_branch_returns_none_when_current_branch_is_empty_string():
    """_current_branch returns None when current_branch is an empty string."""

    def _dispatch(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"current_branch": ""})

    transport = httpx.MockTransport(_dispatch)
    client = RestClient("http://api.test", "mk_test", transport=transport)
    result = run(tools.SessionTools(client)._current_branch("MyProject"))
    assert result is None


def test_current_branch_returns_none_when_response_is_not_dict():
    """_current_branch returns None when the REST response is not a dict."""

    def _dispatch(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "a", "dict"])

    transport = httpx.MockTransport(_dispatch)
    client = RestClient("http://api.test", "mk_test", transport=transport)
    result = run(tools.SessionTools(client)._current_branch("MyProject"))
    assert result is None


# ---------------------------------------------------------------------------
# SessionTools._provision_worktree — missing project_id raises RestError (line 309)
# ---------------------------------------------------------------------------


def test_provision_worktree_raises_when_no_project_id_in_response():
    """_provision_worktree raises RestError when the API omits project_id."""

    def _dispatch(req: httpx.Request) -> httpx.Response:
        # Response has no project_id.
        return httpx.Response(201, json={"branch": "feature/x"})

    transport = httpx.MockTransport(_dispatch)
    client = RestClient("http://api.test", "mk_test", transport=transport)
    with pytest.raises(RestError, match="project_id"):
        run(tools.SessionTools(client)._provision_worktree("MyProject", "feature/x", base=None))


def test_provision_worktree_raises_when_response_is_not_dict():
    """_provision_worktree raises RestError when the API returns a non-dict."""

    def _dispatch(req: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=None)

    transport = httpx.MockTransport(_dispatch)
    client = RestClient("http://api.test", "mk_test", transport=transport)
    with pytest.raises(RestError, match="project_id"):
        run(tools.SessionTools(client)._provision_worktree("MyProject", "feature/x", base="main"))


# ---------------------------------------------------------------------------
# SessionTools._safe_agent_tree — RestError returns None (lines 322-323)
# and non-dict result also returns None
# ---------------------------------------------------------------------------


def test_safe_agent_tree_returns_none_on_rest_error():
    """_safe_agent_tree returns None gracefully when the agents endpoint errors."""

    def _dispatch(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "no agents"})

    transport = httpx.MockTransport(_dispatch)
    client = RestClient("http://api.test", "mk_test", transport=transport)
    result = run(tools.SessionTools(client)._safe_agent_tree("s1"))
    assert result is None


def test_safe_agent_tree_returns_none_when_response_is_not_dict():
    """_safe_agent_tree returns None when the REST response is a non-dict value."""

    def _dispatch(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["agent1", "agent2"])

    transport = httpx.MockTransport(_dispatch)
    client = RestClient("http://api.test", "mk_test", transport=transport)
    result = run(tools.SessionTools(client)._safe_agent_tree("s1"))
    assert result is None


# ---------------------------------------------------------------------------
# SessionTools.interrupt — response without `interrupted` → "no_active_run"
# ---------------------------------------------------------------------------


def test_interrupt_returns_no_active_run_when_not_interrupted(fake_rest):
    """When the API returns no 'interrupted' field, status is 'no_active_run'."""
    fake = fake_rest.on("POST", "/api/sessions/s1/interrupt", {}, status=202)
    result = run(tools.SessionTools(fake.client()).interrupt(session_id="s1"))
    assert result["status"] == "no_active_run"
    assert result["session_id"] == "s1"


# ---------------------------------------------------------------------------
# SessionTools.send_followup — response without `enqueued` → "unknown"
# ---------------------------------------------------------------------------


def test_send_followup_returns_unknown_when_not_enqueued(fake_rest):
    """When the API response lacks 'enqueued', status is 'unknown'."""
    fake = fake_rest.on("POST", "/api/sessions/s1/message", {}, status=202)
    result = run(tools.SessionTools(fake.client()).send_followup(session_id="s1", message="hi"))
    assert result["status"] == "unknown"
    assert result["session_id"] == "s1"


# ---------------------------------------------------------------------------
# WikiTools._start_qa — invalid JSON data line is skipped (lines 593-594)
# ---------------------------------------------------------------------------


def _build_wiki_client(dispatch):
    return RestClient("http://api.test", "mk_test", transport=httpx.MockTransport(dispatch))


def test_start_qa_skips_invalid_json_data_line():
    """A data: line that is not valid JSON must not raise — it is skipped."""
    # SSE stream: first a data line with bad JSON, then the real meta frame.
    primer = ":" + (" " * 8) + "\n\n"
    bad_data = "id: 0\ndata: NOT_JSON_AT_ALL\n\n"
    meta = 'id: 1\nevent: meta\ndata: {"answerId": "ans-skip", "model": "m", "fromPageId": ""}\n\n'
    sse_body = (primer + bad_data + meta).encode()

    def _dispatch(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/v1/wiki/qa":
            return httpx.Response(
                200, content=sse_body, headers={"content-type": "text/event-stream"}
            )  # noqa: E501
        if req.method == "GET" and req.url.path == "/v1/wiki/qa/ans-skip":
            return httpx.Response(200, json={"blocks": [{"kind": "p", "text": "Good."}]})
        return httpx.Response(404, json={"message": "no route"})

    client = _build_wiki_client(_dispatch)
    result = run(
        tools.WikiTools(client, timeout_s=5.0, poll_interval_s=0.0).ask(project="p", question="q")
    )
    # Despite the bad JSON line, the meta frame was read and the answer extracted.
    assert result["answer_id"] == "ans-skip"
    assert result["answer"] == "Good."
    assert result["status"] == "complete"


# ---------------------------------------------------------------------------
# WikiTools._render_qa_blocks — non-list input returns ("", []) (line 613)
# and non-dict entry is skipped (line 618)
# ---------------------------------------------------------------------------


def test_render_qa_blocks_returns_empty_for_non_list():
    """_render_qa_blocks returns ('', []) when blocks is not a list."""
    assert tools.WikiTools._render_qa_blocks(None) == ("", [])  # type: ignore[arg-type]
    assert tools.WikiTools._render_qa_blocks("not a list") == ("", [])
    assert tools.WikiTools._render_qa_blocks(42) == ("", [])


def test_render_qa_blocks_skips_non_dict_entries():
    """Non-dict entries inside the blocks list are silently skipped."""
    blocks = [
        "just a string",
        42,
        {"kind": "p", "text": "real"},
        None,
    ]
    text, citations = tools.WikiTools._render_qa_blocks(blocks)
    assert text == "real"
    assert citations == []


# ---------------------------------------------------------------------------
# WikiTools._block_text — ul and accordion kinds (lines 636-644)
# ---------------------------------------------------------------------------


def test_block_text_ul_with_items():
    """A 'ul' block renders each item as a Markdown list entry."""
    block = {"kind": "ul", "items": ["First item", "Second item"]}
    result = tools.WikiTools._block_text(block)
    assert result == "- First item\n- Second item"


def test_block_text_ul_with_empty_items():
    """A 'ul' block with no items renders as an empty string joined from nothing."""
    block = {"kind": "ul", "items": []}
    result = tools.WikiTools._block_text(block)
    assert result == ""


def test_block_text_ul_with_non_list_items():
    """A 'ul' block where items is not a list falls through to empty string."""
    block = {"kind": "ul", "items": "not a list"}
    result = tools.WikiTools._block_text(block)
    assert result == ""


def test_block_text_accordion_with_items():
    """An 'accordion' block renders title + joined items."""
    block = {"kind": "accordion", "title": "FAQ", "items": ["Q1", "Q2"]}
    result = tools.WikiTools._block_text(block)
    assert "FAQ" in result
    assert "Q1" in result
    assert "Q2" in result


def test_block_text_accordion_with_no_title():
    """An 'accordion' block without a title still renders the items."""
    block = {"kind": "accordion", "items": ["Only item"]}
    result = tools.WikiTools._block_text(block)
    assert "Only item" in result


def test_block_text_accordion_with_no_items():
    """An 'accordion' block with no items list renders just the title."""
    block = {"kind": "accordion", "title": "Section", "items": None}
    result = tools.WikiTools._block_text(block)
    assert result == "Section"


def test_block_text_unknown_kind_returns_empty():
    """An unknown block kind returns an empty string."""
    block = {"kind": "video", "url": "https://example.com/v"}
    result = tools.WikiTools._block_text(block)
    assert result == ""


def test_block_text_heading_kinds():
    """h2 and h3 blocks render via _inline_text like 'p'."""
    assert tools.WikiTools._block_text({"kind": "h2", "text": "Section"}) == "Section"
    assert tools.WikiTools._block_text({"kind": "h3", "text": "Sub"}) == "Sub"


# ---------------------------------------------------------------------------
# WikiTools._inline_text — list and dict nodes (lines 651-657)
# ---------------------------------------------------------------------------


def test_inline_text_plain_string():
    """A plain string is returned as-is."""
    assert tools.WikiTools._inline_text("hello") == "hello"


def test_inline_text_list_concatenates_nodes():
    """A list of inline nodes is concatenated in order."""
    result = tools.WikiTools._inline_text(["hello", " ", "world"])
    assert result == "hello world"


def test_inline_text_list_with_nested_dict():
    """A list containing dict nodes extracts their text fields recursively."""
    result = tools.WikiTools._inline_text([{"text": "bold"}, " text"])
    assert result == "bold text"


def test_inline_text_dict_with_text_key():
    """A dict with a 'text' key returns that text."""
    assert tools.WikiTools._inline_text({"text": "content"}) == "content"


def test_inline_text_dict_with_text_key_as_list():
    """A dict's 'text' value can itself be a list of inline nodes."""
    result = tools.WikiTools._inline_text({"text": ["part1", " part2"]})
    assert result == "part1 part2"


def test_inline_text_dict_without_text_key():
    """A dict without a 'text' key returns an empty string."""
    assert tools.WikiTools._inline_text({"href": "https://example.com"}) == ""


def test_inline_text_none():
    """None (or any non-string/list/dict) returns an empty string."""
    assert tools.WikiTools._inline_text(None) == ""
    assert tools.WikiTools._inline_text(42) == ""


# ---------------------------------------------------------------------------
# WikiTools._render_qa_blocks — end-to-end with ul / accordion / h2
# ---------------------------------------------------------------------------


def test_render_qa_blocks_mixed_kinds():
    """Multiple block kinds are joined with double newlines; sources extracted."""
    blocks = [
        {"kind": "h2", "text": "Background"},
        {"kind": "p", "text": "Reason."},
        {"kind": "ul", "items": ["Point A", "Point B"]},
        {"kind": "sources", "items": ["src://a", "src://b"]},
        {"kind": "accordion", "title": "Details", "items": ["More info"]},
    ]
    text, citations = tools.WikiTools._render_qa_blocks(blocks)
    assert "Background" in text
    assert "Reason." in text
    assert "- Point A" in text
    assert "Details" in text
    assert citations == ["src://a", "src://b"]


def test_render_qa_blocks_sources_with_non_list_items():
    """A sources block whose items is not a list contributes no citations."""
    blocks = [{"kind": "sources", "items": "not a list"}]
    text, citations = tools.WikiTools._render_qa_blocks(blocks)
    assert citations == []
    assert text == ""


# ---------------------------------------------------------------------------
# SearchTools._shape_run — error field forwarded when present (line 891)
# ---------------------------------------------------------------------------


def test_shape_run_forwards_error_field():
    """When the payload carries an 'error' key, it is included in the result."""
    payload = {
        "run_id": "run-e",
        "answer": {"tldr": "", "bullets": [], "confidence": 0.0, "sources_count": 0},
        "results": [],
        "error": "Connector timed out.",
    }
    result = tools.SearchTools._shape_run(payload, "failed", None, "answer")
    assert result["status"] == "failed"
    assert result["error"] == "Connector timed out."


def test_shape_run_omits_error_field_when_absent():
    """When no 'error' key is in the payload, the result dict has no 'error'."""
    payload = {
        "run_id": "run-ok",
        "answer": {"tldr": "ok", "bullets": [], "confidence": 1.0, "sources_count": 0},
        "results": [],
    }
    result = tools.SearchTools._shape_run(payload, "completed", None, "answer")
    assert "error" not in result


def test_shape_run_includes_workspace_name_when_provided():
    """workspace_name is included only when explicitly passed (not None)."""
    payload = {
        "run_id": "run-ws",
        "answer": {"tldr": "t", "bullets": [], "confidence": 0.5, "sources_count": 1},
        "results": [],
    }
    with_name = tools.SearchTools._shape_run(payload, "completed", "Engineering", "answer")
    assert with_name["workspace_name"] == "Engineering"
    without_name = tools.SearchTools._shape_run(payload, "completed", None, "answer")
    assert "workspace_name" not in without_name


# ---------------------------------------------------------------------------
# SearchTools._shape_result — full detail with insight but no refs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("detail", ["answer", "full"])
def test_shape_result_detail_tiers(detail):
    """answer tier omits snippet; full tier includes it."""
    r = {
        "id": "r1",
        "source": "notion",
        "kind": "doc",
        "title": "T",
        "url": "https://n/r1",
        "relevance": 0.9,
        "snippet": "some text",
        "author": "Alice",
        "timestamp": "2026-06-01",
    }
    result = tools.SearchTools._shape_result(r, detail)
    if detail == "answer":
        assert "snippet" not in result
    else:
        assert result["snippet"] == "some text"
        assert result["author"] == "Alice"


def test_shape_result_full_with_insight_dict():
    """Full detail with an insight dict shapes label+body."""
    r = {
        "id": "r2",
        "source": "slack",
        "kind": "thread",
        "title": "T",
        "url": "u",
        "relevance": 0.5,
        "snippet": "s",
        "insight": {"label": "Key", "body": "Critical."},
    }
    result = tools.SearchTools._shape_result(r, "full")
    assert result["insight"] == {"label": "Key", "body": "Critical."}


def test_shape_result_full_with_non_dict_insight_omits_insight():
    """A non-dict insight (e.g. string) is not included in the full result."""
    r = {
        "id": "r3",
        "source": "notion",
        "kind": "doc",
        "title": "T",
        "url": "u",
        "relevance": 0.5,
        "snippet": "s",
        "insight": "just a string",
    }
    result = tools.SearchTools._shape_result(r, "full")
    assert "insight" not in result


# ---------------------------------------------------------------------------
# Coercion primitives — edge cases for the module-level helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ({}, {}),
        ({"a": 1}, {"a": 1}),
        (None, {}),
        ([], {}),
        ("string", {}),
        (42, {}),
    ],
)
def test_as_dict_coercion(value, expected):
    """_as_dict returns the value when it is a dict, else empty dict."""
    assert tools._as_dict(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ([], []),
        ([1, 2], [1, 2]),
        (None, []),
        ({}, []),
        ("string", []),
    ],
)
def test_as_list_coercion(value, expected):
    """_as_list returns the value when it is a list, else empty list."""
    assert tools._as_list(value) == expected


@pytest.mark.parametrize(
    "payload,key,expected",
    [
        ({"items": [{"a": 1}, {"b": 2}]}, "items", [{"a": 1}, {"b": 2}]),
        # Non-dict items in the list are dropped.
        ({"items": [{"a": 1}, "not a dict", 42]}, "items", [{"a": 1}]),
        # Missing key → empty list.
        ({}, "items", []),
        # Non-list value → empty list.
        ({"items": "not a list"}, "items", []),
        # Non-dict payload → empty list.
        (None, "items", []),
    ],
)
def test_dict_list_coercion(payload, key, expected):
    """_dict_list extracts key as list of dicts, dropping malformed entries."""
    assert tools._dict_list(payload, key) == expected


# ---------------------------------------------------------------------------
# SessionTools._auto_branch_name — slug derivation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "seed,expected_prefix",
    [
        ("Add authentication", "mewbo/add-authentication"),
        ("  spaces  and  CAPS  ", "mewbo/spaces-and-caps"),
        ("Special!@#chars", "mewbo/special-chars"),
        # Very long seed is truncated at 32 chars.
        ("a" * 100, "mewbo/" + "a" * 32),
        # Empty-ish seed falls back to 'session'.
        ("!@#$%", "mewbo/session"),
    ],
)
def test_auto_branch_name(seed, expected_prefix):
    """_auto_branch_name produces a mewbo/<slug> name from any seed."""
    result = tools.SessionTools._auto_branch_name(seed)
    assert result == expected_prefix, f"Got: {result!r}"


# ---------------------------------------------------------------------------
# SessionTools._truncate — text truncation at TURN_TEXT_TRUNC boundary
# ---------------------------------------------------------------------------


def test_truncate_within_limit():
    """Text within the limit is returned unchanged."""
    short = "x" * 100
    assert tools.SessionTools._truncate(short) == short


def test_truncate_at_exact_limit():
    """Text at exactly the limit is returned unchanged (no ellipsis)."""
    exact = "y" * tools.SessionTools.TURN_TEXT_TRUNC
    result = tools.SessionTools._truncate(exact)
    assert result == exact
    assert not result.endswith("…")


def test_truncate_beyond_limit():
    """Text beyond the limit is cut and appended with an ellipsis."""
    long_text = "z" * (tools.SessionTools.TURN_TEXT_TRUNC + 50)
    result = tools.SessionTools._truncate(long_text)
    assert result.endswith("…")
    assert len(result) == tools.SessionTools.TURN_TEXT_TRUNC + 1  # limit chars + ellipsis


# ---------------------------------------------------------------------------
# SessionTools._overview — empty turns edge-case
# ---------------------------------------------------------------------------


def test_overview_with_no_turns():
    """_overview works when the session has no turns yet (first load)."""
    result = tools.SessionTools._overview("s-empty", [], running=True)
    assert result["session_id"] == "s-empty"
    assert result["title"] == ""
    assert result["summary"] == ""
    assert result["turn_count"] == 0
    assert result["running"] is True
    assert result["status"] == "running"
