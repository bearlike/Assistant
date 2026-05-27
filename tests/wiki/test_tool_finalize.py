"""Tests for WikiFinalizeTool — TDD: tests written first."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import Frontmatter, IndexingJob, WikiPage

# ── Helpers ────────────────────────────────────────────────────────────────────


def _store(tmp_path: Path) -> JsonWikiStore:
    return JsonWikiStore(root_dir=tmp_path)


def _job(job_id: str = "job-fin", slug: str = "org/repo") -> IndexingJob:
    return IndexingJob(
        job_id=job_id,
        slug=slug,
        status="finalizing",
        scanned_count=10,
        total_count=10,
        current_file=None,
    )


def _fake_runtime(store: JsonWikiStore) -> SimpleNamespace:
    return SimpleNamespace(wiki_store=store)


def _make_action_step(tool_input: dict) -> MagicMock:
    step = MagicMock()
    step.tool_input = tool_input
    return step


def _save_page(store: JsonWikiStore, slug: str, page_id: str, title: str = "Title") -> None:
    """Helper to persist a minimal WikiPage."""
    fm = Frontmatter(title=title, slug=page_id)
    page = WikiPage(id=page_id, title=title, frontmatter=fm, body="# Body", toc=[], nav=[])
    store.save_page(slug, page)


def _seed_submission(
    store: JsonWikiStore,
    job_id: str,
    *,
    slug: str = "github.com/org/repo",
    repo_url: str = "https://github.com/org/repo",
    platform: str = "github",
    language: str = "en",
) -> None:
    """Seed the canonical wizard submission for *job_id*.

    finalize.py refuses to finalize without one — the submission is the
    sole canonical source of identity (platform, host via repo_url, lang).
    """
    store.save_job_submission(
        job_id,
        {
            "repoUrl": repo_url,
            "slug": slug,
            "platform": platform,
            "language": language,
            "depth": "concise",
            "model": "anthropic/claude-sonnet-4-6",
            "filterMode": "exclude",
            "dirs": [],
            "files": [],
        },
    )


# ── Test 1: finalize persists project + emits complete event ──────────────────


def test_finalize_persists_project_and_emits_complete(tmp_path: Path) -> None:
    """Submit 3 pages → finalize → project exists, status=complete, complete event emitted."""
    import mewbo_graph.plugins.wiki.finalize as mod
    from mewbo_graph.plugins.wiki.finalize import WikiFinalizeTool

    store = _store(tmp_path)
    job = _job("job-fin1", "org/repo")
    store.create_job(job)
    store.attach_job_session("job-fin1", "sess-fin1")
    _seed_submission(store, "job-fin1", slug="org/repo")

    # Save 3 pages
    for pid in ("overview", "architecture", "api-reference"):
        _save_page(store, "org/repo", pid)

    runtime = _fake_runtime(store)
    tool = WikiFinalizeTool(session_id="sess-fin1")

    with patch.object(mod, "_resolve_runtime", return_value=runtime):
        result = asyncio.run(tool.handle(_make_action_step({"landingPageId": "overview"})))

    assert "error" not in result.content
    assert "complete" in result.content
    assert "pageCount" in result.content

    # Project persisted
    project = store.get_project("org/repo")
    assert project is not None
    assert project.slug == "org/repo"
    assert project.pages == 3

    # Job status updated
    updated_job = store.get_job("job-fin1")
    assert updated_job is not None
    assert updated_job.status == "complete"
    assert updated_job.landing_page_id == "overview"

    # Complete event emitted
    events = store.load_job_events("job-fin1")
    complete_events = [e for e in events if e["type"] == "complete"]
    assert len(complete_events) == 1
    ev = complete_events[0]
    assert ev["landingPageId"] == "overview"
    assert ev["pageCount"] == 3


# ── Test 2: unknown landingPageId → validation error ─────────────────────────


def test_finalize_rejects_unknown_landing_page(tmp_path: Path) -> None:
    """landingPageId that doesn't exist in pages list → validation error."""
    import mewbo_graph.plugins.wiki.finalize as mod
    from mewbo_graph.plugins.wiki.finalize import WikiFinalizeTool

    store = _store(tmp_path)
    job = _job("job-fin2", "org/repo")
    store.create_job(job)
    store.attach_job_session("job-fin2", "sess-fin2")
    _seed_submission(store, "job-fin2", slug="org/repo")

    _save_page(store, "org/repo", "intro")

    runtime = _fake_runtime(store)
    tool = WikiFinalizeTool(session_id="sess-fin2")

    with patch.object(mod, "_resolve_runtime", return_value=runtime):
        result = asyncio.run(tool.handle(_make_action_step({"landingPageId": "does-not-exist"})))

    assert "validation" in result.content

    # Job not modified
    updated_job = store.get_job("job-fin2")
    assert updated_job is not None
    assert updated_job.status == "finalizing"

    # No complete event
    events = store.load_job_events("job-fin2")
    assert not any(e["type"] == "complete" for e in events)


