"""Round-trip + extra-forbid tests for wiki Pydantic wire types."""
from __future__ import annotations

import pydantic
import pytest
from mewbo_api.wiki.types import (
    AccordionBlock,
    BlockCloseEvent,
    BlockDeltaEvent,
    BlockOpenEvent,
    BlockUnion,
    CancelledEvent,
    CompleteEvent,
    DiagramBlock,
    Embedding,
    ErrorEvent,
    FinalizingEvent,
    GraphEdge,
    GraphNode,
    H2Block,
    H3Block,
    HeartbeatEvent,
    HrBlock,
    IndexingEventUnion,
    IndexingJob,
    # InlineNode
    InlineNode,
    MetaEvent,
    NavEntry,
    # Internal
    PagePlan,
    # Block variants
    PBlock,
    Project,
    # Q&A
    QaCancelledEvent,
    QaCompleteEvent,
    QaErrorEvent,
    QaEventUnion,
    QaHeartbeatEvent,
    # Indexing events
    QueuedEvent,
    ScannedEvent,
    ScanningEvent,
    SourcesBlock,
    SummaryReadyEvent,
    TableBlock,
    TocEntry,
    UlBlock,
    WikiError,
    WikiPage,
    WizardSubmission,
)

# ── Helpers ────────────────────────────────────────────────────────────────────

def roundtrip(model_cls, data: dict):
    """Instantiate, dump to JSON dict (by alias), then re-parse — assert equal."""
    obj = model_cls.model_validate(data)
    dumped = obj.model_dump(mode="json", by_alias=True)
    reparsed = model_cls.model_validate(dumped)
    assert obj == reparsed
    return obj, dumped


# ── 1. Project ─────────────────────────────────────────────────────────────────

def test_project_roundtrip():
    data = {
        "slug": "bearlike/assistant",
        "source": "gitea",
        "lang": "Python",
        "indexedAt": "2026-05-14T10:00:00Z",
        "pages": 42,
        "desc": "AI assistant monorepo",
        "primary": True,
    }
    obj, dumped = roundtrip(Project, data)
    assert dumped["slug"] == "bearlike/assistant"
    assert dumped["indexedAt"] == "2026-05-14T10:00:00Z"
    assert dumped["pages"] == 42
    # Python attribute uses snake_case
    assert obj.indexed_at == "2026-05-14T10:00:00Z"


def test_project_primary_optional():
    data = {
        "slug": "x/y",
        "source": "github",
        "lang": "Go",
        "indexedAt": "2026-01-01T00:00:00Z",
        "pages": 1,
        "desc": "minimal",
    }
    obj, dumped = roundtrip(Project, data)
    assert obj.primary is None
    # optional field omitted from dump when None by default — just check no error


# ── 2. WizardSubmission ────────────────────────────────────────────────────────

def test_wizard_submission_roundtrip():
    data = {
        "repoUrl": "https://github.com/example/repo",
        "slug": "example/repo",
        "platform": "github",
        "token": "ghp_secret",
        "depth": "comprehensive",
        "language": "en",
        "model": "anthropic/claude-sonnet-4-5",
        "filterMode": "exclude",
        "dirs": ["src", "tests"],
        "files": ["README.md"],
    }
    obj, dumped = roundtrip(WizardSubmission, data)
    assert dumped["repoUrl"] == "https://github.com/example/repo"
    assert dumped["filterMode"] == "exclude"
    assert obj.repo_url == "https://github.com/example/repo"
    assert obj.filter_mode == "exclude"


def test_wizard_submission_token_optional():
    data = {
        "repoUrl": "https://github.com/x/y",
        "slug": "x/y",
        "platform": "gitlab",
        "depth": "concise",
        "language": "fr",
        "model": "openai/gpt-4o",
        "filterMode": "include",
        "dirs": [],
        "files": [],
    }
    obj, _ = roundtrip(WizardSubmission, data)
    assert obj.token is None


# ── 3. IndexingJob ─────────────────────────────────────────────────────────────

