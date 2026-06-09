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
            return httpx.Response(
                200, json={"status": "complete", "blocks": [{"kind": "p", "text": "Good."}]}
            )
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
    result = tools.SessionTools._overview("s-empty", [], {"running": True})
    assert result["session_id"] == "s-empty"
    assert result["title"] == ""
    assert result["summary"] == ""
    assert result["turn_count"] == 0
    assert result["running"] is True
    assert result["status"] == "running"


def test_overview_prefers_authoritative_status_and_title():
    """_overview reads the API's status/title from the events meta, not the timeline."""
    result = tools.SessionTools._overview(
        "s-meta", [], {"running": False, "status": "completed", "title": "My Task"}
    )
    assert result["status"] == "completed"
    assert result["title"] == "My Task"


# ---------------------------------------------------------------------------
# Fix 1 — WikiTools._block_text: table / diagram / hr block kinds
# ---------------------------------------------------------------------------


def test_block_text_table_renders_markdown_table():
    """A 'table' block renders as a Markdown table with header + separator + rows."""
    block = {
        "kind": "table",
        "head": ["Name", "Status"],
        "rows": [
            ["Alice", "active"],
            ["Bob", "inactive"],
        ],
    }
    result = tools.WikiTools._block_text(block)
    # Header row must be present
    assert "| Name |" in result
    assert "| Status |" in result
    # Separator row
    assert "---" in result
    # Data rows
    assert "Alice" in result
    assert "inactive" in result


def test_block_text_table_with_inline_cell():
    """Table cells that are inline-node dicts are rendered via _inline_text."""
    block = {
        "kind": "table",
        "head": ["Pkg"],
        "rows": [[{"text": "mewbo-core"}]],
    }
    result = tools.WikiTools._block_text(block)
    assert "mewbo-core" in result


def test_block_text_diagram_renders_placeholder():
    """A 'diagram' block renders a terse mermaid placeholder referencing the id."""
    block = {"kind": "diagram", "id": "graph-overview"}
    result = tools.WikiTools._block_text(block)
    assert "graph-overview" in result


def test_block_text_hr_renders_horizontal_rule():
    """An 'hr' block renders as '---'."""
    block = {"kind": "hr"}
    result = tools.WikiTools._block_text(block)
    assert result == "---"


# ---------------------------------------------------------------------------
# Fix 1 — WikiTools._inline_text: code / link / src inline node shapes
# ---------------------------------------------------------------------------


def test_inline_text_code_span():
    """A {'code': '...'} dict renders as a backtick code span."""
    result = tools.WikiTools._inline_text({"code": "my_func()"})
    assert result == "`my_func()`"


def test_inline_text_link_node():
    """A {'link': '...', 'text': '...'} dict renders as [text](link)."""
    result = tools.WikiTools._inline_text({"link": "https://example.com", "text": "Example"})
    assert result == "[Example](https://example.com)"


def test_inline_text_src_node_with_lines():
    """A {'kind': 'src', 'path': '...', 'lines': '...'} renders as path:lines."""
    result = tools.WikiTools._inline_text({"kind": "src", "path": "auth.py", "lines": "10-20"})
    assert result == "auth.py:10-20"


def test_inline_text_src_node_without_lines():
    """A {'kind': 'src', 'path': '...'} without lines renders as just the path."""
    result = tools.WikiTools._inline_text({"kind": "src", "path": "auth.py"})
    assert result == "auth.py"


# ---------------------------------------------------------------------------
# Fix 1 — _render_qa_blocks + ask end-to-end: table block in answer
# ---------------------------------------------------------------------------


def test_render_qa_blocks_mixed_kinds_with_table():
    """A table block is rendered (not dropped) when present among other blocks."""
    blocks = [
        {"kind": "h2", "text": "Comparison"},
        {"kind": "table", "head": ["Feature", "Status"], "rows": [["Auth", "done"]]},
        {"kind": "sources", "items": ["src://x"]},
    ]
    text, citations = tools.WikiTools._render_qa_blocks(blocks)
    assert "Comparison" in text
    # The table row content must appear in the answer — the core regression fix
    assert "Auth" in text
    assert "done" in text
    assert citations == ["src://x"]


def test_ask_wiki_answer_contains_table_rows(fake_rest):
    """End-to-end: ask returns an answer string containing the table cells."""
    primer = ":" + (" " * 8) + "\n\n"
    meta = (
        "id: 0\nevent: meta\n"
        'data: {"answerId": "ans-tbl", "model": "m", "fromPageId": ""}\n\n'
    )
    sse_body = (primer + meta).encode()

    def _dispatch(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/v1/wiki/qa":
            return httpx.Response(
                200, content=sse_body, headers={"content-type": "text/event-stream"}
            )
        if req.method == "GET" and req.url.path == "/v1/wiki/qa/ans-tbl":
            return httpx.Response(
                200,
                json={
                    "status": "complete",
                    "blocks": [
                        {"kind": "p", "text": "See the table below."},
                        {
                            "kind": "table",
                            "head": ["Component", "Coverage"],
                            "rows": [["core", "92%"], ["tools", "88%"]],
                        },
                        {"kind": "sources", "items": ["src://cov"]},
                    ],
                },
            )
        return httpx.Response(404)

    client = _build_wiki_client(_dispatch)
    result = run(
        tools.WikiTools(client, timeout_s=5.0, poll_interval_s=0.0).ask(project="p", question="q")
    )
    assert result["status"] == "complete"
    # The table rows must be in the answer, not silently dropped
    assert "Component" in result["answer"]
    assert "core" in result["answer"]
    assert "92%" in result["answer"]
    assert result["citations"] == ["src://cov"]


# ---------------------------------------------------------------------------
# Fix 2 — create_session returns minimal shape (no worktree ids)
# ---------------------------------------------------------------------------


def test_create_session_returns_minimal_shape_no_project(fake_rest):
    """create_session without a project returns only {session_id, status}."""
    fake = (
        fake_rest
        .on("POST", "/api/sessions", {"session_id": "s-min"})
        .on("POST", "/api/sessions/s-min/query", {"accepted": True}, status=202)
    )
    result = run(tools.SessionTools(fake.client()).create(prompt="hello"))
    assert result["session_id"] == "s-min"
    assert result["status"] == "running"
    assert "worktree_project_id" not in result
    assert "parent_project_id" not in result


def test_create_session_with_project_still_provisions_but_no_ids_returned(fake_rest):
    """Worktree is provisioned server-side but ids are NOT returned to the caller."""
    fake = (
        fake_rest
        .on("GET", "/api/v_projects/Repo/branches", {"current_branch": "main"})
        .on(
            "POST",
            "/api/v_projects/Repo/worktrees",
            {"project_id": "wt:42", "branch": "mewbo/task"},
            status=201,
        )
        .on("POST", "/api/sessions", {"session_id": "s-wt"})
        .on("POST", "/api/sessions/s-wt/query", {"accepted": True}, status=202)
    )
    result = run(
        tools.SessionTools(fake.client()).create(prompt="task", repo="Repo")
    )
    assert result["session_id"] == "s-wt"
    assert result["status"] == "running"
    # Worktree IS still provisioned (POST to worktrees endpoint was made)
    fake.find("POST", "/api/v_projects/Repo/worktrees")
    # But the caller does NOT receive the worktree ids
    assert "worktree_project_id" not in result
    assert "parent_project_id" not in result