# ── Test 3: git snapshot + maintainer-edited propagate from job + clone dir ──


def test_finalize_propagates_branch_commit_and_maintainer_edited(
    tmp_path: Path, monkeypatch
) -> None:
    """clone-written branch+commit_sha land on Project; grounder presence flips badge."""
    import mewbo_graph.plugins.wiki.finalize as mod
    from mewbo_graph.plugins.wiki.finalize import WikiFinalizeTool

    # Anchor the clone-dir resolver at tmp_path so we can drop a grounder file.
    clone_root = tmp_path / "clones"
    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(clone_root))

    store = _store(tmp_path)
    store.create_job(_job("job-fin-meta", "org/repo"))
    # Mid-flight clone wrote these — finalize reads them off the job.
    store.update_job("job-fin-meta", branch="main", commit_sha="a1b2c3d4e5f6")
    store.attach_job_session("job-fin-meta", "sess-fin-meta")
    _seed_submission(store, "job-fin-meta", slug="org/repo")
    _save_page(store, "org/repo", "home")

    grounder = clone_root / "job-fin-meta" / ".mewbo" / "wiki.json"
    grounder.parent.mkdir(parents=True, exist_ok=True)
    grounder.write_text("{}", encoding="utf-8")

    runtime = _fake_runtime(store)
    tool = WikiFinalizeTool(session_id="sess-fin-meta")
    with patch.object(mod, "_resolve_runtime", return_value=runtime):
        result = asyncio.run(tool.handle(_make_action_step({"landingPageId": "home"})))

    assert "error" not in result.content
    project = store.get_project("org/repo")
    assert project is not None
    assert project.branch == "main"
    assert project.commit_sha == "a1b2c3d4e5f6"
    assert project.commit_short == "a1b2c3d"
    assert project.maintainer_edited is True


def test_finalize_without_grounder_or_snapshot_keeps_defaults(
    tmp_path: Path, monkeypatch
) -> None:
    """No grounder + no clone-supplied branch → maintainer_edited=False, fields None."""
    import mewbo_graph.plugins.wiki.finalize as mod
    from mewbo_graph.plugins.wiki.finalize import WikiFinalizeTool

    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))

    store = _store(tmp_path)
    store.create_job(_job("job-fin-bare", "org/bare"))
    store.attach_job_session("job-fin-bare", "sess-fin-bare")
    _seed_submission(store, "job-fin-bare", slug="org/bare")
    _save_page(store, "org/bare", "home")

    runtime = _fake_runtime(store)
    tool = WikiFinalizeTool(session_id="sess-fin-bare")
    with patch.object(mod, "_resolve_runtime", return_value=runtime):
        result = asyncio.run(tool.handle(_make_action_step({"landingPageId": "home"})))

    assert "error" not in result.content
    project = store.get_project("org/bare")
    assert project is not None
    assert project.branch is None
    assert project.commit_sha is None
    assert project.commit_short is None
    assert project.maintainer_edited is False


# ── Test 7: identity from persisted submission ───────────────────────────────


def test_finalize_uses_submission_platform_and_repo_url(tmp_path: Path) -> None:
    """Saved submission's `platform` wins over URL-host detection (DRY: wizard already chose it)."""
    import mewbo_graph.plugins.wiki.finalize as mod
    from mewbo_graph.plugins.wiki.finalize import WikiFinalizeTool

    store = _store(tmp_path)
    job = _job("job-fin7", "bearlike/Grove")
    store.create_job(job)
    store.attach_job_session("job-fin7", "sess-fin7")
    _save_page(store, "bearlike/Grove", "home")

    # Wizard submission persisted at job start; host is a private TLD that
    # URL-detection alone cannot map to gitea — the wizard's explicit choice
    # MUST win.
    store.save_job_submission("job-fin7", {
        "repoUrl": "https://git.hurricane.home/bearlike/Grove",
        "slug": "bearlike/Grove",
        "platform": "gitea",
        "language": "en",
        "depth": "concise",
        "model": "anthropic/claude-sonnet-4-6",
        "filterMode": "exclude",
        "dirs": [],
        "files": [],
    })

    runtime = _fake_runtime(store)
    tool = WikiFinalizeTool(session_id="sess-fin7")

    # Stub the description fetch so the test doesn't reach the network.
    with patch.object(mod, "_resolve_runtime", return_value=runtime), \
         patch.object(mod, "_fetch_description", return_value="A grove of git worktrees."):
        result = asyncio.run(tool.handle(_make_action_step({"landingPageId": "home"})))

    assert "error" not in result.content
    project = store.get_project("bearlike/Grove")
    assert project is not None
    assert project.source == "gitea"
    assert project.desc == "A grove of git worktrees."
    assert project.landing_page_id == "home"
    assert project.repo_url == "https://git.hurricane.home/bearlike/Grove"