def test_indexing_job_roundtrip():
    data = {
        "jobId": "job-abc-123",
        "slug": "x/y",
        "status": "scanning",
        "scannedCount": 5,
        "totalCount": 20,
        "currentFile": "src/main.py",
        "landingPageId": "overview",
        "error": None,
    }
    obj, dumped = roundtrip(IndexingJob, data)
    assert dumped["jobId"] == "job-abc-123"
    assert dumped["scannedCount"] == 5
    assert obj.job_id == "job-abc-123"
    assert obj.scanned_count == 5


def test_indexing_job_invalid_status():
    with pytest.raises(pydantic.ValidationError):
        IndexingJob.model_validate({
            "jobId": "j",
            "slug": "x/y",
            "status": "unknown_status",
            "scannedCount": 0,
            "totalCount": 0,
            "currentFile": None,
        })


# ── 4. IndexingEvent discriminated union ───────────────────────────────────────

@pytest.mark.parametrize("data,expected_type", [
    (
        {"type": "queued", "jobId": "j1", "slug": "x/y", "totalCount": 10},
        QueuedEvent,
    ),
    (
        {"type": "scanning", "file": "src/a.py", "index": 0, "totalCount": 10},
        ScanningEvent,
    ),
    (
        {"type": "scanned", "file": "src/a.py", "index": 0, "totalCount": 10},
        ScannedEvent,
    ),
    (
        {"type": "finalizing", "scannedCount": 10, "totalCount": 10},
        FinalizingEvent,
    ),
    (
        {"type": "complete", "landingPageId": "overview", "pageCount": 5},
        CompleteEvent,
    ),
    (
        {"type": "cancelled"},
        CancelledEvent,
    ),
    (
        {"type": "error", "error": {"code": "internal", "message": "oops"}},
        ErrorEvent,
    ),
    (
        {"type": "heartbeat"},
        HeartbeatEvent,
    ),
])
def test_indexing_event_roundtrip(data, expected_type):
    wrapper = IndexingEventUnion.model_validate(data)
    # IndexingEventUnion is a RootModel; the actual event lives in .root
    assert isinstance(wrapper.root, expected_type)
    dumped = wrapper.model_dump(mode="json", by_alias=True)
    reparsed = IndexingEventUnion.model_validate(dumped)
    assert wrapper == reparsed


# ── 5. QaEvent discriminated union ────────────────────────────────────────────

@pytest.mark.parametrize("data,expected_type", [
    (
        {
            "type": "meta", "answerId": "a1",
            "model": "anthropic/claude-sonnet-4-5", "fromPageId": "overview",
        },
        MetaEvent,
    ),
    (
        {"type": "summary_ready", "sources": ["src/a.py", "src/b.py"]},
        SummaryReadyEvent,
    ),
    (
        {"type": "block_open", "index": 0, "block": {"kind": "hr"}},
        BlockOpenEvent,
    ),
    (
        {"type": "block_delta", "index": 0, "textAppend": "hello"},
        BlockDeltaEvent,
    ),
    (
        {"type": "block_close", "index": 0},
        BlockCloseEvent,
    ),
    (
        {"type": "complete", "totalBlocks": 3},
        QaCompleteEvent,
    ),
    (
        {"type": "cancelled"},
        QaCancelledEvent,
    ),
    (
        {"type": "error", "error": {"code": "rate_limited", "message": "slow down"}},
        QaErrorEvent,
    ),
    (
        {"type": "heartbeat"},
        QaHeartbeatEvent,
    ),
])
def test_qa_event_roundtrip(data, expected_type):
    wrapper = QaEventUnion.model_validate(data)
    # QaEventUnion is a RootModel; the actual event lives in .root
    assert isinstance(wrapper.root, expected_type)
    dumped = wrapper.model_dump(mode="json", by_alias=True)
    reparsed = QaEventUnion.model_validate(dumped)
    assert wrapper == reparsed


# ── 6. Block discriminated union ───────────────────────────────────────────────

