"""Checkpoint correctness — fail-closed resume, commit pinning, honest finalize.

Each test here pins a specific way the indexing pipeline used to lose or fake
work: a store read failure read as "nothing is built" (and rebuilt everything), a
commit pin that erased its own record, a resume that re-cloned a tree it already
had, a clone directory nothing ever reaped, and a finalize that reported success
with no pages. Only I/O is stubbed — the real ``ResumePlan`` / tool / store code
paths run.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from mewbo_graph.wiki.resume import ResumeCountError, ResumePlan
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import Frontmatter, IndexingJob, WikiPage, make_graph_node

_SHA = "a" * 40
_OTHER_SHA = "b" * 40


# ── Helpers ───────────────────────────────────────────────────────────────────


def _store(tmp_path: Path) -> JsonWikiStore:
    return JsonWikiStore(root_dir=tmp_path / "wiki")


def _job(
    store: JsonWikiStore,
    *,
    job_id: str = "job-1",
    slug: str = "org/repo",
    status: str = "interrupted",
    commit_sha: str | None = None,
    branch: str | None = None,
) -> IndexingJob:
    job = IndexingJob(
        jobId=job_id, slug=slug, status=status,
        scannedCount=0, totalCount=0, currentFile=None,
        commitSha=commit_sha, branch=branch,
    )
    store.create_job(job)
    return job


def _node(slug: str, nid: str):
    return make_graph_node(
        slug=slug, node_id=nid, type="Function", name="f", file="a.py", range=(0, 1)
    )


def _page(pid: str) -> WikiPage:
    return WikiPage(
        id=pid, title=pid, frontmatter=Frontmatter(title=pid, slug=pid),
        body="x", toc=[], nav=[],
    )


def _action_step(tool_input: dict) -> MagicMock:
    step = MagicMock()
    step.tool_input = tool_input
    return step


class _GitRecorder:
    """Stub for ``subprocess.run`` that records argvs and fakes a checkout.

    Populates the target directory on ``fetch``/``clone`` so the tool's own file
    count sees a real tree, and answers ``rev-parse HEAD`` with *head_sha* so a
    test can drive the pin-verification branch either way.
    """

    def __init__(self, clone_dir: Path, *, head_sha: str, detached: bool = True) -> None:
        self.clone_dir = clone_dir
        self.head_sha = head_sha
        self.detached = detached
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if "init" in cmd or "clone" in cmd:
            self._materialise()
        if "fetch" in cmd:
            self._materialise()
        if "rev-parse" in cmd:
            out = b"HEAD\n" if ("--abbrev-ref" in cmd and self.detached) else (
                self.head_sha.encode() + b"\n"
            )
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=out, stderr=b"")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=b"", stderr=b"")

    def _materialise(self) -> None:
        self.clone_dir.mkdir(parents=True, exist_ok=True)
        (self.clone_dir / ".git").mkdir(exist_ok=True)
        (self.clone_dir / "README.md").write_text("hello")

    def ran(self, token: str) -> bool:
        return any(token in call for call in self.calls)

    def argv_containing(self, token: str) -> list[str]:
        for call in self.calls:
            if token in call:
                return call
        raise AssertionError(f"no git call contained {token!r}: {self.calls}")


def _run_clone(store: JsonWikiStore, session: str, tool_input: dict, recorder):
    """Drive ``wiki_clone_repo`` against *store* with git stubbed by *recorder*."""
    import mewbo_graph.plugins.wiki.clone as clone_mod
    from mewbo_graph.plugins.wiki.clone import WikiCloneRepoTool

    tool = WikiCloneRepoTool(session_id=session)
    runtime = SimpleNamespace(wiki_store=store)
    with patch.object(clone_mod, "_resolve_runtime", return_value=runtime), \
         patch("subprocess.run", side_effect=recorder):
        return asyncio.run(tool.handle(_action_step(tool_input)))


# ── Artifact counts fail CLOSED ───────────────────────────────────────────────


def test_resume_refuses_when_graph_count_read_fails(tmp_path: Path) -> None:
    """A store read that RAISES must not be read as "nothing is built".

    The regression this pins: the count swallowed every exception and returned 0,
    so a transient read failure selected the full-rebuild branch — a complete
    re-index plus a re-embedding pass — silently and with no log line.
    """

    class _RaisingGraph(JsonWikiStore):
        def count_graph_nodes(self, slug, *, commit_sha):
            raise RuntimeError("transient store read failure")

    store = _RaisingGraph(root_dir=tmp_path / "wiki")
    job = _job(store)
    # The graph IS populated; the read is what fails. A returned plan here would
    # have thrown away real work.
    store.upsert_nodes("org/repo", [_node("org/repo", "n1")])

    with pytest.raises(ResumeCountError):
        ResumePlan.build(store, job)


def test_resume_refuses_when_entity_count_read_fails(tmp_path: Path) -> None:
    class _RaisingEntities(JsonWikiStore):
        def count_entities(self, slug, *, commit_sha):
            raise RuntimeError("transient store read failure")

    store = _RaisingEntities(root_dir=tmp_path / "wiki")
    job = _job(store)
    with pytest.raises(ResumeCountError):
        ResumePlan.build(store, job)


def test_genuinely_empty_graph_still_selects_a_rebuild(tmp_path: Path) -> None:
    """The negative control: "counted zero" is a real answer and still rebuilds.

    Fail-closed must not degrade into fail-always — a first-ever resume of a job
    that built nothing has to keep working.
    """
    store = _store(tmp_path)
    job = _job(store)
    plan = ResumePlan.build(store, job)
    assert plan.skip == frozenset()
    assert plan.is_noop()


# ── A restart is not a resume ─────────────────────────────────────────────────


def test_restart_and_empty_resume_render_different_intents() -> None:
    """Both plans skip nothing; only one of them is discarding work on purpose."""
    restart = ResumePlan.for_restart().summary()
    resume = ResumePlan().summary()

    assert restart.startswith("RESTART")
    assert resume.startswith("RESUME")
    assert "reuse completed work" not in restart
    assert restart != resume


def test_restart_flag_survives_the_resume_sidecar() -> None:
    """The intent has to reach the phase tools, which rebuild from the sidecar."""
    round_tripped = ResumePlan.from_persisted(ResumePlan.for_restart().to_persisted())
    assert round_tripped is not None
    assert round_tripped.restart is True
    assert round_tripped.summary().startswith("RESTART")


# ── Server-side commit pinning ────────────────────────────────────────────────


def test_pinned_job_fetches_the_commit_and_ignores_the_model_supplied_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recorded commit is the pin — not whatever ref the model passed.

    ``git clone --branch`` resolves its argument as a branch name on the remote,
    so a sha there always failed and the model then re-cloned unpinned. The pin
    now comes off the job and drives init + fetch + checkout FETCH_HEAD.
    """
    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    store = _store(tmp_path)
    _job(store, job_id="job-pin", commit_sha=_SHA, branch="main")
    store.attach_job_session("job-pin", "sess-pin")
    recorder = _GitRecorder(tmp_path / "clones" / "job-pin", head_sha=_SHA)

    result = _run_clone(
        store, "sess-pin",
        {"url": "https://git.example.com/org/repo", "ref": "some-other-branch"},
        recorder,
    )

    assert "error" not in result.content
    # The pinned commit was fetched; no --branch clone was attempted at all.
    assert not recorder.ran("clone")
    fetch = recorder.argv_containing("fetch")
    assert fetch[-1] == _SHA
    assert "--depth=1" in fetch
    assert "some-other-branch" not in fetch
    # Hardened posture is carried on the pinned path too.
    assert "credential.helper=" in fetch
    assert recorder.ran("checkout")
    assert "FETCH_HEAD" in recorder.argv_containing("checkout")


