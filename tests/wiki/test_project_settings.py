"""Editable wiki-project settings — store + engine contracts.

Covers the half of the feature that lives BELOW the HTTP surface (the route
contract is ``test_routes_settings.py``):

- the ``ProjectSettings`` ↔ ``WizardSubmission`` projection (token never lands);
- the slug-keyed store accessors + partial ``update_project``, on BOTH backends;
- the read-preserve contract: a reindex must not wipe an edited model/desc —
  the whole point of the record (``Project`` is rebuilt wholesale at finalize);
- ``WikiIndexingJob.refresh`` preferring the settings record, and the legacy
  fallback's ordering (the uuid4-hex sort was NOT creation order);
- the graph-only flip dropping now-unreachable documentation pages.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import mongomock
import pytest
from mewbo_graph.wiki.store import JsonWikiStore, MongoWikiStore
from mewbo_graph.wiki.types import (
    Frontmatter,
    IndexingJob,
    Project,
    ProjectSettings,
    WikiPage,
    WizardSubmission,
    make_graph_node,
)

SLUG = "github.com/org/repo"


# ── Helpers ────────────────────────────────────────────────────────────────────


def _submission(**overrides) -> WizardSubmission:
    data = {
        "repoUrl": "https://github.com/org/repo",
        "slug": SLUG,
        "platform": "github",
        "depth": "comprehensive",
        "language": "en",
        "model": "anthropic/claude-sonnet-4-6",
        "filterMode": "exclude",
        "dirs": [],
        "files": [],
    }
    data.update(overrides)
    return WizardSubmission.model_validate(data)


def _project(slug: str = SLUG, desc: str = "from the platform API") -> Project:
    return Project(
        slug=slug,
        source="github",
        lang="en",
        indexedAt="2026-01-01T00:00:00Z",
        pages=1,
        desc=desc,
        repoUrl="https://github.com/org/repo",
    )


def _seed_graph(store, slug: str = SLUG) -> None:
    """One node so finalize's empty-graph completion gate passes."""
    store.upsert_nodes(
        slug,
        [
            make_graph_node(
                slug=slug, node_id=f"{slug}:n1", type="File",
                name="a.py", file="a.py", range=(0, 0),
            )
        ],
    )


@pytest.fixture(params=["json", "mongo"])
def store(request, tmp_path: Path):
    """Both persistence backends must behave identically."""
    if request.param == "json":
        return JsonWikiStore(root_dir=tmp_path / "wiki")
    return MongoWikiStore(client=mongomock.MongoClient(), database="test_settings")


# ── ProjectSettings ↔ WizardSubmission ────────────────────────────────────────


def test_from_submission_drops_the_token() -> None:
    """A secret must never reach the settings record — credentials have ONE registry."""
    settings = ProjectSettings.from_submission(_submission(token="ghp_supersecret"))

    assert "token" not in settings.model_dump()
    assert "ghp_supersecret" not in settings.model_dump_json()


def test_submission_round_trip_preserves_every_editable_field() -> None:
    """to_submission() must replay exactly what a refresh needs — and stay token-less."""
    original = _submission(
        ref="develop",
        depth="concise",
        graphOnly=True,
        filterMode="include",
        dirs=["src"],
        files=["*.py"],
        token="ghp_x",
    )

    replayed = ProjectSettings.from_submission(original).to_submission()

    assert replayed.token is None
    assert replayed.ref == "develop"
    assert replayed.depth == "concise"
    assert replayed.graph_only is True
    assert replayed.filter_mode == "include"
    assert replayed.dirs == ["src"]
    assert replayed.files == ["*.py"]
    assert replayed.model == original.model
    assert replayed.slug == original.slug


def test_from_submission_carries_an_existing_desc_override_forward() -> None:
    """Re-seeding from a submission (every start/refresh) must not drop the user's desc."""
    settings = ProjectSettings.from_submission(_submission(), desc="hand-written")

    assert settings.desc == "hand-written"


# ── Store: settings accessors (both backends) ─────────────────────────────────


