"""``ScopedRefreshRunner`` — the deterministic Free tier, driven end to end.

Every test here runs the REAL runner over a REAL tree-sitter parse; the two I/O
boundaries are stubbed exactly as ``test_graph_only_indexer.py`` stubs them (git
clone via ``subprocess.run`` populating the clone dir, and the embedder switched
off). Nothing here injects a fake orchestrator: the whole point of the scoped
path is which store rows survive it, and a stubbed delta stage cannot answer
that.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from mewbo_graph.plugins.wiki import _jobless as jobless_mod
from mewbo_graph.plugins.wiki._ctx import build_jobless_ctx, emit_scope_preview
from mewbo_graph.plugins.wiki.scoped_refresh import ScopedRefreshRunner
from mewbo_graph.wiki.memory_types import FileManifest
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import (
    CommitScope,
    IndexingJob,
    Project,
    ScopePreview,
    WizardSubmission,
    make_graph_node,
)

from .conftest import FakeEmbedder

SLUG = "example.com/o/r"
OLD_COMMIT = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
NEW_COMMIT = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

# The two-file working tree every test refreshes. ``kept.py`` is byte-identical
# to what the seeded manifest records, so the change detector must never mark it
# dirty — it is the file whose survival the supersede regression turns on.
KEPT_SOURCE = "def untouched():\n    return 1\n"
CHANGED_BEFORE = "def moved():\n    return 2\n"
CHANGED_AFTER = "def moved():\n    return 2\n\n\ndef added_later():\n    return 3\n"


def _submission() -> WizardSubmission:
    return WizardSubmission.model_validate({
        "repoUrl": "https://example.com/o/r",
        "slug": SLUG,
        "platform": "git",
        "depth": "comprehensive",
        "language": "en",
        "model": "unused-in-a-scoped-refresh",
        "filterMode": "exclude",
        "dirs": [],
        "files": [],
    })


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _seed_prior_index(store: JsonWikiStore, *, changed_body: str) -> None:
    """Persist the state a completed FULL index at ``OLD_COMMIT`` would leave.

    Nodes stamped with the old commit, a manifest whose hashes match the bytes
    that index saw, and a Project record carrying display facts (pages, desc,
    landing page) that a scoped refresh must not invent replacements for.
    """
    store.upsert_nodes(
        SLUG,
        [
            make_graph_node(
                slug=SLUG, node_id="kept-file", type="File",
                name="kept.py", file="kept.py", range=(0, len(KEPT_SOURCE)),
            ),
            make_graph_node(
                slug=SLUG, node_id="kept-fn", type="Function",
                name="untouched", file="kept.py", range=(0, len(KEPT_SOURCE)),
            ),
            make_graph_node(
                slug=SLUG, node_id="changed-file", type="File",
                name="changed.py", file="changed.py", range=(0, len(changed_body)),
            ),
        ],
        commit_sha=OLD_COMMIT,
    )
    store.upsert_file_manifest(
        SLUG,
        [
            FileManifest(
                slug=SLUG, path="kept.py", content_hash=_hash(KEPT_SOURCE),
                last_indexed_commit=OLD_COMMIT, entity_keys=["kept.py", "kept.py#untouched"],
            ),
            FileManifest(
                slug=SLUG, path="changed.py", content_hash=_hash(changed_body),
                last_indexed_commit=OLD_COMMIT, entity_keys=["changed.py"],
            ),
        ],
    )
    store.create_project(Project(
        slug=SLUG, source="git", lang="en", indexedAt="2020-01-01T00:00:00Z",
        pages=7, desc="the description a maintainer edited",
        landingPageId="overview", repoUrl="https://example.com/o/r",
        branch="main", commitSha=OLD_COMMIT, commitShort=OLD_COMMIT[:7],
    ))


@pytest.fixture
def refresh(tmp_path, monkeypatch):
    """Return ``run(changed_body, embedder=None) -> store`` for one real refresh.

    *changed_body* is what ``changed.py`` holds on disk AT REFRESH TIME. Passing
    :data:`CHANGED_AFTER` makes it dirty; passing :data:`CHANGED_BEFORE` (what
    the seeded manifest hashed) leaves the whole tree clean, which is the no-op
    case.

    *embedder* is what the DEFAULT composer would resolve, not something handed
    to the runner: it is installed behind ``make_embedder_or_none`` + the
    operator switch, because the property worth testing is that the runner
    reaches that resolution at all. ``None`` switches embedding off through the
    same operator setting a deployment would use.
    """
    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    # The clone dir is not a git repository, so the real ``rev-parse`` would run
    # against the stubbed ``subprocess.run`` and mkdir garbage from its argv.
    monkeypatch.setattr(
        jobless_mod, "_git_rev_parse",
        lambda d, args: NEW_COMMIT if args == ["HEAD"] else "main",
    )

    def _run(changed_body: str, *, embedder=None) -> JsonWikiStore:
        monkeypatch.setattr(
            "mewbo_graph.wiki.embedder.Embedder.enabled",
            staticmethod(lambda: embedder is not None),
        )
        monkeypatch.setattr(
            "mewbo_graph.wiki.embedder.make_embedder_or_none",
            lambda model=None: embedder,
        )
        store = JsonWikiStore(root_dir=tmp_path / "wiki")
        store.create_job(IndexingJob(
            jobId="j-refresh", slug=SLUG, status="queued",
            scannedCount=0, totalCount=0, currentFile=None,
        ))
        _seed_prior_index(store, changed_body=CHANGED_BEFORE)

        def _fake_clone(cmd, *a, **kw):
            dest = Path(cmd[-1])
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "kept.py").write_text(KEPT_SOURCE, encoding="utf-8")
            (dest / "changed.py").write_text(changed_body, encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        monkeypatch.setattr(subprocess, "run", _fake_clone)
        ctx = build_jobless_ctx(job_id="j-refresh", slug=SLUG, store=store)
        ScopedRefreshRunner(ctx, _submission()).run()
        return store

    return _run


def _files_in_graph(store: JsonWikiStore) -> set[str]:
    return {n.file for n in store.query_graph(SLUG, scope=CommitScope.every())}


# ── the supersede regression ────────────────────────────────────────────────


def test_scoped_refresh_leaves_untouched_files_in_the_graph(refresh) -> None:
    """The untouched majority of the graph must survive a scoped refresh.

    This is the defect the incremental path exists to avoid, and the shape it
    would take if someone "restored the missing ``supersede_graph_artifacts``
    call" in ``ScopedRefreshRunner._finalize`` to match the other two
    finalizers. That reaper deletes every row NOT stamped with the kept commit;
    a scoped pass re-stamps ONLY what it re-parsed, so ``kept.py`` still carries
    the OLD commit and would be reaped along with it — silently, since the job
    still completes and the project still renders.

    Asserted on the surviving NODES rather than on the absence of the call, so
    it fails for a re-introduction by any route, including a store-side one.
    """
    store = refresh(CHANGED_AFTER)

    job = store.get_job("j-refresh")
    assert job is not None and job.status == "complete"
    assert job.progress is not None
    scan = {record.key: record for record in job.progress.steps_in("scan")}
    assert set(scan) == {
        "scan.discover",
        "scan.inspect_files",
        "scan.persist_manifest",
    }
    assert all(record.state == "done" for record in scan.values())
    assert scan["scan.inspect_files"].current == scan["scan.inspect_files"].total == 2
    files = _files_in_graph(store)
    assert "kept.py" in files, (
        "the untouched file's nodes were reaped — a commit-scoped supersede ran "
        "after a refresh that only re-stamped the changed scope"
    )
    assert "changed.py" in files
    # And the survivors really are the OLD-commit rows: nothing re-stamped them,
    # which is exactly why a supersede sweep here would have taken them.
    kept = [n for n in store.query_graph(SLUG, scope=CommitScope.every()) if n.file == "kept.py"]
    assert {n.node_id for n in kept} == {"kept-file", "kept-fn"}


def test_scoped_refresh_reparses_only_the_changed_file(refresh) -> None:
    """The changed file is re-parsed (fresh node ids); the clean one is not."""
    store = refresh(CHANGED_AFTER)

    changed = {
        n.name
        for n in store.query_graph(SLUG, scope=CommitScope.every())
        if n.file == "changed.py"
    }
    # The seeded stand-in node is gone, replaced by a real tree-sitter parse
    # that sees the function added since the last index.
    assert "added_later" in changed
    assert "changed-file" not in {
        n.node_id
        for n in store.query_graph(SLUG, scope=CommitScope.every())
        if n.file == "changed.py"
    }


# ── the published scope ─────────────────────────────────────────────────────


def _preview_event(store: JsonWikiStore) -> dict:
    events = store.load_job_events("j-refresh", after_idx=-1)
    matches = [e for e in events if e.get("type") == "scope_preview"]
    assert matches, "the refresh published no scope_preview event"
    return matches[-1]


def test_scoped_refresh_publishes_its_scope_on_both_transports(refresh) -> None:
    """One write, two transports — the event and the snapshot cannot disagree.

    The live indexing timeline reads the event stream and the landing card polls
    the snapshot, so a preview reaching only one of them is a surface reporting
    a scope that never happened. ``emit_scope_preview`` is the single writer;
    this pins that both legs land from one call.
    """
    store = refresh(CHANGED_AFTER)

    snapshot = store.get_job("j-refresh").scope_preview
    assert isinstance(snapshot, ScopePreview)
    event = _preview_event(store)
    # ``type`` and the store's own ``idx`` are envelope, not payload — every
    # count the model carries must match, in the aliases the console reads.
    counts = snapshot.model_dump(by_alias=True)
    assert {k: event.get(k) for k in counts} == counts
    assert set(event) - set(counts) == {"type", "idx"}


def test_scope_preview_counts_the_change_it_actually_made(refresh) -> None:
    """The published counts describe this diff, not a generic non-zero shape."""
    store = refresh(CHANGED_AFTER)
    preview = store.get_job("j-refresh").scope_preview

    assert preview.files_modified == 1
    assert preview.files_added == 0
    assert preview.files_deleted == 0
    # Deterministic tier: the memory reconciler's drift band is the only stage
    # that can reach a model, and nothing here is anchored to the changed scope.
    assert preview.llm_calls == 0
    assert preview.affected_entities >= 1


def test_emit_scope_preview_writes_the_event_and_the_snapshot(tmp_path) -> None:
    """The emitter's own contract, isolated from a run that happens to call it.

    Driven directly because the property is about the WRITER, not the refresh:
    a future caller wiring only one leg by hand is the failure this guards, and
    a test that only ever reaches the emitter through ``run()`` would not see it.
    """
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    store.create_job(IndexingJob(
        jobId="j-emit", slug=SLUG, status="queued",
        scannedCount=0, totalCount=0, currentFile=None,
    ))
    ctx = build_jobless_ctx(job_id="j-emit", slug=SLUG, store=store)
    preview = ScopePreview(
        filesAdded=1, filesModified=2, filesDeleted=3, earlyCutoffFiles=4,
        affectedEntities=5, memoryKept=6, memoryInvalidated=7, memoryRevalidated=8,
        pagesKeep=9, pagesEdit=10, pagesRegenerate=11, newPages=12, llmCalls=13,
    )

    emit_scope_preview(ctx, preview)

    assert store.get_job("j-emit").scope_preview == preview
    events = [
        e for e in store.load_job_events("j-emit", after_idx=-1)
        if e.get("type") == "scope_preview"
    ]
    assert len(events) == 1
    assert events[0]["filesModified"] == 2
    assert events[0]["llmCalls"] == 13


# ── the embedder must survive the trip through the runner ───────────────────


def test_the_runner_vectorises_the_scope_it_reparsed(refresh) -> None:
    """The default composer's embedder has to REACH the delta stage from here.

    ``GraphDeltaIndexer`` deletes a file's vectors along with its nodes before
    it would write new ones, so a runner that arrived at ``from_store`` in a
    state where nothing resolves does not "leave things alone" — it strips the
    vectors off every file the refresh touched, silently, with the job still
    reporting complete.

    ``test_refresh_orchestrator.py`` pins that ``from_store`` resolves a default
    embedder. This pins the other half, which no orchestrator test can see: that
    the runner reaches that resolution rather than defeating it. Nothing is
    injected into the runner — the fake is installed behind the same
    ``make_embedder_or_none`` seam a deployment resolves through.
    """
    store = refresh(CHANGED_AFTER, embedder=FakeEmbedder())

    reparsed = {
        n.node_id
        for n in store.query_graph(SLUG, scope=CommitScope.every())
        if n.file == "changed.py"
    }
    vectorised = {e.node_id for e in store.vector_search(SLUG, [1.0, 0.0, 0.0, 0.0], k=100)}
    assert reparsed, "the changed file produced no nodes — the parse itself failed"
    assert reparsed <= vectorised, (
        "the re-parsed scope lost its vectors: the delta stage retracted them "
        "and no embedder reached it to write them back"
    )


# ── the no-change path ──────────────────────────────────────────────────────


def test_a_clean_tree_is_a_no_op_that_still_advances_the_commit(refresh) -> None:
    """Nothing changed ⇒ nothing re-indexed, but the wiki still moves forward.

    The commit advance is the load-bearing half: without it the project keeps
    claiming the older sha, every freshness check reads as behind, and the next
    refresh re-does this same no-op forever.
    """
    store = refresh(CHANGED_BEFORE)

    job = store.get_job("j-refresh")
    assert job.status == "complete"
    preview = job.scope_preview
    assert (preview.files_added, preview.files_modified, preview.files_deleted) == (0, 0, 0)
    assert preview.affected_entities == 0

    project = store.get_project(SLUG)
    assert project.commit_sha == NEW_COMMIT
    assert project.commit_short == NEW_COMMIT[:7]
    # The graph is untouched — both seeded files still carry the OLD commit.
    assert _files_in_graph(store) == {"kept.py", "changed.py"}


def test_scoped_finalize_preserves_the_display_facts_it_did_not_regenerate(
    refresh,
) -> None:
    """Only the git snapshot moves; pages/desc/landing page survive verbatim.

    A scoped refresh regenerates no pages, so rebuilding the ``Project`` record
    the way the full finalizers do would reset a page count to zero and drop a
    maintainer-edited description on every incremental run.
    """
    store = refresh(CHANGED_AFTER)
    project = store.get_project(SLUG)

    assert project.commit_sha == NEW_COMMIT
    assert project.indexed_at != "2020-01-01T00:00:00Z"
    assert project.pages == 7
    assert project.desc == "the description a maintainer edited"
    assert project.landing_page_id == "overview"
    assert project.graph_only is False


# ── lifecycle parity with the other jobless runner ──────────────────────────


def test_scoped_refresh_emits_the_existing_phase_vocabulary(refresh) -> None:
    """Reusing ``clone``/``scan``/``graph``/``finalize`` is what keeps the FE dumb.

    A scoped-only phase name would need adding to ``PHASE_SEQUENCE``, which
    renumbers the ordinal every stored progress comparison is derived from.
    """
    store = refresh(CHANGED_AFTER)
    events = store.load_job_events("j-refresh", after_idx=-1)

    assert [e["name"] for e in events if e.get("type") == "phase"] == [
        "clone", "scan", "graph", "finalize",
    ]
    types = {e.get("type") for e in events}
    assert {"queued", "scope_preview", "complete"} <= types
    complete = next(e for e in events if e.get("type") == "complete")
    assert complete["landingPageId"] == "overview"
    assert complete["pageCount"] == 7


def test_a_cancel_between_phases_stops_the_refresh(tmp_path, monkeypatch) -> None:
    """The inherited cooperative cancel must survive on this runner too.

    Same contract ``GraphOnlyIndexer`` has — the daemon thread re-reads status at
    each phase boundary and must not clobber a cancel with ``complete``.

    The cancel lands during SCAN rather than clone, because clone's own
    ``update_job(status="scanning", …)`` is a status write and would overwrite an
    earlier one. Scan writes only ``current_file``/``scanned_count``, so a cancel
    taken there survives to the pre-``graph`` boundary check — the same window
    the graph-only runner's equivalent test uses.
    """
    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    monkeypatch.setattr(
        "mewbo_graph.wiki.embedder.Embedder.enabled", staticmethod(lambda: False)
    )
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    store.create_job(IndexingJob(
        jobId="j-cancel", slug=SLUG, status="queued",
        scannedCount=0, totalCount=0, currentFile=None,
    ))
    _seed_prior_index(store, changed_body=CHANGED_BEFORE)

    def _fake_clone(cmd, *a, **kw):
        dest = Path(cmd[-1])
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "kept.py").write_text(KEPT_SOURCE, encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", _fake_clone)
    monkeypatch.setattr(
        jobless_mod, "_git_rev_parse",
        lambda d, args: NEW_COMMIT if args == ["HEAD"] else "main",
    )

    real_collect = jobless_mod._collect_files

    def _cancel_during_scan(clone_dir, args):
        store.cancel_job("j-cancel")  # ← out-of-band cancel, mid scan-phase
        return real_collect(clone_dir, args)

    monkeypatch.setattr(jobless_mod, "_collect_files", _cancel_during_scan)

    ctx = build_jobless_ctx(job_id="j-cancel", slug=SLUG, store=store)
    ScopedRefreshRunner(ctx, _submission()).run()

    assert store.get_job("j-cancel").status == "cancelled"
    # The project still describes the commit it was last actually indexed at.
    assert store.get_project(SLUG).commit_sha == OLD_COMMIT


# ── the stamped fingerprint must equal the probed one ────────────────────────


def test_the_probe_agrees_with_what_the_graph_phase_stamps(tmp_path, monkeypatch) -> None:
    """A just-indexed project must read as reusable against a fresh probe.

    This is the one failure in the whole refresh path that is INVISIBLE to every
    other test here, because it fails GREEN. ``build_graph_core`` stamps
    ``IndexingJob.fingerprint`` from its own locals, ``current_index_fingerprint``
    derives the same four facts independently, and every OTHER test stubs one
    side or the other. So if the two ever disagree — a normalisation applied on
    one path and not the other is the obvious way — then `RefreshDecision.decide`
    returns ``fingerprint_mismatch`` for every project forever, the scoped path
    becomes unreachable, and NOTHING fails: the suite passes, the console renders
    an honest-looking full-rebuild reason, and the feature is simply inert.

    The trap this pins concretely: ``Embedder`` normalises a configured model
    name (the proxy-prefix rule lives on that class), so a probe that read the
    raw config value instead of constructing one would mismatch on every single
    refresh while looking perfectly reasonable in review.

    Only the fields that can drift are compared. ``embedding_model`` is compared
    against ``_make_embedder().model`` — the exact expression the stamp site
    assigns from — rather than against a literal, so the assertion follows a
    future change to the normalisation rule instead of pinning today's answer.
    """
    from mewbo_graph.plugins.wiki.build_graph import (
        _embeddings_enabled,
        _make_embedder,
        _resolver_available,
        _tree_sitter_pack_version,
    )
    from mewbo_graph.plugins.wiki.scoped_refresh import current_index_fingerprint
    from mewbo_graph.wiki.types import CodeGraph

    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    probed = current_index_fingerprint(store, "acme/repo")

    # The three non-embedding legs are read from the SAME helpers the stamp site
    # calls, so any future divergence has to be a real one rather than two
    # spellings of one probe.
    assert probed.graph_schema_version == CodeGraph.model_fields["schema_version"].default
    assert probed.grammar_pack_version == _tree_sitter_pack_version()
    assert probed.resolver_available == _resolver_available()

    # The embedding leg is the one with a normalisation step between config and
    # the stamped value, so it gets the load-bearing assertion.
    if _embeddings_enabled():
        assert probed.embedding_model == _make_embedder(store, "acme/repo").model, (
            "the probe disagrees with what the graph phase would stamp — every "
            "refresh would report fingerprint_mismatch and silently rebuild"
        )
    else:
        assert probed.embedding_model is None

    # …and again with a config value that DEMANDS normalisation, because the
    # assertion above is only sensitive when the ambient value is not already
    # in its normalised form. Under a config whose model name already carries
    # the proxy prefix, a probe that read the raw value would agree with the
    # stamp site by coincidence and this test would pass while the bug shipped.
    # Forcing an un-prefixed name removes that dependence on the environment.
    monkeypatch.setattr(
        "mewbo_graph.wiki.embedder.Embedder.enabled", staticmethod(lambda: True)
    )
    monkeypatch.setattr(
        "mewbo_graph.wiki.embedder.get_config_value",
        lambda *keys, default=None: (
            "needs-a-prefix" if keys[-1] == "model" else default
        ),
    )
    forced = current_index_fingerprint(store, "acme/repo")
    assert forced.embedding_model == _make_embedder(store, "acme/repo").model
    assert forced.embedding_model == "openai/needs-a-prefix", (
        "the probe returned a raw config value — the stamp site normalises it, "
        "so every refresh would compare un-normalised against normalised"
    )


def test_embeddings_disabled_reads_as_stale_against_a_vectorised_index(
    tmp_path, monkeypatch
) -> None:
    """Turning embedding OFF must invalidate an index that HAS vectors.

    The mirror of the test above, and the reason ``embedding_model`` is nullable
    rather than defaulted: an index built with vectors is genuinely stale against
    a deployment that would now build none, because half its retrieval surface
    would silently stop being maintained. Collapsing that into a match is the
    tempting simplification this pins shut.
    """
    from mewbo_graph.plugins.wiki.scoped_refresh import current_index_fingerprint
    from mewbo_graph.wiki.types import FingerprintDecision

    monkeypatch.setattr(
        "mewbo_graph.wiki.embedder.Embedder.enabled", staticmethod(lambda: False)
    )
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    probed = current_index_fingerprint(store, "acme/repo")
    assert probed.embedding_model is None

    vectorised = probed.model_copy(update={"embedding_model": "openai/some-embedder"})
    verdict = FingerprintDecision.compute(probed, vectorised)
    assert verdict.reason == "mismatch"
    assert [m.field for m in verdict.mismatches] == ["embedding_model"]


# ── the untouched majority must stay VISIBLE, not merely stay alive ──────────


def test_untouched_files_remain_visible_to_live_readers(refresh) -> None:
    """A scoped refresh must not make the rest of the codebase invisible.

    The sibling of the supersede regression above, and a strictly harder
    property: that test proves the untouched rows still EXIST, this one proves a
    reader can still SEE them. Both can fail independently and the second failed
    silently for a while.

    ``store.live_scope(slug)`` is ``CommitScope.at(project.commit_sha)``, and it
    is what the knowledge-graph view, the retriever and the agent graph tools
    all read through. A scoped refresh re-stamps only what it re-parsed, so if
    it advances the project row without carrying the rest forward, every one of
    those readers resolves a generation containing just the handful of changed
    files. Nothing errors. Nothing is deleted. The codebase simply stops being
    there, which is why this needs its own assertion rather than trusting the
    existence check.
    """
    store = refresh(CHANGED_AFTER)

    live = store.query_graph(SLUG, scope=store.live_scope(SLUG))
    every = store.query_graph(SLUG, scope=CommitScope.every())

    live_files = {n.file for n in live}
    assert "kept.py" in live_files, (
        "the untouched file is invisible under live_scope — a scoped refresh "
        "advanced the project's commit without carrying its artifacts forward"
    )
    # One generation must describe the WHOLE slug after a scoped refresh, or
    # some reader somewhere is looking at a subset and cannot tell.
    assert {n.node_id for n in live} == {n.node_id for n in every}
    assert store.get_project(SLUG).commit_sha == NEW_COMMIT


def test_restamping_does_not_resurrect_an_older_generation(refresh) -> None:
    """Carrying the previous commit forward must not revive the one before it.

    The tempting simplification — stamp every row for the slug onto the new
    commit — trades this feature's bug for a worse one. A store can hold
    generations older than the one being carried forward (a supersede that never
    ran, the field-absent rows the isolation backfill exists for), and those
    describe code that is genuinely gone. Blanket-stamping would render deleted
    symbols as live, which is precisely the defect the epic this work belongs to
    was filed to remove.
    """
    store = refresh(CHANGED_AFTER)
    ancient = "c" * 40
    # A row from a generation two indexes ago, of a file that no longer exists.
    store.upsert_nodes(
        SLUG,
        [make_graph_node(
            slug=SLUG, node_id="ancient-1", type="Function",
            name="deleted_helper", file="gone.py", range=(0, 10),
        )],
        commit_sha=ancient,
    )
    # A second scoped refresh moves ONLY the current generation forward.
    newer = "d" * 40
    store.restamp_graph_artifacts(SLUG, from_commit=NEW_COMMIT, to_commit=newer)

    survivors = {
        n.node_id: n.commit_sha
        for n in store.query_graph(SLUG, scope=CommitScope.every())
    }
    assert survivors["ancient-1"] == ancient, (
        "an older generation was carried forward — deleted code would render "
        "as live under the new commit"
    )
    assert all(
        sha == newer for nid, sha in survivors.items() if nid != "ancient-1"
    )
