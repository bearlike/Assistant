"""Progress honesty for the two phases with no per-unit boundary tool.

``graph`` and ``enrich`` are the only phases whose work is a loop or a fan-out
with no per-unit boundary tool, so between their first write and their last the
job snapshot did not change — one real index sat in the tree-sitter loop for 25
minutes emitting nothing, which from outside is exactly what a wedged run looks
like. These tests pin the three pieces that fix it: the throttle, the field a
reader consults, and the wiring that actually calls them.

Only the clock is injected; the store, the tools and the parser are real.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from mewbo_graph.plugins.wiki import _ctx as ctx_mod
from mewbo_graph.plugins.wiki._ctx import PhaseProgress, emit_phase
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import IndexingJob

FIXTURE = Path(__file__).parent / "fixtures" / "tiny_python_repo"
T0 = datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)


# ── Helpers ───────────────────────────────────────────────────────────────────


class _Clock:
    """A hand-wound clock — the phase throttle is time-based, not sleep-based."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _store(tmp_path: Path) -> JsonWikiStore:
    return JsonWikiStore(root_dir=tmp_path / "wiki")


def _job(store: JsonWikiStore, job_id: str = "j1", slug: str = "org/repo") -> IndexingJob:
    job = IndexingJob(
        jobId=job_id, slug=slug, status="scanning",
        scannedCount=0, totalCount=0, currentFile=None,
    )
    store.create_job(job)
    return job


def _ctx(store: JsonWikiStore, job_id: str = "j1") -> SimpleNamespace:
    return SimpleNamespace(store=store, job_id=job_id, slug="org/repo")


def _logs(store: JsonWikiStore, job_id: str = "j1") -> list[str]:
    return [e["text"] for e in store.load_job_events(job_id) if e["type"] == "log"]


# ── The stale-field question `last_progress_at` exists to answer ──────────────


def test_seconds_since_progress_reads_the_stamp_and_refuses_to_guess() -> None:
    """"Never reported" and "unreadable" are both "no baseline", never "just now"."""
    job = IndexingJob(
        jobId="j", slug="s", status="scanning",
        scannedCount=0, totalCount=0, currentFile=None,
    )
    assert job.seconds_since_progress(T0) is None

    moved = job.model_copy(update={"last_progress_at": IndexingJob.format_stamp(T0)})
    assert moved.seconds_since_progress(T0 + timedelta(seconds=90)) == 90.0

    garbled = job.model_copy(update={"last_progress_at": "yesterday"})
    assert garbled.seconds_since_progress(T0) is None


def test_entering_a_phase_seeds_the_progress_baseline(tmp_path: Path) -> None:
    """A transition IS movement, so every phase starts with a stamp to compare against."""
    store = _store(tmp_path)
    _job(store)
    emit_phase(_ctx(store), "graph")

    job = store.get_job("j1")
    assert job is not None
    assert job.last_progress_at is not None
    assert job.last_progress_at == job.phase_started_at


def test_the_progress_triple_reaches_the_wire_under_its_camelcase_aliases() -> None:
    """The console reads ``phaseProgress*``; the store field names are snake_case.

    The rename between those two spellings is exactly the kind of mismatch no
    test on either side can catch alone — both compile, both pass, and the
    consumer silently never fires. This asserts the one seam that binds them.
    """
    job = IndexingJob(
        jobId="j", slug="s", status="scanning",
        scannedCount=0, totalCount=0, currentFile=None,
    ).model_copy(update={
        "phase_progress_current": 12,
        "phase_progress_total": 40,
        "phase_progress_unit": "files",
        "last_progress_at": IndexingJob.format_stamp(T0),
    })
    wire = job.model_dump(mode="json", by_alias=True, exclude_none=True)

    assert wire["phaseProgressCurrent"] == 12
    assert wire["phaseProgressTotal"] == 40
    assert wire["phaseProgressUnit"] == "files"
    assert wire["lastProgressAt"] == IndexingJob.format_stamp(T0)