def test_settings_round_trip(store) -> None:
    """Save → get returns an equal record, aliases and all."""
    settings = ProjectSettings.from_submission(
        _submission(ref="main", graphOnly=True), desc="edited"
    )

    store.save_project_settings(SLUG, settings)
    loaded = store.get_project_settings(SLUG)

    assert loaded is not None
    assert loaded.model == "anthropic/claude-sonnet-4-6"
    assert loaded.ref == "main"
    assert loaded.graph_only is True
    assert loaded.desc == "edited"
    assert loaded.slug == SLUG


def test_settings_absent_reads_none(store) -> None:
    """A project onboarded before the record existed has none — the NORMAL state."""
    assert store.get_project_settings(SLUG) is None


def test_settings_save_is_an_upsert(store) -> None:
    """A second save replaces rather than duplicating."""
    store.save_project_settings(SLUG, ProjectSettings.from_submission(_submission()))
    store.save_project_settings(
        SLUG, ProjectSettings.from_submission(_submission(model="openai/gpt-5.4"))
    )

    loaded = store.get_project_settings(SLUG)
    assert loaded is not None
    assert loaded.model == "openai/gpt-5.4"


def test_settings_delete(store) -> None:
    """Delete reports whether a record existed (so a re-created slug starts clean)."""
    store.save_project_settings(SLUG, ProjectSettings.from_submission(_submission()))

    assert store.delete_project_settings(SLUG) is True
    assert store.get_project_settings(SLUG) is None
    assert store.delete_project_settings(SLUG) is False


# ── Store: partial update_project (both backends) ─────────────────────────────


def test_update_project_applies_a_partial_edit(store) -> None:
    """desc is writable and the untouched fields survive."""
    store.create_project(_project())

    updated = store.update_project(SLUG, {"desc": "a better description"})

    assert updated is not None
    assert updated.desc == "a better description"
    assert updated.pages == 1  # untouched
    assert store.get_project(SLUG).desc == "a better description"  # persisted


def test_update_project_ignores_system_owned_fields(store) -> None:
    """The whitelist is the guard: a caller can hand over a whole body unfiltered.

    ``pages``/``indexedAt``/``landingPageId`` are rebuilt by the next index — letting
    a PATCH write them would make the record lie about what was actually indexed.
    """
    store.create_project(_project())

    updated = store.update_project(
        SLUG,
        {"desc": "new", "pages": 999, "indexed_at": "1999-01-01T00:00:00Z", "slug": "evil/repo"},
    )

    assert updated is not None
    assert updated.desc == "new"
    assert updated.pages == 1
    assert updated.indexed_at == "2026-01-01T00:00:00Z"
    assert updated.slug == SLUG


def test_update_project_unknown_slug_returns_none(store) -> None:
    """Absent project → None (the route turns that into a 404)."""
    assert store.update_project("nope/nope", {"desc": "x"}) is None


def test_update_project_with_no_writable_field_is_a_noop(store) -> None:
    """An update carrying nothing writable returns the record unchanged."""
    store.create_project(_project())

    updated = store.update_project(SLUG, {"pages": 42})

    assert updated is not None
    assert updated.desc == "from the platform API"
    assert updated.pages == 1


# ── Read-preserve: a reindex must not wipe an edit ────────────────────────────


def test_finalize_does_not_wipe_an_edited_desc(tmp_path: Path) -> None:
    """THE regression this feature exists to prevent.

    ``Project`` is rebuilt WHOLESALE at every finalize, and the description
    normally comes from the platform API — so without the read-preserve seam a
    user's edited description is silently overwritten on the next reindex.
    """
    import mewbo_graph.plugins.wiki.finalize as mod
    from mewbo_graph.plugins.wiki.finalize import WikiFinalizeTool

    store = JsonWikiStore(root_dir=tmp_path)
    _seed_graph(store)
    store.create_job(
        IndexingJob(
            job_id="job-1", slug=SLUG, status="finalizing",
            scanned_count=1, total_count=1, current_file=None,
        )
    )
    store.attach_job_session("job-1", "sess-1")
    store.save_job_submission("job-1", _submission().model_dump(by_alias=True))
    store.save_page(
        SLUG,
        WikiPage(
            id="overview", title="Overview",
            frontmatter=Frontmatter(title="Overview", slug="overview"),
            body="# Overview", toc=[], nav=[],
        ),
    )
    store.create_project(_project())
    # The user edited the description.
    store.save_project_settings(
        SLUG, ProjectSettings.from_submission(_submission(), desc="MY hand-written blurb")
    )

    step = MagicMock()
    step.tool_input = {"landingPageId": "overview"}
    tool = WikiFinalizeTool(session_id="sess-1")
    # The platform API answers with the repo's own description — which is exactly
    # what would clobber the edit if finalize didn't read-preserve.
    with patch.object(mod, "_resolve_runtime", return_value=SimpleNamespace(wiki_store=store)), \
         patch.object(mod, "_fetch_description", return_value="upstream repo blurb"):
        result = asyncio.run(tool.handle(step))

    assert "error" not in result.content
    assert store.get_project(SLUG).desc == "MY hand-written blurb"