def test_pinned_job_keeps_its_commit_and_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verified pin is never re-recorded, and the detached HEAD keeps the branch."""
    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    store = _store(tmp_path)
    _job(store, job_id="job-keep", commit_sha=_SHA, branch="main")
    store.attach_job_session("job-keep", "sess-keep")
    recorder = _GitRecorder(tmp_path / "clones" / "job-keep", head_sha=_SHA)

    _run_clone(store, "sess-keep", {"url": "https://git.example.com/org/repo"}, recorder)

    job = store.get_job("job-keep")
    assert job is not None
    assert job.commit_sha == _SHA
    assert job.branch == "main"  # not clobbered by the detached checkout
    assert job.status == "scanning"


def test_writeback_mismatch_fails_instead_of_overwriting_the_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pin used to erase its own record. A mismatch is now a hard failure.

    Previously the tool wrote whatever HEAD it landed on straight over
    ``commit_sha``, so a pin that silently failed left no evidence it had ever
    been requested.
    """
    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    store = _store(tmp_path)
    _job(store, job_id="job-drift", commit_sha=_SHA)
    store.attach_job_session("job-drift", "sess-drift")
    # The checkout lands somewhere other than the pinned commit.
    recorder = _GitRecorder(tmp_path / "clones" / "job-drift", head_sha=_OTHER_SHA)

    result = _run_clone(
        store, "sess-drift", {"url": "https://git.example.com/org/repo"}, recorder
    )

    assert "repo_access" in result.content
    job = store.get_job("job-drift")
    assert job is not None
    assert job.commit_sha == _SHA  # the record of what was pinned survives
    assert job.status == "failed"