def test_finalize_fetch_description_short_circuits_without_repo_url(tmp_path: Path) -> None:
    """No repo_url → no fetch, no error, desc stays empty."""
    from mewbo_graph.plugins.wiki.finalize import _fetch_description

    desc = _fetch_description(repo_url="", platform="github", token=None, slug="o/r")
    assert desc == ""


def test_finalize_overwrites_existing_project(tmp_path: Path) -> None:
    """Pre-seed a project for the slug → finalize → project replaced, no duplicate, no error."""
    import mewbo_graph.plugins.wiki.finalize as mod
    from mewbo_graph.plugins.wiki.finalize import WikiFinalizeTool
    from mewbo_graph.wiki.types import Project

    store = _store(tmp_path)
    job = _job("job-fin4", "org/repo")
    store.create_job(job)
    store.attach_job_session("job-fin4", "sess-fin4")
    _seed_submission(store, "job-fin4", slug="org/repo")

    _save_page(store, "org/repo", "landing")

    # Pre-seed a project with stale data
    stale = Project(
        slug="org/repo",
        source="github",
        lang="fr",
        indexedAt="2020-01-01T00:00:00Z",
        pages=0,
        desc="old",
    )
    store.create_project(stale)

    runtime = _fake_runtime(store)
    tool = WikiFinalizeTool(session_id="sess-fin4")

    with patch.object(mod, "_resolve_runtime", return_value=runtime):
        result = asyncio.run(tool.handle(_make_action_step({"landingPageId": "landing"})))

    assert "error" not in result.content

    # Only one project record (upsert, not insert)
    all_projects = store.list_projects()
    matching = [p for p in all_projects if p.slug == "org/repo"]
    assert len(matching) == 1

    # Data is updated (pages=1, not stale 0)
    project = matching[0]
    assert project.pages == 1
    assert project.indexed_at != "2020-01-01T00:00:00Z"


# ── Tests for the dedupe + refresh-keep-existing behaviour ────────────────────


def test_finalize_prunes_stale_pages_outside_committed_plan(tmp_path: Path) -> None:
    """Re-index drops pages from prior runs that aren't in the new plan."""
    import mewbo_graph.plugins.wiki.finalize as mod
    from mewbo_graph.plugins.wiki.finalize import WikiFinalizeTool

    store = _store(tmp_path)
    job = _job("job-prune", "org/repo")
    store.create_job(job)
    store.attach_job_session("job-prune", "sess-prune")
    _seed_submission(store, "job-prune", slug="org/repo")

    # Stale pages from a previous run that the new plan doesn't include.
    _save_page(store, "org/repo", "stale-auth-and-pairing")
    _save_page(store, "org/repo", "stale-auth-and-session-security")
    # Pages that the new plan does include.
    _save_page(store, "org/repo", "overview")
    _save_page(store, "org/repo", "auth")

    store.save_job_plan(
        "job-prune",
        [{"id": "overview", "title": "Overview"}, {"id": "auth", "title": "Auth"}],
    )

    runtime = _fake_runtime(store)
    tool = WikiFinalizeTool(session_id="sess-prune")

    with patch.object(mod, "_resolve_runtime", return_value=runtime):
        result = asyncio.run(tool.handle(_make_action_step({"landingPageId": "overview"})))

    assert "error" not in result.content

    surviving = {p.id for p in store.list_pages("org/repo")}
    assert surviving == {"overview", "auth"}

    project = store.get_project("org/repo")
    assert project is not None
    assert project.pages == 2


def test_finalize_keeps_existing_desc_when_refresh_lacks_token(tmp_path: Path) -> None:
    """Token-less refresh: keep prior description rather than overwrite with ""."""
    import mewbo_graph.plugins.wiki.finalize as mod
    from mewbo_graph.plugins.wiki.finalize import WikiFinalizeTool
    from mewbo_graph.wiki.types import Project

    store = _store(tmp_path)
    job = _job("job-keep", "org/repo")
    store.create_job(job)
    store.attach_job_session("job-keep", "sess-keep")
    _seed_submission(store, "job-keep", slug="org/repo")
    _save_page(store, "org/repo", "overview")

    # Pre-seed a project with a real description from a previous successful run.
    store.create_project(
        Project(
            slug="org/repo",
            source="github",
            lang="en",
            indexedAt="2026-01-01T00:00:00Z",
            pages=1,
            desc="The old description from a token-authed first index",
        )
    )

    runtime = _fake_runtime(store)
    tool = WikiFinalizeTool(session_id="sess-keep")

    # Force the description fetch to return "" — simulates a token-less
    # refresh hitting a private host that rejects anon API calls.
    with patch.object(mod, "_resolve_runtime", return_value=runtime), \
         patch.object(mod, "_fetch_description", return_value=""):
        result = asyncio.run(tool.handle(_make_action_step({"landingPageId": "overview"})))

    assert "error" not in result.content
    project = store.get_project("org/repo")
    assert project is not None
    assert project.desc == "The old description from a token-authed first index"