@pytest.mark.parametrize("data,expected_type", [
    ({"kind": "p", "text": "hello world"}, PBlock),
    ({"kind": "h2", "id": "intro", "text": "Introduction"}, H2Block),
    ({"kind": "h3", "text": "Subtitle"}, H3Block),
    ({"kind": "hr"}, HrBlock),
    ({"kind": "ul", "items": ["item one", "item two"]}, UlBlock),
    ({"kind": "accordion", "title": "FAQ", "items": ["q1", "q2"]}, AccordionBlock),
    ({"kind": "sources", "items": ["src/a.py", "src/b.py"]}, SourcesBlock),
    (
        {"kind": "table", "head": ["Col A", "Col B"], "rows": [["cell1", "cell2"]]},
        TableBlock,
    ),
    ({"kind": "diagram", "id": "arch-diagram"}, DiagramBlock),
])
def test_block_roundtrip(data, expected_type):
    wrapper = BlockUnion.model_validate(data)
    # BlockUnion is a RootModel; the actual block lives in .root
    assert isinstance(wrapper.root, expected_type)
    dumped = wrapper.model_dump(mode="json", by_alias=True)
    reparsed = BlockUnion.model_validate(dumped)
    assert wrapper == reparsed


def test_inline_node_variants():
    """InlineNode handles all its variant shapes."""
    # plain string
    n1 = InlineNode.model_validate("hello")
    assert n1.root == "hello"

    # code span
    n2 = InlineNode.model_validate({"code": "x = 1"})
    assert n2.root == {"code": "x = 1"}

    # link
    n3 = InlineNode.model_validate({"link": "https://example.com", "text": "Example"})
    assert n3.root == {"link": "https://example.com", "text": "Example"}

    # src reference
    n4 = InlineNode.model_validate({"kind": "src", "path": "src/main.py", "lines": "1-10"})
    assert n4.root == {"kind": "src", "path": "src/main.py", "lines": "1-10"}

    # list of InlineNode (recursive)
    n5 = InlineNode.model_validate(["hello", {"code": "x"}])
    assert isinstance(n5.root, list)


# ── 7. WikiPage ────────────────────────────────────────────────────────────────

def test_wiki_page_roundtrip():
    data = {
        "id": "overview",
        "title": "Overview",
        "frontmatter": {
            "title": "Overview",
            "slug": "overview",
            "relevantSources": [{"path": "src/main.py", "lines": "1-50"}],
            "sources": [{"path": "README.md"}],
        },
        "body": "# Overview\n\nThis is the overview page.",
        "toc": [{"id": "overview", "label": "Overview", "lvl": 1}],
        "nav": [{"id": "overview", "label": "Overview", "lvl": 1, "parent": None}],
    }
    obj, dumped = roundtrip(WikiPage, data)
    assert dumped["id"] == "overview"
    assert dumped["frontmatter"]["relevantSources"][0]["path"] == "src/main.py"
    assert len(dumped["toc"]) == 1
    assert len(dumped["nav"]) == 1


# ── 8. WikiError ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("code", [
    "not_found", "forbidden", "repo_access", "quota_exceeded",
    "rate_limited", "validation", "cancelled", "internal", "network",
])
def test_wiki_error_roundtrip(code):
    data = {
        "code": code,
        "message": f"Error: {code}",
        "hint": "Try again later",
        "fields": {"url": "Invalid URL"},
        "retryAfter": 30,
    }
    obj, dumped = roundtrip(WikiError, data)
    assert dumped["code"] == code
    assert dumped["retryAfter"] == 30
    assert obj.retry_after == 30


def test_wiki_error_invalid_code():
    with pytest.raises(pydantic.ValidationError):
        WikiError.model_validate({"code": "bogus_code", "message": "x"})


# ── 9. PagePlan (internal) ─────────────────────────────────────────────────────

