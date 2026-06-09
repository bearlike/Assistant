"""WikiResume driver + /v1/wiki resume/recoverable routes (Gitea #54, Part B)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from mewbo_api.wiki.resume import WikiResume
from mewbo_graph.entities.types import Entity
from mewbo_graph.wiki.credentials import CredentialStore
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.tokens import CloneTokenCache
from mewbo_graph.wiki.types import (
    Frontmatter,
    GraphNode,
    IndexingJob,
    Project,
    RepoCredential,
    WikiPage,
)

# ── shared fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    return JsonWikiStore(root_dir=tmp_path / "wiki")


@pytest.fixture
def runtime(store):
    rt = MagicMock()
    rt.wiki_store = store
    rt.resolve_session.return_value = "sess-resume"
    rt.start_async.return_value = True
    return rt


def _node(slug, nid):
    return GraphNode(slug=slug, node_id=nid, type="Function", name="f", file="a.py", range=(0, 1))


def _page(pid):
    return WikiPage(
        id=pid, title=pid, frontmatter=Frontmatter(title=pid, slug=pid), body="x", toc=[], nav=[]
    )


def _seed_interrupted_at_pages(store, *, job_id="j-resume", slug="org/repo"):
    """Graph built + 6/7 plan pages written; job interrupted at the pages phase."""
    store.create_project(Project(
        slug=slug, source="gitea", lang="Python",
        indexedAt="2026-06-07T00:00:00Z", pages=1, desc="x",
    ))
    job = IndexingJob(
        jobId=job_id, slug=slug, status="interrupted",
        scannedCount=0, totalCount=0, currentFile=None,
        model="anthropic/claude-sonnet-4-6", commitSha="abc123",
    )
    store.create_job(job)
    store.save_job_submission(job_id, {
        "repoUrl": f"https://{slug}", "slug": slug, "platform": "gitea",
        "depth": "comprehensive", "language": "Python",
        "model": "anthropic/claude-sonnet-4-6", "filterMode": "exclude",
        "dirs": [], "files": [],
    })
    store.upsert_nodes(slug, [_node(slug, f"n{i}") for i in range(4)])
    store.upsert_entities(slug, [Entity(name="Widget", type="concept")])
    ids = [f"p{i}" for i in range(7)]
    store.save_job_plan(job_id, [{"id": pid, "title": pid} for pid in ids])
    for pid in ids:
        if pid != "p3":
            store.save_page(slug, _page(pid))
    return job_id, slug


# ── WikiResume.resume ───────────────────────────────────────────────────────────


def test_resume_reuses_job_id_and_persists_plan(store, runtime):
    job_id, slug = _seed_interrupted_at_pages(store)
    result = WikiResume.resume(store, runtime, job_id)

    # SAME job_id reused (continuous event log), running again.
    assert result["job_id"] == job_id
    assert result["session_id"] == "sess-resume"
    assert result["status"] == "scanning"
    assert store.get_job(job_id).status == "scanning"

    # The resume decision was persisted so per-tool-call ctx reads it cheaply.
    persisted = store.get_resume_plan(job_id)
    assert persisted is not None
    assert set(persisted["skip"]) == {"graph", "enrich", "plan"}
    assert persisted["pages_remaining"] == ["p3"]


def test_resume_readvertises_wiki_capability(store, runtime):
    """The resumed session re-advertises client_capabilities:["wiki"] (else the
    indexer can't spawn wiki-* AgentDefs → 'stuck after scan')."""
    job_id, _ = _seed_interrupted_at_pages(store)
    WikiResume.resume(store, runtime, job_id)

    cap_calls = [
        c for c in runtime.append_context_event.call_args_list
        if c.args[1] == {"client_capabilities": ["wiki"]}
    ]
    assert cap_calls, "resume must advertise the wiki capability"


def test_resume_restores_credential(store, runtime):
    """The durable per-slug credential is warmed into CloneTokenCache so the
    re-clone authenticates after the warming process is gone."""
    job_id, slug = _seed_interrupted_at_pages(store)
    CredentialStore.save(store, slug, RepoCredential(kind="token", value="ghp_dur", username=None))

    WikiResume.resume(store, runtime, job_id)
    assert CloneTokenCache.peek(job_id) == "ghp_dur"


def test_resume_injects_plan_summary_into_indexer(store, runtime):
    """The indexer is started with the ResumePlan summary so it skips done work."""
    job_id, _ = _seed_interrupted_at_pages(store)
    WikiResume.resume(store, runtime, job_id)

    kw = runtime.start_async.call_args.kwargs
    # Allowlist mirrors start() (shared seam).
    assert "wiki_build_graph" in kw["allowed_tools"]
    assert "spawn_agent" in kw["allowed_tools"]
    # Resume guidance rides the user_query task description (single injection —
    # NOT duplicated into skill_instructions, which stays the bare playbook).
    assert "RESUME" in kw["user_query"]
    assert "SKIP wiki_build_graph" in kw["user_query"]
    assert "p3" in kw["user_query"]
    # Commit-pinned re-clone (recorded SHA, not latest HEAD).
    assert "abc123" in kw["user_query"]


def test_resume_user_initiated_resets_recovery_cap(store, runtime):
    job_id, slug = _seed_interrupted_at_pages(store)
    store.bump_recovery_attempts(slug)
    store.bump_recovery_attempts(slug)
    assert store.get_recovery_attempts(slug) == 2

    WikiResume.resume(store, runtime, job_id, user_initiated=True)
    assert store.get_recovery_attempts(slug) == 0


def test_resume_automatic_does_not_reset_cap(store, runtime):
    job_id, slug = _seed_interrupted_at_pages(store)
    store.bump_recovery_attempts(slug)
    WikiResume.resume(store, runtime, job_id, user_initiated=False)
    assert store.get_recovery_attempts(slug) == 1


def test_resume_rejects_complete_job(store, runtime):
    store.create_job(IndexingJob(
        jobId="done", slug="org/repo", status="complete",
        scannedCount=0, totalCount=0, currentFile=None, commitSha="x",
    ))
    with pytest.raises(ValueError):
        WikiResume.resume(store, runtime, "done")


def test_resume_unknown_job_raises_keyerror(store, runtime):
    with pytest.raises(KeyError):
        WikiResume.resume(store, runtime, "nope")


def test_resume_idempotent_graph_skip_not_re_executed(store, runtime):
    """A resumed job whose graph exists never re-runs the graph build: the
    build tool short-circuits on the persisted skip decision (no parse)."""
    import asyncio
    from unittest.mock import patch

    from mewbo_graph.plugins.wiki import build_graph as bg

    job_id, slug = _seed_interrupted_at_pages(store)
    WikiResume.resume(store, runtime, job_id)

    # Rebuild the ctx the way a real tool call would (reads the persisted plan).
    store.attach_job_session(job_id, "sess-resume")
    tool = bg.WikiBuildGraphTool(session_id="sess-resume")
    tool_runtime = MagicMock(wiki_store=store)
    made_embedder = MagicMock()
    with patch.object(bg, "_resolve_runtime", return_value=tool_runtime), \
         patch.object(bg, "_make_embedder", return_value=made_embedder) as mk:
        result = asyncio.run(tool.handle(MagicMock(tool_input={})))

    body = str(result.content)
    assert "reused on resume" in body
    # The expensive embedder is never even constructed when graph is skipped.
    mk.assert_not_called()