def test_finalize_still_fetches_when_no_override_is_set(tmp_path: Path) -> None:
    """No override ⇒ byte-identical to the old behaviour (the platform fetch wins)."""
    import mewbo_graph.plugins.wiki.finalize as mod
    from mewbo_graph.plugins.wiki.finalize import WikiFinalizeTool

    store = JsonWikiStore(root_dir=tmp_path)
    _seed_graph(store)
    store.create_job(
        IndexingJob(
            job_id="job-2", slug=SLUG, status="finalizing",
            scanned_count=1, total_count=1, current_file=None,
        )
    )
    store.attach_job_session("job-2", "sess-2")
    store.save_job_submission("job-2", _submission().model_dump(by_alias=True))
    store.save_page(
        SLUG,
        WikiPage(
            id="overview", title="Overview",
            frontmatter=Frontmatter(title="Overview", slug="overview"),
            body="# Overview", toc=[], nav=[],
        ),
    )

    step = MagicMock()
    step.tool_input = {"landingPageId": "overview"}
    tool = WikiFinalizeTool(session_id="sess-2")
    with patch.object(mod, "_resolve_runtime", return_value=SimpleNamespace(wiki_store=store)), \
         patch.object(mod, "_fetch_description", return_value="upstream repo blurb"):
        asyncio.run(tool.handle(step))

    assert store.get_project(SLUG).desc == "upstream repo blurb"


def test_reindex_does_not_wipe_an_edited_model(tmp_path: Path) -> None:
    """The edited model must survive a completed index and drive the NEXT refresh.

    ``model`` isn't a ``Project`` field at all, so the wipe vector is the settings
    record being re-seeded from a submission — which is why ``start`` merges rather
    than overwrites.
    """
    store = JsonWikiStore(root_dir=tmp_path)
    store.create_project(_project())
    store.save_project_settings(
        SLUG,
        ProjectSettings.from_submission(_submission(model="openai/gpt-5.4"), desc="edited"),
    )

    runtime = MagicMock()
    runtime.wiki_store = store
    runtime.resolve_session.return_value = "sess-x"

    from mewbo_api.wiki.jobs import WikiIndexingJob

    with patch(
        "mewbo_core.config.get_config"
    ) as cfg:
        cfg.return_value.llm.resolve_available_model.side_effect = lambda m, fallback: m
        WikiIndexingJob.refresh(SLUG, runtime=runtime, hook_manager=None)

    after = store.get_project_settings(SLUG)
    assert after is not None
    assert after.model == "openai/gpt-5.4"
    assert after.desc == "edited"


# ── refresh: settings record wins; legacy fallback is ordered honestly ────────


def test_refresh_prefers_the_settings_record_over_the_job_sidecars(tmp_path: Path) -> None:
    """An edit is only real if the next index actually runs with it."""
    store = JsonWikiStore(root_dir=tmp_path)
    store.create_project(_project())
    # A stale sidecar from the original onboarding.
    store.create_job(
        IndexingJob(
            job_id="old-job", slug=SLUG, status="complete",
            scanned_count=1, total_count=1, current_file=None,
            phase_started_at="2026-01-01T00:00:00Z",
        )
    )
    store.save_job_submission(
        "old-job", _submission(model="stale/model", ref=None).model_dump(by_alias=True)
    )
    # …and the edited settings the user actually wants.
    store.save_project_settings(
        SLUG,
        ProjectSettings.from_submission(_submission(model="openai/gpt-5.4", ref="develop")),
    )

    runtime = MagicMock()
    runtime.wiki_store = store
    runtime.resolve_session.return_value = "sess-x"

    from mewbo_api.wiki.jobs import WikiIndexingJob

    with patch("mewbo_core.config.get_config") as cfg:
        cfg.return_value.llm.resolve_available_model.side_effect = lambda m, fallback: m
        job = WikiIndexingJob.refresh(SLUG, runtime=runtime, hook_manager=None)

    # The NEW job's submission is the edited one, not the stale sidecar.
    fresh = store.get_job_submission(job.job_id)
    assert fresh["model"] == "openai/gpt-5.4"
    assert fresh["ref"] == "develop"
    assert job.model == "openai/gpt-5.4"


