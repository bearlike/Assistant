"""``wiki_build_graph`` SessionTool — parses cloned tree into graph + embeddings."""
from __future__ import annotations

import asyncio
import os
import sys
from typing import TYPE_CHECKING, Any

from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from mewbo_core.config import get_config_value
from pydantic import BaseModel, ConfigDict

from mewbo_graph.plugins.wiki._base import WikiSessionTool, _err_result
from mewbo_graph.plugins.wiki._ctx import (
    ProgressReporter,
    WikiJobCtx,
    emit_log,
    emit_phase,
    resolve_runtime,
)
from mewbo_graph.plugins.wiki.step_plans import (  # noqa: F401 — compatibility export
    GRAPH_STEPS,
    planned_steps_for_slug,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mewbo_core.classes import ActionStep

    from mewbo_graph.wiki.graph import GraphParseResult
    from mewbo_graph.wiki.types import GraphEdge, GraphNode, GraphResolution

logging = get_logger(name="mewbo_graph.plugins.wiki.build_graph")


class WikiBuildGraphArgs(BaseModel):
    """Args for wiki_build_graph (no inputs — uses ctx)."""

    model_config = ConfigDict(extra="forbid")


def _resolve_runtime() -> Any:
    """Resolve the wiki runtime (the down-only store seam). Patched in tests."""
    return resolve_runtime()


def _make_embedder(store: Any, slug: str) -> Any:
    """Create the Embedder for one project; isolated so tests can stub it."""
    from mewbo_graph.wiki.embedder import make_embedder_for  # noqa: PLC0415

    return make_embedder_for(store, slug)


def _embeddings_enabled() -> bool:
    """Read the embedding switch; isolated so tests can stub it."""
    from mewbo_graph.wiki.embedder import Embedder  # noqa: PLC0415
    return Embedder.enabled()


# DEFAULT for how long the off-loop graph build may run before the tool stops
# waiting on it; the operator sizes the real value (``wiki.phase_timeouts.graph_build_s``).
# A build is minutes on a real repository — the largest measured pass ran for the
# better part of half an hour — so this is a WEDGE detector, not a pacing knob:
# responsiveness comes from running the work off the event loop, never from
# clipping it. The default sits well above any observed honest build so that
# expiring means something is stuck rather than that the repository is big — but
# "well above" depends on the repositories and the hardware a deployment has, so
# it cannot be a number baked in here.
_GRAPH_BUILD_BUDGET_S: float = 3600.0

# Headroom between this tool's own deadline and the loop's outer ceiling, so the
# TOOL is what expires and the model reads a result rather than a failed call.
# Deliberately NOT operator-tunable, mirroring ``ask_user.QUESTION_TIMEOUT_MARGIN_S``:
# it encodes which layer expires first, not a quantity about this deployment, and
# the only settings that would change anything are the ones that invert the
# invariant and silently trade the readable result back for a failed tool call.
_CEILING_MARGIN_S: float = 60.0


class WikiBuildGraphTool(WikiSessionTool):
    """SessionTool: parse cloned files into a graph + embeddings."""

    tool_id = "wiki_build_graph"
    args_cls = WikiBuildGraphArgs
    schema = pydantic_to_openai_tool(WikiBuildGraphArgs, name="wiki_build_graph")

    #: Recorded by :meth:`handle`, drained by the loop's ``tool_result`` emit.
    _headline: str | None = None

    def result_headline(self) -> str | None:
        """The one-line trace title for the graph phase that just finished.

        The declared-headline convention (``mewbo_core.tooling.session_tools``): read
        once and cleared, so it can never title a later call.
        """
        headline, self._headline = self._headline, None
        return headline

    def _budget_s(self) -> float:
        """This deployment's ceiling for one graph build, in seconds.

        Read from config at CALL time rather than frozen at import, so the two
        readers — the wait in :meth:`handle` and the ceiling this tool declares
        to the loop — resolve the same number and cannot drift apart. A wedge on
        a small repository behind a fast proxy and a wedge on a large monorepo
        behind a slow one are not the same duration, which is exactly why the
        module constant is the DEFAULT and not the value.
        """
        return float(
            get_config_value(
                "wiki", "phase_timeouts", "graph_build_s", default=_GRAPH_BUILD_BUDGET_S
            )
        )

    def execution_timeout(self, tool_input: object) -> float | None:
        """The loop's outer ceiling for this call: this tool's budget + margin.

        A SessionTool carries no ``ToolSpec``, so an undeclared ceiling is the
        registry's flat 120s fallback — a number sized for shell and MCP output,
        inherited by a tool that legitimately runs for tens of minutes. That
        fallback was inert only for as long as the body blocked the event loop:
        the ceiling is an ``asyncio.wait_for``, and a timer cannot fire on a loop
        that is not turning, so the declared bound expired unobserved and the
        call returned normally whenever it finished. Moving the work off the loop
        (see :meth:`handle`) arms that ceiling for the first time, which is why
        it has to be declared here in the same change.

        The margin keeps this tool, not the loop, the thing that expires: an
        over-budget build then returns a readable RESULT the model can act on
        instead of the loop's flat "timed out" failure — the same inversion
        ``ask_user_question`` states for its own deadline.
        """
        return self._budget_s() + _CEILING_MARGIN_S

    def _over_budget_result(self, ctx: WikiJobCtx, budget_s: float) -> MockSpeaker:
        """The bounded outcome for a build that outran its configured budget.

        Abandoning the wait does NOT stop the work: a thread inside tree-sitter
        reaches no cancellation point, so the parse runs on and, if it finishes,
        persists a graph a later resume reuses. Saying so is the whole point of
        writing this message by hand — a bare "timed out" reads as "nothing
        happened" and invites an immediate re-run, which would put a second parse
        of the same tree on top of the first and have both upsert the same nodes.
        The same fact goes into the job timeline, because that is the surface
        somebody watching the index is actually looking at.
        """
        budget = f"{budget_s:g}s"
        emit_log(ctx, f"Graph build exceeded its budget of {budget}", level="warn")
        self._headline = f"Graph build exceeded its budget of {budget}"
        return _err_result(
            "timeout",
            f"graph build exceeded its budget of {budget}. The parse is still "
            "running in the background and its result, if it completes, is "
            "persisted for a resume to reuse — do not call wiki_build_graph "
            "again for this job.",
        )

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a ``wiki_build_graph`` tool call."""
        ctx = self._job_ctx()
        if ctx is None:
            return _err_result("internal", "wiki job ctx not found")
        parsed_args = self._parse_args(WikiBuildGraphArgs, action_step)
        if isinstance(parsed_args, MockSpeaker):
            return parsed_args

        emit_phase(ctx, "graph")

        # Checkpoint-aware resume: the persisted graph is reused
        # as-is when an interrupted index already built it (graph + enrich are
        # the ~6-min expensive idempotent phases). Done-detection lives ONLY in
        # ResumePlan (DRY); this is the single-line short-circuit.
        rp = ctx.resume_plan
        if rp is not None and rp.should_skip("graph"):
            ProgressReporter(ctx).skip_group("graph", note="reused on resume")
            emit_log(ctx, f"Graph already built ({rp.node_count} nodes) — skipped on resume")
            # A skip and a rebuild are the same ~6-minute-shaped step in the
            # trace unless the title says which one ran.
            self._headline = (
                f"Graph reused from checkpoint: {rp.node_count} nodes "
                "(skipped on resume)"
            )
            return MockSpeaker(content=str({
                "nodeCount": rp.node_count,
                "skipped": "graph already built — reused on resume",
            }))

        # 1. Walk the clone dir.
        repo_root = ctx.clone_dir
        if not repo_root.exists():
            return _err_result("internal", f"clone dir missing: {repo_root}")

        # 2-4. Parse + persist + embed via the shared deterministic core (reused
        # by the zero-LLM GraphOnlyIndexer so tree-sitter parsing + the embedding
        # fallback live in exactly ONE place).
        #
        # OFF THE EVENT LOOP. ``handle`` is a coroutine, but this body is
        # tree-sitter, blocking driver writes and a synchronous embedding call —
        # not one suspension point among them. Awaited inline it did not merely
        # take a long time, it OWNED the loop for the whole build: no
        # cancellation, no watchdog tick, no sibling coroutine, and no progress
        # emission from anything that was not already writing from this same
        # frame. ``GraphOnlyIndexer`` has always driven this exact function from
        # a worker thread, so running it off-loop here is the shape that was
        # already load-bearing, not a new assumption about its thread-safety.
        budget_s = self._budget_s()
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(build_graph_core, ctx), timeout=budget_s
            )
        except asyncio.TimeoutError:
            return self._over_budget_result(ctx, budget_s)
        self._headline = (
            f"Built graph: {result.get('nodeCount', 0)} nodes, "
            f"{result.get('edgeCount', 0)} edges"
        )

        # The ``enrich`` phase is deliberately NOT stamped here. build_graph is
        # the last tool before the wiki-enricher fan-out, and stamping a phase
        # when its PREDECESSOR ends claims work that has not begun: a run
        # cancelled moments after this returns would leave a job reading
        # ``enrich`` forever for a fan-out that never spawned. The enrichers
        # stamp it themselves on their first mint (``mint_entity`` →
        # ``emit_phase_once``), so the phase marks work that actually started.
        return MockSpeaker(content=str(result))


def build_graph_core(ctx: Any) -> dict[str, object]:
    """Parse the clone dir into a graph, persist it, and embed nodes.

    The DETERMINISTIC heart of the ``graph`` phase — zero LLM. Extracted from
    ``WikiBuildGraphTool.handle`` so the agent-driven indexer AND the zero-LLM
    ``GraphOnlyIndexer`` share one tree-sitter parse + persist + embedding-
    fallback path (DRY: tree-sitter parsing and the BM25 degrade live HERE, not
    duplicated). Operates purely over a job ctx (``slug``/``clone_dir``/``store``
    + ``emit_log``); does NOT emit phase events (the caller owns the phase
    transitions). Returns the result summary dict the tool serialises.
    """
    repo_root = ctx.clone_dir
    progress = ProgressReporter(ctx)
    progress.declare(planned_steps_for_slug(ctx.store, ctx.slug, "graph"))

    # 1. Parse with GraphIndex.
    from mewbo_graph.wiki.graph import GraphIndex  # noqa: PLC0415

    with progress.step("graph.discover_files"):
        files = [
            path for path in repo_root.rglob("*") if path.is_file() and ".git" not in path.parts
        ]
        emit_log(ctx, f"Parsing {len(files)} files with tree-sitter…")
    gi = GraphIndex()
    with progress.step("graph.parse", total=len(files)) as step:
        def on_progress(done: int, total: int, path: str) -> None:
            """Advance the active parse step and retain its timeline detail."""
            step.advance(done, total, path)
            emit_log(ctx, f"Parsing {done}/{total}: {path}")

        parsed = gi.parse_repo(
            slug=ctx.slug,
            repo_root=repo_root,
            files=files,
            on_progress=on_progress,
        )
    # Replace tree-sitter's name-matched PYTHON relationship edges with the
    # faithful resolver's exact edges when scip-python is available. No-op (the
    # parse passes straight through) when the resolver can't run — but never a
    # SILENT one: the fallback graph looks healthy, so the outcome is written to
    # the job log either way and "which leg ran" is answerable after the fact.
    #
    # The outcome is CAPTURED as well as logged: a job-log line is only ever read
    # by someone who already suspects something, and a total resolution outage
    # sat unnoticed behind exactly such a line. ``_record_resolution`` below puts
    # the same fact on the records a reader consults first.
    resolution: GraphResolution | None = None

    def _capture(outcome: GraphResolution) -> None:
        nonlocal resolution
        resolution = outcome

    resolver_step_keys = {
        "index": "graph.resolve_scip_index",
        "read": "graph.read_scip_index",
        "definitions": "graph.index_definitions",
        "occurrences": "graph.resolve_occurrences",
    }
    active_key = "graph.resolve_scip_index"
    active_step = progress.step(active_key)
    active_step.__enter__()
    reached = {active_key}

    def on_resolver_progress(stage: str, current: int, total: int) -> None:
        """Advance the plugin-owned resolver step the injected callback names."""
        nonlocal active_key, active_step
        key = resolver_step_keys[stage]
        if key != active_key:
            active_step.__exit__(None, None, None)
            active_key = key
            active_step = progress.step(key, total=total)
            active_step.__enter__()
            reached.add(key)
        active_step.advance(current, total)

    error: tuple[Any, Any, Any] = (None, None, None)
    try:
        parsed = _apply_resolver(
            ctx.slug,
            repo_root,
            parsed,
            on_report=lambda message: emit_log(ctx, message),
            on_outcome=_capture,
            on_progress=on_resolver_progress,
        )
        emit_log(ctx, f"Built graph: {len(parsed.nodes)} nodes, {len(parsed.edges)} edges")
    except BaseException:
        error = sys.exc_info()
        raise
    finally:
        active_step.__exit__(*error)
        for key in resolver_step_keys.values():
            if key not in reached:
                progress.skip(key, note="not reached")

    # 1b. Validate the whole graph ONCE at ingest (schema v2): node-id
    # uniqueness + referential integrity + CPG endpoint rules. A malformed graph
    # fails loudly HERE rather than corrupting the persisted store silently.
    from pydantic import ValidationError  # noqa: PLC0415

    from mewbo_graph.wiki.types import CodeGraph, IndexFingerprint  # noqa: PLC0415

    with progress.step("graph.validate"):
        try:
            code_graph = CodeGraph(nodes=parsed.nodes, edges=parsed.edges)
        except ValidationError as exc:
            raise ValueError(
                f"graph schema validation failed for {ctx.slug}: {exc}"
            ) from exc

    # 2. Persist the validated graph, attributed to the commit this job indexed.
    # The commit is read from the job record (written by clone) rather than a ctx
    # field: the agent build_graph tool resolves ctx fresh per call, but the
    # graph-only indexer builds its ctx once BEFORE clone, so the job record is
    # the one authoritative source both paths agree on. Stamping here is what lets
    # ``wiki_finalize`` supersede the previous commit's nodes instead of unioning
    # into them, and what makes the resume "graph for THIS commit built" count true.
    job = ctx.store.get_job(ctx.job_id)
    commit_sha = (job.commit_sha if job is not None else None) or getattr(
        ctx, "commit_sha", None
    )
    with progress.step("graph.persist_nodes") as step:
        ctx.store.upsert_nodes(
            ctx.slug,
            code_graph.nodes,
            commit_sha=commit_sha,
            job_id=ctx.job_id,
            on_progress=step.advance,
        )
    with progress.step("graph.persist_edges") as step:
        ctx.store.upsert_edges(
            ctx.slug,
            code_graph.edges,
            commit_sha=commit_sha,
            job_id=ctx.job_id,
            on_progress=step.advance,
        )

    # 3. Embed nodes if enabled. Embedding failures are non-fatal — retrieval
    # falls back to BM25 + 1-hop graph traversal, which is still useful. This
    # lets the indexer run against LLM proxies that don't expose an embedding
    # model.
    embedded_count = 0
    embedding_error: str | None = None
    # ``None`` unless a vector actually gets persisted below — mirrors
    # ``embedded_count`` exactly (both stay at their zero/None default
    # together, both get set together). This is deliberate, not incidental:
    # see ``IndexFingerprint.embedding_model``'s own docstring — a fingerprint
    # records what happened, never a config fallback for "what would have run".
    embedding_model: str | None = None
    if _embeddings_enabled() and parsed.nodes:
        with progress.step("graph.embed", total=len(parsed.nodes)) as step:
            try:
                embedder = _make_embedder(ctx.store, ctx.slug)
                items = [(n.node_id, n.embedding_text) for n in parsed.nodes]
                emit_log(ctx, f"Embedding {len(items)} nodes via {embedder.model}…")
                embeddings = _embed_with_progress(ctx, embedder, items, step)
            except Exception as exc:  # noqa: BLE001 — degrade gracefully
                embedding_error = str(exc)
                logging.warning(
                    "wiki_build_graph: embeddings unavailable; falling back to "
                    "BM25-only retrieval. Reason: {}",
                    embedding_error,
                )
                embeddings = []
                emit_log(
                    ctx,
                    f"Embeddings unavailable ({embedding_error}); falling back to BM25",
                    level="warn",
                )
        # Declared UNCONDITIONALLY — a store with nothing to persist (every
        # embed call returned empty, or a fully-degraded pass) is a real
        # terminal state for this step, not an absent one; leaving it pending
        # is the same "no closing scope" shape the enrich fan-out had.
        with progress.step("graph.persist_embeddings"):
            if embeddings:
                ctx.store.upsert_embeddings(
                    ctx.slug, embeddings, commit_sha=commit_sha, job_id=ctx.job_id
                )
                embedded_count = len(embeddings)
                embedding_model = embedder.model
                emit_log(
                    ctx,
                    f"Embedded {embedded_count} nodes (dim={embeddings[0].dim})",
                )
    else:
        with progress.step("graph.embed"):
            if not _embeddings_enabled():
                emit_log(ctx, "Embeddings disabled (wiki.embedding.enabled=false)", level="warn")
            else:
                emit_log(ctx, "No nodes to embed")
        with progress.step("graph.persist_embeddings"):
            pass

    # 4. Stamp this run's index fingerprint onto the job — the non-content
    # inputs that can later invalidate a hash-identical reuse decision.
    # Captured live HERE, at the moment the graph was actually built, and
    # NEVER re-derived at finalize: a re-probe there would answer "what is
    # available now", not "what built the artifacts actually in the store",
    # and those disagree exactly when a resume skips this whole phase
    # (``WikiBuildGraphTool.handle``'s ``rp.should_skip("graph")`` short
    # circuit above) — the earlier invocation's stamp on the SAME job_id is
    # what a resume must read, not a fresh probe of "now".
    #
    # ``update_job`` is an unlocked read-modify-write on both backends (a
    # known, separately-tracked defect) — a concurrent writer on another
    # thread (a Cancel arriving from the request thread is the documented
    # case) can lose this stamp. That failure mode is SAFE by construction:
    # an absent fingerprint later reads as
    # ``FingerprintDecision(reason="unknown")``, which forces a full rebuild
    # rather than silently permitting reuse of artifacts nothing actually
    # fingerprinted. Do not "optimise" a missing value into an assumed match.
    with progress.step("graph.record_fingerprint"):
        try:
            fingerprint = IndexFingerprint(
                embedding_model=embedding_model,
                graph_schema_version=code_graph.schema_version,
                grammar_pack_version=_tree_sitter_pack_version(),
                # ``_apply_resolver`` above already probed this once but doesn't
                # return the answer to its caller; the probe is cheap and
                # stateless (``shutil.which``), so a second call costs nothing.
                resolver_available=_resolver_available(),
            )
            ctx.store.update_job(
                ctx.job_id, fingerprint=fingerprint, resolution=resolution
            )
        except Exception as exc:  # pragma: no cover — best-effort, see comment above
            logging.warning(
                "wiki_build_graph: failed to stamp index fingerprint for {} ({})",
                ctx.slug, exc,
            )

    # 4b. Put the same resolution outcome on the PROJECT row, which is what a
    # reader consults to ask "are this wiki's cross-file edges exact?" without
    # replaying a job log. ``update_project`` is a no-op when the project record
    # does not exist yet (a first index creates it at finalize), so this write
    # lands on a RE-index; the first index's copy has to be carried across by
    # finalize, which builds the record wholesale. Best-effort in either case —
    # a graph that built is not worth failing over a display field.
    if resolution is not None:
        try:
            ctx.store.update_project(ctx.slug, {"resolution": resolution})
        except Exception as exc:  # pragma: no cover — best-effort
            logging.warning(
                "wiki_build_graph: failed to record symbol resolution for {} ({})",
                ctx.slug, exc,
            )

    languages = sorted({_lang_from_ext(n.file) for n in parsed.nodes if n.type != "Module"})

    result: dict[str, object] = {
        "nodeCount": len(parsed.nodes),
        "edgeCount": len(parsed.edges),
        "embeddedCount": embedded_count,
        "languages": [lang for lang in languages if lang],
        "skippedCount": len(parsed.skipped),
    }
    if embedding_error is not None:
        result["embeddingWarning"] = (
            "Embeddings unavailable — retrieval will use BM25 + graph only."
        )
    return result


# Nodes per embedding slice. The embedder batches internally, so slicing the
# CALL SITE adds no requests worth counting — it exists only to create a place
# where progress can be reported at all. Large enough that the slicing is free
# next to the request it wraps, small enough that a slice is not itself a
# multi-minute silence.
_EMBED_SLICE = 500


def _embed_with_progress(
    ctx: Any, embedder: Any, items: list[tuple[str, str]], step: Any
) -> list[Any]:
    """Embed *items* slice by slice so the graph phase reports while it embeds.

    Fixing only the parse loop left a smaller copy of the same blackout in the
    same phase: one ``embed_nodes`` call over tens of thousands of nodes is
    minutes of silence immediately after a loop that had just been taught to
    report every few seconds.

    Failure semantics are UNCHANGED and that is deliberate: a raise propagates
    to the caller's handler, which degrades the WHOLE pass to BM25. Returning
    the slices that happened to succeed would persist a partial vector set that
    nothing downstream would ever finish, and retrieval would silently rank
    against a fraction of the graph.
    """
    out: list[Any] = []
    total = len(items)
    for start in range(0, total, _EMBED_SLICE):
        out.extend(embedder.embed_nodes(items[start : start + _EMBED_SLICE], slug=ctx.slug))
        done = min(start + _EMBED_SLICE, total)
        step.advance(done, total)
    return out


# Edge kinds the faithful resolver SUPERSEDES for Python sources. tree-sitter
# emits these cross-file edges by NAME (every same-named symbol collapses onto
# one node); the resolver replaces them with exact, real-id REFERENCES/EXTENDS.
_RESOLVER_SUPERSEDED_EDGES = frozenset({"IMPORTS", "CALLS", "EXTENDS"})
_PYTHON_SUFFIXES = (".py", ".pyi")


def _tree_sitter_pack_version() -> str | None:
    """Installed ``tree-sitter-language-pack`` version, or ``None`` if absent.

    Mirrors ``mewbo_core.config.get_version``'s try/except
    ``PackageNotFoundError`` shape: the ``treesitter`` extra is optional, so
    an uninstalled pack is an honest absence, never a raise.
    """
    from importlib.metadata import PackageNotFoundError, version  # noqa: PLC0415

    try:
        return version("tree-sitter-language-pack")
    except PackageNotFoundError:
        return None


def _resolver_available() -> bool:
    """True when the faithful Python symbol resolver can run (both scip binaries).

    Isolated so tests can force the resolver on/off without the external CLIs.
    The ``wiki/resolve`` package is pure-Python (its scip backends are
    subprocesses) but lives behind the ``mewbo-graph[resolve]`` extra, so the
    import is guarded — an install without it degrades cleanly to tree-sitter-only
    edges, exactly as today.
    """
    try:
        from mewbo_graph.wiki.resolve import ScipPythonResolver  # noqa: PLC0415
    except ImportError:
        return False
    return ScipPythonResolver.is_available()


def _make_resolver(slug: str) -> Any:
    """Construct the Python symbol resolver for *slug*; isolated for test injection."""
    from mewbo_graph.wiki.resolve import ScipPythonResolver  # noqa: PLC0415

    return ScipPythonResolver(slug)


def _apply_resolver(
    slug: str,
    repo_root: Path,
    result: GraphParseResult,
    *,
    on_report: Callable[[str], None] | None = None,
    on_outcome: Callable[[GraphResolution], None] | None = None,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> GraphParseResult:
    """Swap tree-sitter's name-matched Python edges for the resolver's exact ones.

    The tree-sitter extractor emits cross-file IMPORTS/CALLS/EXTENDS edges *by
    name* — every same-named symbol collapses onto one node. When the faithful
    ``ScipPythonResolver`` is available it resolves those EXACTLY (real ``node_id``
    targets, ``target_name=None``), so for PYTHON source files we drop the
    synthetic name-matched edges and substitute the resolver's. Everything else is
    untouched: ``CONTAINS`` (structural, all languages) and the non-Python
    synthetic edges (Go/Rust/TS keep tree-sitter resolution until they get a
    backend) flow through unchanged. The resolver's descriptor-keyed ``External``
    nodes are persisted alongside the real nodes so its real-id edges survive the
    view's ``target in node_ids`` endpoint check.

    Degrades to a byte-identical ``result`` when the resolver can't run (binaries
    absent / extra uninstalled) or indexes nothing (``not res.available``) — NO
    behaviour change from the tree-sitter-only path.

    **Superseding is all-or-nothing across the repository, and that is a
    deliberate floor rather than the shape we would choose.** ``available`` is
    merely "at least one root was indexed", so superseding on it alone would let
    a pass that indexed 6 of 9 roots drop the name-matched edges of ALL NINE —
    leaving the three roots the resolver never reached with no cross-file edges
    whatsoever, which is worse than the guesses it removed. The precise repair
    is per-root: drop only the
    edges whose source file sits under a root that actually indexed. That is not
    derivable here — ``ResolutionResult``/``ResolutionStats`` carry root COUNTS
    and no root identities, so this asks :attr:`GraphResolution.faithful` for the
    conservative question it CAN answer ("did every discovered root index?") and
    keeps every name-matched edge otherwise. A partial pass still contributes its
    exact edges: they are additive and correct, and only the destructive half is
    withheld.

    ``on_outcome`` receives the :class:`GraphResolution` for the branch taken —
    on EVERY branch, including the two that return early. It is what makes the
    degradation answerable from the indexed project itself rather than from a
    line buried in a job log, and it is injected for the same reason
    ``on_report`` is: this helper resolves symbols and knows nothing about the
    record the outcome belongs on.

    **That degradation announces itself through ``on_report``, and must.** The
    graph it produces is not visibly broken — it is fully populated, passes
    validation, and renders — so a silent fallback is indistinguishable from a
    healthy index until someone counts cross-file edges by hand. A deployment
    whose image lacked the binaries degraded every index it ever ran, unnoticed,
    for exactly that reason. The callback is INJECTED for the same reason
    ``parse_repo`` takes ``on_progress``: this helper resolves symbols and knows
    nothing about the indexing job the message belongs to.
    """
    from mewbo_graph.wiki.graph import GraphParseResult  # noqa: PLC0415
    from mewbo_graph.wiki.types import GraphResolution  # noqa: PLC0415

    report = on_report or (lambda _message: None)
    record = on_outcome or (lambda _resolution: None)
    if not _resolver_available():
        report(
            "Faithful Python symbol resolution unavailable (scip / scip-python "
            "not on PATH) — Python cross-file edges fall back to name matching."
        )
        record(GraphResolution(available=False))
        return result
    resolver = _make_resolver(slug)
    if on_progress is None:
        res = resolver.resolve(repo_root, result.nodes)
    else:
        res = resolver.resolve(repo_root, result.nodes, on_progress=on_progress)
    resolution = GraphResolution(
        available=True,
        rootsDiscovered=res.stats.project_roots,
        rootsIndexed=res.stats.indexed_roots,
        resolvedEdges=len(res.edges),
    )
    if not res.available:
        report(
            "Faithful Python symbol resolution indexed no project root — "
            f"Python cross-file edges fall back to name matching. {res.stats.describe()}"
        )
        record(resolution)
        return result
    report(f"Resolved Python symbols: {res.stats.describe()}")
    record(resolution)

    if not resolution.faithful:
        # A partial pass ADDS its exact edges and drops none: the roots it never
        # reached would otherwise be left with no cross-file edges at all. Said
        # at warn level because the counters alone read as a successful pass.
        report(f"Keeping every name-matched Python edge: {resolution.describe()}")
        return GraphParseResult(
            nodes=result.nodes + res.externals,
            edges=result.edges + res.edges,
            skipped=result.skipped,
        )

    node_by_id = {n.node_id: n for n in result.nodes}
    kept_edges = [
        e for e in result.edges if not _is_superseded_python_edge(e, node_by_id)
    ]
    return GraphParseResult(
        nodes=result.nodes + res.externals,
        edges=kept_edges + res.edges,
        skipped=result.skipped,
    )


def _is_superseded_python_edge(
    edge: GraphEdge, node_by_id: dict[str, GraphNode]
) -> bool:
    """True for a synthetic IMPORTS/CALLS/EXTENDS edge whose source is a Python file.

    The edge's ``source`` is the in-repo node the relationship originates at (the
    File node for IMPORTS/CALLS, the subclass node for EXTENDS); reading its
    ``file`` tells us the source language. An unmappable source (shouldn't happen
    for these synthetic kinds) is conservatively KEPT — only edges we KNOW the
    resolver supersedes are dropped.

    Asks only about ONE edge. Whether superseding may happen at all is a
    property of the PASS, and it is asked once by the caller through
    :attr:`GraphResolution.faithful` — a per-edge re-derivation of that would be
    the same question answered in two places.
    """
    if edge.type not in _RESOLVER_SUPERSEDED_EDGES:
        return False
    src = node_by_id.get(edge.source)
    return src is not None and src.file.endswith(_PYTHON_SUFFIXES)


def _lang_from_ext(file_path: str) -> str:
    from mewbo_graph.wiki.graph import _LANG_BY_EXT  # noqa: PLC0415
    return _LANG_BY_EXT.get(os.path.splitext(file_path)[1].lower(), "")


__all__ = [
    "WikiBuildGraphArgs",
    "WikiBuildGraphTool",
    "build_graph_core",
]