def test_unpinned_first_clone_still_uses_the_branch_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nothing pinned yet, the model's ref still selects the branch."""
    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    store = _store(tmp_path)
    _job(store, job_id="job-fresh", status="queued")
    store.attach_job_session("job-fresh", "sess-fresh")
    recorder = _GitRecorder(
        tmp_path / "clones" / "job-fresh", head_sha=_SHA, detached=False
    )

    _run_clone(
        store, "sess-fresh",
        {"url": "https://git.example.com/org/repo", "ref": "develop"},
        recorder,
    )

    clone = recorder.argv_containing("clone")
    assert "--branch" in clone
    assert "develop" in clone
    assert not recorder.ran("fetch")
    # A first clone DOES record what it landed on — that is how the pin is born.
    job = store.get_job("job-fresh")
    assert job is not None
    assert job.commit_sha == _SHA


# ── Reusing a checkout already at the pinned commit ───────────────────────────


def test_resume_reuses_a_clone_already_at_the_pinned_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every resume turn used to wipe and re-fetch a tree it already had."""
    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    store = _store(tmp_path)
    _job(store, job_id="job-reuse", commit_sha=_SHA, branch="main")
    store.attach_job_session("job-reuse", "sess-reuse")

    clone_dir = tmp_path / "clones" / "job-reuse"
    (clone_dir / ".git").mkdir(parents=True)
    (clone_dir / "README.md").write_text("already here")
    recorder = _GitRecorder(clone_dir, head_sha=_SHA)

    result = _run_clone(
        store, "sess-reuse", {"url": "https://git.example.com/org/repo"}, recorder
    )

    assert "'reused': True" in result.content
    assert not recorder.ran("clone")
    assert not recorder.ran("fetch")
    assert not recorder.ran("init")
    job = store.get_job("job-reuse")
    assert job is not None
    assert job.status == "scanning"
    assert job.total_count == 1


def test_clone_dir_at_the_wrong_commit_is_re_cloned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reuse is exact-match only — a stale checkout must not be trusted."""
    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    store = _store(tmp_path)
    _job(store, job_id="job-stale", commit_sha=_SHA)
    store.attach_job_session("job-stale", "sess-stale")

    clone_dir = tmp_path / "clones" / "job-stale"
    (clone_dir / ".git").mkdir(parents=True)
    # rev-parse answers the pinned sha only AFTER the re-fetch; the pre-check
    # sees the stale one.
    recorder = _GitRecorder(clone_dir, head_sha=_OTHER_SHA)
    recorder.head_sha = _OTHER_SHA

    _run_clone(store, "sess-stale", {"url": "https://git.example.com/org/repo"}, recorder)

    assert recorder.ran("fetch")


# ── Clone directories are reaped at a job-terminal seam ───────────────────────