def test_page_plan_roundtrip():
    data = {
        "id": "architecture",
        "title": "Architecture",
        "description": "System architecture overview",
        "importance": "high",
        "relevantFiles": ["src/main.py", "src/core.py"],
        "relatedPages": ["overview"],
        "parent": None,
    }
    obj, dumped = roundtrip(PagePlan, data)
    assert obj.id == "architecture"
    assert obj.importance == "high"
    assert dumped["relevantFiles"] == ["src/main.py", "src/core.py"]


def test_page_plan_defaults():
    obj = PagePlan(id="x", title="X")
    assert obj.description == ""
    assert obj.importance == "medium"
    assert obj.relevant_files == []
    assert obj.related_pages == []
    assert obj.parent is None


# ── 10. GraphNode / GraphEdge / Embedding ─────────────────────────────────────

def test_graph_node_roundtrip():
    data = {
        "slug": "x/y",
        "node_id": "n1",
        "type": "Class",
        "name": "MyClass",
        "file": "src/main.py",
        "range": [10, 50],
        "docstring": "A class.",
    }
    obj, dumped = roundtrip(GraphNode, data)
    assert obj.node_id == "n1"
    assert obj.type == "Class"
    assert dumped["range"] == [10, 50]


def test_graph_edge_roundtrip():
    data = {
        "slug": "x/y",
        "source": "n1",
        "target": "n2",
        "type": "CALLS",
    }
    obj, dumped = roundtrip(GraphEdge, data)
    assert obj.source == "n1"
    assert obj.target == "n2"


def test_embedding_roundtrip():
    data = {
        "slug": "x/y",
        "node_id": "n1",
        "vector": [0.1, 0.2, 0.3],
        "model": "text-embedding-3-small",
        "dim": 3,
    }
    obj, dumped = roundtrip(Embedding, data)
    assert len(obj.vector) == 3
    assert obj.dim == 3


# ── 11. extra="forbid" enforcement ────────────────────────────────────────────

@pytest.mark.parametrize("model_cls,valid_data,unknown_key", [
    (
        Project,
        {
            "slug": "x/y", "source": "github", "lang": "en",
            "indexedAt": "2026-01-01T00:00:00Z", "pages": 1, "desc": "d",
        },
        "unknownField",
    ),
    (
        WikiError,
        {"code": "internal", "message": "err"},
        "extra",
    ),
    (
        TocEntry,
        {"id": "x", "label": "X", "lvl": 1},
        "bogus",
    ),
    (
        NavEntry,
        {"id": "x", "label": "X", "lvl": 2},
        "surprise",
    ),
    (
        IndexingJob,
        {
            "jobId": "j", "slug": "x/y", "status": "queued",
            "scannedCount": 0, "totalCount": 0, "currentFile": None,
        },
        "extra",
    ),
])
def test_extra_forbid(model_cls, valid_data, unknown_key):
    bad_data = {**valid_data, unknown_key: "unexpected"}
    with pytest.raises(pydantic.ValidationError, match="Extra inputs are not permitted"):
        model_cls.model_validate(bad_data)


# ── 12. camelCase alias compatibility ─────────────────────────────────────────

def test_camel_alias_and_snake_attr():
    """Parsing camelCase JSON works; attribute access uses snake_case."""
    data = {
        "jobId": "job-xyz",
        "slug": "x/y",
        "status": "queued",
        "scannedCount": 0,
        "totalCount": 5,
        "currentFile": None,
    }
    obj = IndexingJob.model_validate(data)
    # Python attribute is snake_case
    assert obj.job_id == "job-xyz"
    assert obj.scanned_count == 0
    assert obj.total_count == 5
    assert obj.current_file is None

    # Dump uses camelCase alias
    dumped = obj.model_dump(mode="json", by_alias=True)
    assert "jobId" in dumped
    assert "scannedCount" in dumped


def test_populate_by_name_allows_snake_case_input():
    """With populate_by_name=True, snake_case field names also work on input."""
    data = {
        "job_id": "job-abc",
        "slug": "x/y",
        "status": "complete",
        "scanned_count": 10,
        "total_count": 10,
        "current_file": None,
    }
    obj = IndexingJob.model_validate(data)
    assert obj.job_id == "job-abc"
