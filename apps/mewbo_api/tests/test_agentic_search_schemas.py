"""Tests for the Agentic Search Pydantic wire/storage contracts."""

# mypy: ignore-errors

import pytest
from mewbo_api.agentic_search.schemas import (
    OUTPUT_CONTRACT_VERSION,
    SEARCH_EVENT_TYPES,
    TERMINAL_EVENT_TYPES,
    AnswerBullet,
    AnswerSynthesis,
    PastQuery,
    RunPayload,
    RunRecord,
    SearchResult,
    SourceCatalogEntry,
    Workspace,
    WorkspaceInput,
    clean_for_model,
)
from pydantic import ValidationError


def test_source_catalog_entry_roundtrip():
    """SourceCatalogEntry round-trips through model_dump/model_validate."""
    entry = SourceCatalogEntry(
        id="notion", name="Notion", glyph="N", tool_ids=["notion_search"]
    )
    dumped = entry.model_dump()
    assert dumped["id"] == "notion"
    assert dumped["available"] is True
    assert dumped["unavailable_reason"] is None
    assert dumped["tool_ids"] == ["notion_search"]
    again = SourceCatalogEntry.model_validate(dumped)
    assert again == entry


def test_workspace_roundtrip_with_past_queries():
    """Workspace (incl. nested PastQuery) survives a dump/validate cycle."""
    ws = Workspace(
        id="ws-1",
        name="Demo",
        sources=["web", "notion"],
        past_queries=[PastQuery(q="hello", run_id="run-1", status="completed")],
    )
    dumped = ws.model_dump()
    again = Workspace.model_validate(dumped)
    assert again == ws
    assert again.past_queries[0].q == "hello"
    # Default ISO timestamp fields are populated.
    assert ws.created_at
    assert ws.updated_at


def test_run_record_roundtrip_with_payload():
    """RunRecord nests a RunPayload and round-trips intact."""
    payload = RunPayload(
        run_id="run-1",
        session_id="sess-1",
        query="q",
        workspace_id="ws-1",
        results=[SearchResult(id="r1", source="web", kind="web", title="T")],
        answer=AnswerSynthesis(tldr="t", sources_count=1),
    )
    record = RunRecord(
        run_id="run-1",
        session_id="sess-1",
        workspace_id="ws-1",
        query="q",
        status="completed",
        payload=payload,
    )
    again = RunRecord.model_validate(record.model_dump())
    assert again == record
    assert again.payload.results[0].id == "r1"
    assert again.output_contract_version == OUTPUT_CONTRACT_VERSION


@pytest.mark.parametrize(
    "model_cls, valid_kwargs",
    [
        (SourceCatalogEntry, {"id": "x", "name": "X"}),
        (Workspace, {"id": "x", "name": "X"}),
        (WorkspaceInput, {"name": "X"}),
        (PastQuery, {"q": "x"}),
        (SearchResult, {"id": "r1", "source": "web", "kind": "web", "title": "T"}),
        (AnswerBullet, {"text": "b"}),
    ],
)
def test_extra_forbid_rejects_unknown_keys(model_cls, valid_kwargs):
    """Every wire model forbids unknown keys (extra='forbid')."""
    # Sanity: the valid kwargs construct cleanly.
    model_cls(**valid_kwargs)
    with pytest.raises(ValidationError):
        model_cls(**valid_kwargs, surprise_field="boom")


def test_workspace_input_rejects_empty_name():
    """WorkspaceInput enforces a non-empty name (min_length=1)."""
    with pytest.raises(ValidationError):
        WorkspaceInput(name="")
    # A non-empty name is accepted with sensible defaults.
    ok = WorkspaceInput(name="QA")
    assert ok.sources == []
    assert ok.desc == ""


def test_clean_for_model_whitelists_declared_fields():
    """clean_for_model drops bookkeeping/underscore keys not on the model."""
    doc = {
        "id": "notion",
        "name": "Notion",
        "_id": "mongo-oid",
        "idx": 4,
        "event_count": 9,
        "not_a_field": True,
    }
    cleaned = clean_for_model(doc, SourceCatalogEntry)
    assert cleaned == {"id": "notion", "name": "Notion"}
    # The cleaned doc validates against the model without error.
    SourceCatalogEntry.model_validate(cleaned)


def test_event_type_membership_sanity():
    """Terminal event types are a strict subset of the full vocabulary."""
    assert TERMINAL_EVENT_TYPES <= SEARCH_EVENT_TYPES
    assert {"run_done", "error", "cancelled"} == set(TERMINAL_EVENT_TYPES)
    # Non-terminal lifecycle events live in SEARCH_EVENT_TYPES but not terminal.
    assert "run_started" in SEARCH_EVENT_TYPES
    assert "run_started" not in TERMINAL_EVENT_TYPES
    assert "answer_delta" in SEARCH_EVENT_TYPES
    # heartbeat is transport-only and intentionally absent from the vocabulary.
    assert "heartbeat" not in SEARCH_EVENT_TYPES