def test_finalize_reaps_superseded_clone_dirs_and_keeps_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing reaped these; one directory accumulated per re-index and resume."""
    from mewbo_graph.plugins.wiki.finalize import _supersede_stale_jobs

    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    store = _store(tmp_path)
    _job(store, job_id="job-old-done", status="complete")
    _job(store, job_id="job-old-stuck", status="scanning")
    _job(store, job_id="job-live", status="complete")
    _job(store, job_id="job-elsewhere", slug="org/other", status="complete")

    for job_id in ("job-old-done", "job-old-stuck", "job-live", "job-elsewhere"):
        (tmp_path / "clones" / job_id).mkdir(parents=True)

    ctx = SimpleNamespace(store=store, slug="org/repo", job_id="job-live")
    _supersede_stale_jobs(ctx)

    assert not (tmp_path / "clones" / "job-old-done").exists()
    assert not (tmp_path / "clones" / "job-old-stuck").exists()
    # The just-completed job keeps its checkout — Q&A source reads resolve to it.
    assert (tmp_path / "clones" / "job-live").is_dir()
    # Another project's job is out of scope for this slug's sweep.
    assert (tmp_path / "clones" / "job-elsewhere").is_dir()
    # The stuck sibling is still retired on status, as before.
    stuck = store.get_job("job-old-stuck")
    assert stuck is not None
    assert stuck.status == "failed"


# ── Finalize asserts its own outcome ──────────────────────────────────────────


def test_finalize_with_zero_pages_does_not_report_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``page_count`` was computed, persisted and logged — but never checked."""
    import mewbo_graph.plugins.wiki.finalize as finalize_mod
    from mewbo_graph.plugins.wiki.finalize import WikiFinalizeTool

    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    store = _store(tmp_path)
    _job(store, job_id="job-empty", status="finalizing")
    store.attach_job_session("job-empty", "sess-empty")
    # A populated graph, so the failure can only be attributed to the page count.
    store.upsert_nodes("org/repo", [_node("org/repo", "n1")])

    tool = WikiFinalizeTool(session_id="sess-empty")
    runtime = SimpleNamespace(wiki_store=store)
    with patch.object(finalize_mod, "_resolve_runtime", return_value=runtime):
        result = asyncio.run(tool.handle(_action_step({"landingPageId": "overview"})))

    assert "error" in result.content
    assert store.get_project("org/repo") is None
    job = store.get_job("job-empty")
    assert job is not None
    assert job.status == "failed"
    assert not [e for e in store.load_job_events("job-empty") if e["type"] == "complete"]
    # And it does NOT ask the loop to stop on a successful terminal state.
    assert tool.should_terminate_run() is False


def test_graph_check_fails_closed_on_an_unreadable_store(tmp_path: Path) -> None:
    """An unreadable graph is not a populated one — it is an unanswered question.

    Returning True here let a transient store error launder a possibly-empty
    graph into a ``complete`` index.
    """
    from mewbo_graph.plugins.wiki.finalize import _graph_is_populated

    class _Raising:
        def query_graph(self, slug, **kwargs):
            raise RuntimeError("transient store read failure")

    assert _graph_is_populated(SimpleNamespace(store=_Raising(), slug="org/repo")) is False


def test_graph_check_still_allows_a_graph_less_install(tmp_path: Path) -> None:
    """The deliberate carve-out: no graph BACKEND is a known gap, not uncertainty."""
    from mewbo_graph.plugins.wiki.finalize import _graph_is_populated

    class _NoBackend:
        def query_graph(self, slug, **kwargs):
            raise NotImplementedError("graph backend absent")

    assert _graph_is_populated(SimpleNamespace(store=_NoBackend(), slug="org/repo")) is True


# ── A phase is stamped by work that started ───────────────────────────────────


def test_build_graph_skip_path_does_not_claim_the_enrich_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stamping a successor on the way out reported work that never began."""
    import mewbo_graph.plugins.wiki.build_graph as bg_mod
    from mewbo_graph.plugins.wiki.build_graph import WikiBuildGraphTool

    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    store = _store(tmp_path)
    _job(store, job_id="job-skip", status="scanning")
    store.attach_job_session("job-skip", "sess-skip")
    store.save_resume_plan(
        "job-skip", ResumePlan(skip=frozenset({"graph"}), node_count=7).to_persisted()
    )

    tool = WikiBuildGraphTool(session_id="sess-skip")
    runtime = SimpleNamespace(wiki_store=store)
    with patch.object(bg_mod, "_resolve_runtime", return_value=runtime):
        asyncio.run(tool.handle(_action_step({})))

    phases = [e["name"] for e in store.load_job_events("job-skip") if e["type"] == "phase"]
    assert phases == ["graph"]
    job = store.get_job("job-skip")
    assert job is not None
    assert job.phase == "graph"


def test_minting_is_what_marks_the_enrich_phase_as_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The enrich fan-out stamps its own phase, on real work rather than on a gap."""
    from mewbo_graph.entities.types import EntityEmbedding
    from mewbo_graph.plugins.wiki import mint_entity as mint_mod

    class _FakeEmbedder:
        model = "fake"

        def embed_query(self, text):
            return [1.0, 0.0]

        def embed_nodes(self, items, *, slug=""):
            return [
                EntityEmbedding(
                    slug=slug, entity_id=nid, vector=[1.0, 0.0], model="fake", dim=2
                )
                for nid, _t in items
            ]

    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    store = _store(tmp_path)
    _job(store, job_id="job-mint", status="scanning")
    ctx = SimpleNamespace(
        slug="org/repo", store=store, session_id="sess-mint", job_id="job-mint",
        clone_dir=None,
    )

    tool = mint_mod.MintEntityTool(session_id="sess-mint")
    with patch.object(mint_mod, "_ctx_for", return_value=ctx), \
         patch.object(mint_mod, "_make_embedder", return_value=_FakeEmbedder()):
        for name in ("RetryStrategy", "RetryStrategy"):
            asyncio.run(tool.handle(_action_step({"name": name, "type": "concept"})))

    phases = [e["name"] for e in store.load_job_events("job-mint") if e["type"] == "phase"]
    assert phases == ["enrich"]
    job = store.get_job("job-mint")
    assert job is not None
    assert job.phase == "enrich"