def test_a_phase_transition_clears_the_previous_phase_s_progress(tmp_path: Path) -> None:
    """THE invariant every progress reader leans on.

    Without it a non-null ``phase_progress_current`` could be a leftover from a
    phase that ended minutes ago while reading as if it described the current
    one — which is exactly what made ``scanned_count``/``current_file``
    untrustworthy, and the reason this triple is generic rather than one pair
    per phase.
    """
    store = _store(tmp_path)
    _job(store)
    ctx = _ctx(store)
    emit_phase(ctx, "graph")
    PhaseProgress(ctx, label="Parsing", unit="files", clock=_Clock()).advance(9, 10)

    mid = store.get_job("j1")
    assert mid is not None
    assert (mid.phase_progress_current, mid.phase_progress_unit) == (9, "files")

    emit_phase(ctx, "enrich")

    job = store.get_job("j1")
    assert job is not None
    assert job.phase == "enrich"
    assert job.phase_progress_current is None
    assert job.phase_progress_total is None
    assert job.phase_progress_unit is None
    # ...but the "did this job move at all" signal is STAMPED, never cleared:
    # entering a phase is movement, and clearing it would blind a staleness
    # reader at the exact moment a phase begins.
    assert job.last_progress_at is not None


# ── The throttle ──────────────────────────────────────────────────────────────


def test_progress_writes_once_per_interval_not_once_per_unit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _job(store)
    clock = _Clock()
    progress = PhaseProgress(
        _ctx(store), label="Parsing", unit="files", interval_s=5.0, clock=clock
    )

    for done in range(1, 5):
        progress.advance(done, 100, detail=f"f{done}.py")
    assert _logs(store) == ["Parsing 1/100: f1.py"]

    clock.advance(5.0)
    progress.advance(5, 100, detail="f5.py")
    assert _logs(store) == ["Parsing 1/100: f1.py", "Parsing 5/100: f5.py"]

    job = store.get_job("j1")
    assert job is not None
    assert job.seconds_since_progress(clock.now) == 0.0
    # The generic per-unit pair the console extrapolates its ETA from.
    assert (job.phase_progress_current, job.phase_progress_total) == (5, 100)


def test_a_fan_out_worker_throttles_against_the_job_not_itself(tmp_path: Path) -> None:
    """The enrich fan-out gets a FRESH instance per mint.

    A purely instance-local timer would therefore never throttle anything — the
    first call on a new object is always "due" — so the deadline is seeded from
    the job's own persisted stamp.
    """
    store = _store(tmp_path)
    _job(store)
    clock = _Clock()

    def mint(name: str) -> None:
        PhaseProgress(
            _ctx(store), label="Enriching", unit="entities", interval_s=5.0, clock=clock
        ).advance(detail=name)

    mint("Ada")
    mint("Grace")
    mint("Alan")
    assert _logs(store) == ["Enriching: Ada"]

    clock.advance(5.0)
    mint("Barbara")
    assert _logs(store) == ["Enriching: Ada", "Enriching: Barbara"]


def test_progress_is_written_when_there_is_no_baseline_to_compare(tmp_path: Path) -> None:
    """A missing/unreadable job must mean "write", never "stay silent"."""
    class _Raising:
        def get_job(self, job_id):
            raise RuntimeError("transient store read failure")

        def append_job_event(self, job_id, event):
            self.event = event
            return 0

        def update_job(self, job_id, **fields):
            raise RuntimeError("transient store write failure")

    store = _Raising()
    PhaseProgress(
        SimpleNamespace(store=store, job_id="j1"), label="Parsing", unit="files", clock=_Clock()
    ).advance(1, 2, detail="a.py")
    assert store.event["text"] == "Parsing 1/2: a.py"


def test_progress_text_degrades_without_counts(tmp_path: Path) -> None:
    """The enrich fan-out has no knowable total; the line still has to read well."""
    store = _store(tmp_path)
    _job(store)
    clock = _Clock()
    PhaseProgress(_ctx(store), label="Enriching", unit="entities", clock=clock).advance(
        detail="RetryStrategy (concept)"
    )
    assert _logs(store) == ["Enriching: RetryStrategy (concept)"]


def test_a_costly_unit_count_is_only_paid_when_a_write_is_due(tmp_path: Path) -> None:
    """``units_of`` is a callable precisely so a throttled call costs nothing.

    The enrich count is a store scan; taking it on every mint to then discard it
    would make progress reporting more expensive than the work it reports.
    """
    store = _store(tmp_path)
    _job(store)
    clock = _Clock()
    calls: list[int] = []

    def units():
        calls.append(1)
        return (len(calls) * 10, None)

    progress = PhaseProgress(
        _ctx(store), label="Enriching", unit="entities", units_of=units, interval_s=5.0, clock=clock
    )
    for _ in range(4):
        progress.advance(detail="Ada")
    assert len(calls) == 1

    clock.advance(5.0)
    progress.advance(detail="Grace")
    assert len(calls) == 2
    job = store.get_job("j1")
    assert job is not None
    # A running count with no knowable total — a status line, not a fraction.
    assert (job.phase_progress_current, job.phase_progress_total) == (20, None)


