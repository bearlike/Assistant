"""``ManifestHash`` — the tool-list fingerprint that gates auto re-map (#81-C).

The drift gate is only as good as the hash: it must be (a) order-independent — a
server that re-orders its advertised tools is NOT a drift — and (b) schema-aware
— a renamed / newly-required argument IS a drift. These tests pin both, plus the
no-tools sentinel the workspace-save path relies on.
"""

from __future__ import annotations

from mewbo_graph.scg.manifest import ManifestHash


def _tools_a() -> list[dict[str, object]]:
    return [
        {
            "name": "search_issues",
            "description": "List repo issues.",
            "inputSchema": {
                "type": "object",
                "properties": {"repo": {"type": "string"}, "state": {"type": "string"}},
                "required": ["repo"],
            },
        },
        {
            "name": "get_issue",
            "description": "Get one issue by id.",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
    ]


def test_hash_is_stable_for_identical_manifests() -> None:
    """The same tool list hashes equal across two independent observations."""
    assert ManifestHash.of_tool_list(_tools_a()) == ManifestHash.of_tool_list(_tools_a())


def test_hash_is_order_independent() -> None:
    """Reordering the advertised tool list is NOT a drift."""
    forward = _tools_a()
    reversed_ = list(reversed(_tools_a()))
    assert ManifestHash.of_tool_list(forward) == ManifestHash.of_tool_list(reversed_)


def test_added_tool_changes_hash() -> None:
    """Adding a tool perturbs the hash (a new capability appeared)."""
    base = ManifestHash.of_tool_list(_tools_a())
    grown = _tools_a() + [{"name": "close_issue"}]
    assert ManifestHash.of_tool_list(grown) != base


def test_removed_tool_changes_hash() -> None:
    """Removing a tool perturbs the hash (a capability vanished)."""
    base = ManifestHash.of_tool_list(_tools_a())
    shrunk = _tools_a()[:1]
    assert ManifestHash.of_tool_list(shrunk) != base


def test_renamed_argument_changes_hash() -> None:
    """A renamed input field is a drift (the binding surface changed)."""
    base = ManifestHash.of_tool_list(_tools_a())
    tools = _tools_a()
    tools[0]["inputSchema"]["properties"] = {  # type: ignore[index]
        "repository": {"type": "string"},
        "state": {"type": "string"},
    }
    assert ManifestHash.of_tool_list(tools) != base


def test_newly_required_argument_changes_hash() -> None:
    """An argument becoming required is a drift (an access-pattern limit changed)."""
    base = ManifestHash.of_tool_list(_tools_a())
    tools = _tools_a()
    tools[0]["inputSchema"]["required"] = ["repo", "state"]  # type: ignore[index]
    assert ManifestHash.of_tool_list(tools) != base


def test_description_change_changes_hash() -> None:
    """A changed tool description perturbs the hash (the prose surface drifted)."""
    base = ManifestHash.of_tool_list(_tools_a())
    tools = _tools_a()
    tools[0]["description"] = "Search repository issues by repo and state."
    assert ManifestHash.of_tool_list(tools) != base


def test_unnamed_and_nondict_entries_are_skipped() -> None:
    """Malformed entries don't perturb the hash (they produce no SCG node)."""
    base = ManifestHash.of_tool_list(_tools_a())
    noisy = [*_tools_a(), {"no_name": True}, "not-a-dict", 42]
    assert ManifestHash.of_tool_list(noisy) == base


def test_of_descriptor_raw_reads_tools_list() -> None:
    """of_descriptor_raw hashes the ``tools`` list inside a descriptor raw."""
    assert ManifestHash.of_descriptor_raw({"tools": _tools_a()}) == ManifestHash.of_tool_list(
        _tools_a()
    )


def test_no_tools_is_a_stable_sentinel() -> None:
    """A descriptor with no ``tools`` hashes the empty manifest (never raises)."""
    empty = ManifestHash.of_tool_list([])
    assert ManifestHash.of_descriptor_raw({}) == empty
    assert ManifestHash.of_descriptor_raw({"openapi": "3.1.0"}) == empty