def test_commit_plan_does_not_claim_the_pages_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Committing a plan is not the same event as writing one."""
    import mewbo_graph.plugins.wiki.commit_plan as cp_mod
    from mewbo_graph.plugins.wiki.commit_plan import WikiCommitPlanTool

    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    store = _store(tmp_path)
    _job(store, job_id="job-plan", status="scanning")
    store.attach_job_session("job-plan", "sess-plan")

    tool = WikiCommitPlanTool(session_id="sess-plan")
    runtime = SimpleNamespace(wiki_store=store)
    with patch.object(cp_mod, "_resolve_runtime", return_value=runtime):
        asyncio.run(tool.handle(_action_step(
            {"pages": [{"id": "overview", "title": "Overview", "relevantFiles": []}]}
        )))

    phases = [e["name"] for e in store.load_job_events("job-plan") if e["type"] == "phase"]
    assert phases == ["plan"]


def test_writing_a_page_is_what_marks_the_pages_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page-writer fan-out stamps its own phase, exactly once."""
    import mewbo_graph.plugins.wiki.submit_page as sp_mod
    from mewbo_graph.plugins.wiki.submit_page import WikiSubmitPageTool

    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    store = _store(tmp_path)
    _job(store, job_id="job-write", status="finalizing")
    store.attach_job_session("job-write", "sess-write")

    tool = WikiSubmitPageTool(session_id="sess-write")
    runtime = SimpleNamespace(wiki_store=store)
    with patch.object(sp_mod, "_resolve_runtime", return_value=runtime):
        for pid in ("overview", "architecture"):
            asyncio.run(tool.handle(_action_step(
                {"pageId": pid, "frontmatter": {"title": pid}, "body": "# body"}
            )))

    phases = [e["name"] for e in store.load_job_events("job-write") if e["type"] == "phase"]
    assert phases == ["pages"]
    job = store.get_job("job-write")
    assert job is not None
    assert job.phase == "pages"


# ── The fallback ladder must survive the refresh path ─────────────────────────


def test_fallback_ladder_survives_submission_settings_submission() -> None:
    """``refresh`` rebuilds its submission from the settings record.

    So a ladder the record cannot carry is dropped from every index after the
    first — silently, and for a project that WAS onboarded with one.
    """
    from mewbo_graph.wiki.types import ProjectSettings, WizardSubmission

    base = {
        "repoUrl": "https://git.example.com/acme/beacon", "slug": "acme/beacon",
        "platform": "gitea", "depth": "concise", "language": "en", "model": "m",
        "filterMode": "exclude", "dirs": [], "files": [],
    }
    sub = WizardSubmission.model_validate({**base, "fallbackModels": ["a", "b"]})
    assert ProjectSettings.from_submission(sub).to_submission().fallback_models == ["a", "b"]

    # None (inherit the configured policy) and [] (explicitly empty) are
    # different answers and must not collapse into each other.
    none_sub = WizardSubmission.model_validate(base)
    assert ProjectSettings.from_submission(none_sub).to_submission().fallback_models is None
    empty = WizardSubmission.model_validate({**base, "fallbackModels": []})
    assert ProjectSettings.from_submission(empty).to_submission().fallback_models == []


# ── agentic_search args: two verified validation misses ───────────────────────