def test_a_unit_count_that_raises_costs_only_the_position(tmp_path: Path) -> None:
    """A store hiccup must not take the progress line down with it."""
    store = _store(tmp_path)
    _job(store)

    def units():
        raise RuntimeError("transient store read failure")

    PhaseProgress(
        _ctx(store), label="Enriching", unit="entities", units_of=units, clock=_Clock()
    ).advance(detail="Ada")

    assert _logs(store) == ["Enriching: Ada"]
    job = store.get_job("j1")
    assert job is not None
    assert job.phase_progress_current is None
    assert job.last_progress_at is not None


# ── The wiring: the parse loop reports, and the tool consumes the report ──────


def test_parse_repo_reports_every_file_and_leaves_the_throttling_to_its_caller() -> None:
    """The parser knows how far along it is; it does not know what is worth writing."""
    from mewbo_graph.wiki.graph import GraphIndex

    files = sorted(p for p in FIXTURE.iterdir() if p.is_file())
    seen: list[tuple[int, int, str]] = []
    GraphIndex().parse_repo(
        slug="x/y",
        repo_root=FIXTURE,
        files=files,
        on_progress=lambda done, total, path: seen.append((done, total, path)),
    )

    assert [done for done, _t, _p in seen] == list(range(1, len(files) + 1))
    assert {total for _d, total, _p in seen} == {len(files)}
    # Paths are repo-relative, matching every other progress surface.
    assert [p for _d, _t, p in seen] == [f.name for f in files]


def test_the_enrich_phase_reports_progress_while_it_mints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other silent phase. Its unit of work is one mint, and it has no total."""
    from mewbo_core.classes import ActionStep
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

    monkeypatch.setattr(ctx_mod, "_PROGRESS_INTERVAL_S", 0.0)
    store = _store(tmp_path)
    _job(store)
    monkeypatch.setattr(mint_mod, "_ctx_for", lambda tool: _ctx(store))
    monkeypatch.setattr(mint_mod, "_make_embedder", lambda *_args: _FakeEmbedder())

    tool = mint_mod.MintEntityTool(session_id="sess-1")
    asyncio.run(tool.handle(ActionStep(
        tool_id="mint_entity", operation="call",
        tool_input={"name": "RetryStrategy", "type": "concept"},
    )))

    assert "Enriching 1: RetryStrategy (concept)" in _logs(store)
    job = store.get_job("j1")
    assert job is not None and job.last_progress_at is not None
    # A running entity count with no knowable total — the console's own contract
    # for this phase, and dead on the wire until something minted it.
    assert (job.phase_progress_current, job.phase_progress_total) == (1, None)


def test_the_graph_phase_reports_progress_while_it_parses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: the phase that was silent for 25 minutes now writes as it goes.

    The interval is dropped to zero because the fixture repo parses in
    milliseconds — the throttle itself is pinned above; what this test proves is
    that the tool is WIRED to it at all.
    """
    from mewbo_graph.plugins.wiki import build_graph as build_graph_mod
    from mewbo_graph.plugins.wiki.build_graph import WikiBuildGraphTool

    monkeypatch.setattr(ctx_mod, "_PROGRESS_INTERVAL_S", 0.0)
    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    store = _store(tmp_path)
    _job(store, slug="x/y")
    store.attach_job_session("j1", "sess-1")
    clone_dir = tmp_path / "clones" / "j1"
    clone_dir.mkdir(parents=True)
    for src in FIXTURE.iterdir():
        if src.is_file():
            (clone_dir / src.name).write_bytes(src.read_bytes())

    tool = WikiBuildGraphTool(session_id="sess-1")
    embedder = MagicMock()
    embedder.embed_nodes.return_value = []
    with patch.object(
        build_graph_mod, "_resolve_runtime", return_value=MagicMock(wiki_store=store)
    ), patch.object(build_graph_mod, "_make_embedder", return_value=embedder):
        asyncio.run(tool.handle(MagicMock(tool_input={})))

    # The counted form only — the phase's own "Parsing N files…" opener is the
    # pre-existing line, and it is the one that was the ONLY thing written.
    parsing = [line for line in _logs(store) if re.match(r"^Parsing \d+/\d+: ", line)]
    # One line per file, each naming how far through the tree the loop is.
    assert len(parsing) == len([p for p in clone_dir.iterdir() if p.is_file()])
    assert parsing[-1].startswith(f"Parsing {len(parsing)}/{len(parsing)}: ")
    job = store.get_job("j1")
    assert job is not None and job.last_progress_at is not None