def test_legacy_submission_scan_orders_by_phase_not_by_uuid(tmp_path: Path) -> None:
    """The refresh-sort bug: ``job_id`` is a uuid4 hex, so it does NOT sort by recency.

    Job ids are chosen so that lexicographic order (the OLD key) is the exact
    REVERSE of chronological order — the old sort would pick the oldest
    submission and silently re-index with retired settings.
    """
    from mewbo_api.wiki.jobs import _latest_job_submission

    store = JsonWikiStore(root_dir=tmp_path)
    # "aaa" < "zzz" lexicographically, but "aaa" is the NEWER job by phase time.
    store.create_job(
        IndexingJob(
            job_id="zzz", slug=SLUG, status="complete",
            scanned_count=1, total_count=1, current_file=None,
            phase_started_at="2026-01-01T00:00:00Z",
        )
    )
    store.save_job_submission("zzz", _submission(model="old/model").model_dump(by_alias=True))
    store.create_job(
        IndexingJob(
            job_id="aaa", slug=SLUG, status="complete",
            scanned_count=1, total_count=1, current_file=None,
            phase_started_at="2026-06-01T00:00:00Z",
        )
    )
    store.save_job_submission("aaa", _submission(model="new/model").model_dump(by_alias=True))

    latest = _latest_job_submission(store, SLUG)

    assert latest is not None
    assert latest.model == "new/model"


def test_start_seeds_the_settings_record(tmp_path: Path) -> None:
    """Onboarding materialises the edit target, so GET settings works immediately."""
    store = JsonWikiStore(root_dir=tmp_path)
    runtime = MagicMock()
    runtime.wiki_store = store
    runtime.resolve_session.return_value = "sess-x"

    from mewbo_api.wiki.jobs import WikiIndexingJob

    WikiIndexingJob.start(_submission(ref="main"), runtime=runtime, hook_manager=None)

    settings = store.get_project_settings(SLUG)
    assert settings is not None
    assert settings.ref == "main"
    assert settings.model == "anthropic/claude-sonnet-4-6"


# ── graph-only flip drops now-unreachable pages ───────────────────────────────


def test_graph_only_finalize_drops_orphaned_pages(tmp_path: Path) -> None:
    """Flipping a documented project to graph-only must not leave unreachable pages.

    The Project is stamped ``graph_only=True``, so the doc-read seam makes every
    surviving page raise — dead rows that would then collide with freshly generated
    ones if the project is ever flipped back.
    """
    from mewbo_graph.plugins.wiki.graph_only import GraphOnlyIndexer, build_graph_only_ctx

    store = JsonWikiStore(root_dir=tmp_path)
    _seed_graph(store)
    store.create_job(
        IndexingJob(
            job_id="job-go", slug=SLUG, status="scanning",
            scanned_count=1, total_count=1, current_file=None,
        )
    )
    # Pages from the project's previous DOCUMENTED index.
    store.save_page(
        SLUG,
        WikiPage(
            id="overview", title="Overview",
            frontmatter=Frontmatter(title="Overview", slug="overview"),
            body="# Overview", toc=[], nav=[],
        ),
    )
    assert len(store.list_pages(SLUG)) == 1

    ctx = build_graph_only_ctx(job_id="job-go", slug=SLUG, store=store)
    indexer = GraphOnlyIndexer(ctx, _submission(graphOnly=True))
    with patch(
        "mewbo_graph.plugins.wiki.graph_only._resolve_project_desc", return_value="d"
    ):
        indexer._finalize()

    assert store.list_pages(SLUG) == []
    project = store.get_project(SLUG)
    assert project is not None
    assert project.graph_only is True
    assert project.pages == 0