def test_agentic_search_accepts_the_cross_surface_workspace_id_spelling() -> None:
    """The REST contract calls it ``workspace_id``; a model that has seen both
    surfaces plausibly sends that, and extra="forbid" turned it into a hard miss."""
    from mewbo_graph.plugins.scg.search import AgenticSearchArgs, AgenticSearchTool

    parsed = AgenticSearchArgs.model_validate({"query": "q", "workspace_id": "ws1"})
    assert parsed.workspace == "ws1"
    assert AgenticSearchArgs.model_validate({"query": "q", "workspace": "ws1"}).workspace == "ws1"
    # The PUBLISHED schema still advertises exactly one canonical spelling.
    props = AgenticSearchTool.schema["function"]["parameters"]["properties"]
    assert "workspace" in props
    assert "workspace_id" not in props


def test_agentic_search_tier_is_case_insensitive() -> None:
    """`"Deep"` hard-failed with no hint; case carries no meaning here."""
    from mewbo_graph.plugins.scg.search import AgenticSearchArgs

    assert AgenticSearchArgs.model_validate({"query": "q", "tier": "Deep"}).tier == "deep"
    assert AgenticSearchArgs.model_validate({"query": "q", "tier": " AUTO "}).tier == "auto"
    # A genuinely unknown tier is still refused — normalisation, not permissiveness.
    with pytest.raises(Exception):
        AgenticSearchArgs.model_validate({"query": "q", "tier": "turbo"})


def test_agentic_search_declares_the_poll_exemption() -> None:
    """A FETCH is honest waiting; a repeated START still counts toward progress.

    The tool declares itself poll-class by argument shape and the loop reads it
    via ``getattr``; this reproduces that read + the guard's matching decision,
    so the declaration can never silently drop off the class.
    """
    from mewbo_core.llm_resilience import PollClassRule
    from mewbo_graph.plugins.scg.search import AgenticSearchTool

    when_args = tuple(getattr(AgenticSearchTool, "poll_when_args", ()) or ())
    assert when_args == ("run_id",)

    rule = PollClassRule(tool_id="agentic_search", when_args=frozenset(when_args))
    assert rule.matches("agentic_search", {"run_id": "abc:r1"}) is True   # fetch → exempt
    assert rule.matches("agentic_search", {"query": "how does X"}) is False  # start → counts
    # A key present but empty is not a poll — matching the exactly-one validator.
    assert rule.matches("agentic_search", {"run_id": None}) is False
    assert rule.matches("agentic_search", {"run_id": ""}) is False


def test_agentic_search_keeps_its_load_bearing_guards() -> None:
    """The two rules that were already correct must survive the relaxation."""
    from mewbo_graph.plugins.scg.search import AgenticSearchArgs

    with pytest.raises(Exception):  # extra="forbid"
        AgenticSearchArgs.model_validate({"query": "q", "bogus": 1})
    with pytest.raises(Exception):  # exactly one of query / run_id
        AgenticSearchArgs.model_validate({"query": "q", "run_id": "r"})
    with pytest.raises(Exception):
        AgenticSearchArgs.model_validate({})


def test_emit_phase_once_advances_only_the_first_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The enrich fan-out has many workers and must produce one phase event."""
    from mewbo_graph.plugins.wiki._ctx import emit_phase_once

    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    store = _store(tmp_path)
    _job(store, job_id="job-fan", status="scanning")
    ctx = SimpleNamespace(store=store, job_id="job-fan", slug="org/repo")

    for _ in range(4):
        emit_phase_once(ctx, "enrich")

    phases = [e["name"] for e in store.load_job_events("job-fan") if e["type"] == "phase"]
    assert phases == ["enrich"]


# ── The submission-level fallback ladder field ────────────────────────────────


def test_wizard_submission_carries_a_fallback_ladder() -> None:
    """Optional and defaulted, so every existing submission validates unchanged."""
    from mewbo_graph.wiki.types import WizardSubmission

    base = {
        "repoUrl": "https://git.example.com/org/repo", "slug": "org/repo",
        "platform": "gitea", "depth": "concise", "language": "en",
        "model": "m", "filterMode": "exclude", "dirs": [], "files": [],
    }
    assert WizardSubmission.model_validate(base).fallback_models is None
    # Accepted by wire alias and by field name (populate_by_name).
    assert WizardSubmission.model_validate(
        {**base, "fallbackModels": ["a", "b"]}
    ).fallback_models == ["a", "b"]
    assert WizardSubmission.model_validate(
        {**base, "fallback_models": ["a"]}
    ).fallback_models == ["a"]
