"""RunEventStreamer — project a live session transcript onto the run event log.

The root-cause fix for "the console sits on *Starting search…* for the whole
run" (#77): the orchestrated runner used to drive ``run_sync`` to completion and
then ``_settle`` batch-replayed EVERY ``agent_*`` event at the end, so a 2m42s
run emitted a single ``run_started`` followed by 53 events in one burst.

The mechanism reuses the SideStage streaming seam verbatim — the core
``SessionEventBus`` (``session_event_bus.py``), the same in-process per-session
pub/sub the realtime ``/v1/draft/stream`` and the console SSE generator already
ride. No new transport: the streamer *subscribes* to the backing session before
the drive starts, drains the subscription on a daemon thread, and projects each
``sub_agent`` lifecycle event onto ``store.append_run_event`` AS it happens
(``agent_start`` → ``agent_line`` → ``agent_done``). The run's own SSE generator
(``RunSseGenerator``) tails that log, so the console reveals each probe live.

Settle is reduced to terminal reconciliation: the synthesis typewriter
(``answer_delta*`` → ``answer_ready``) + ``run_done`` / ``error``, plus a
back-stop that flushes any trace agent the live stream missed (a fast run whose
``completion`` lands before the consumer drains, or a bus drop). Each ``run_id``
is consulted for what it has already streamed, so a reconciled agent is never
double-emitted.

This is a *transport* concern (it writes the api run store), so it lives here in
the api glue, not in the core engine or the graph library.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, cast, get_args

from mewbo_core.common import get_logger
from mewbo_core.session_event_bus import SessionEventBus, Subscription
from mewbo_core.types import EventRecord

from .. import events
from ..schemas import ResultKindLiteral, SearchResult, TraceAgent, TraceLine

# The wire's closed result-kind vocabulary — an emitted entry outside it fails
# `_to_result` and is dropped like any other malformed card.
_RESULT_KINDS = frozenset(get_args(ResultKindLiteral))

logging = get_logger(name="api.agentic_search.scg.run_streamer")


@dataclass
class _LaneState:
    """Per-probe streaming state shared between the consumer + the settle worker.

    ``slot`` is the lane's stable first-seen ordinal (the console lays lanes out
    on it); ``lines`` counts trace lines emitted so far; ``done`` flips once on
    the probe's ``stop`` so a duplicate ``stop`` never emits a second
    ``agent_done``. Mutated only under :attr:`RunEventStreamer._lock`.
    """

    slot: int
    lines: int = 0
    done: bool = False

# A probe brief (the ``start`` event ``detail``) can be the whole task block —
# we surface only its first substantive line as the lane's opening trace line so
# the console shows the pathway/sub-query, not the system-prompt boilerplate.
_PROBE_LINE_CAP = 160


class ProbeTrace:
    """Pure projection of a ``sub_agent`` event into trace fields (atomic, DRY).

    Both the LIVE streamer (:class:`RunEventStreamer`) and the settle-time
    :meth:`OrchestratedSearchRunner._build_trace` reconciliation render a
    ``sub_agent`` event the SAME way through these statics — so a reconciled
    lane is byte-identical to the one that streamed live, and the
    "every lane shows just the header + completed" projection bug is fixed in
    one place: a ``start`` carries the probe's pathway/sub-query brief (its first
    substantive line), a ``stop`` carries the real outcome detail rather than a
    bare ``done_reason``.
    """

    @staticmethod
    def lane_name(payload: dict[str, Any]) -> str:
        """The lane's display name — the probe's agent KIND (``scg-path-probe``).

        Reads the ``sub_agent`` payload's ``agent_type`` (the agent kind Lane A
        threads onto the lifecycle event). The EVIDENCE: the old code returned
        ``payload["model"]`` (e.g. ``claude-haiku-4-5``) so every lane was
        labelled by its MODEL, not its role — its own docstring said it should
        be the kind. ``model`` is now exposed SEPARATELY via :meth:`model`.
        Falls back to the literal ``scg-path-probe`` (NEVER the model) when
        ``agent_type`` is absent (a pre-Lane-A transcript).
        """
        return str(payload.get("agent_type") or "scg-path-probe")

    @staticmethod
    def model(payload: dict[str, Any]) -> str | None:
        """The LLM model the lane ran on (``sub_agent`` payload ``model``).

        Surfaced as its OWN field per the wire contract — the model is honest
        provenance, just not the lane's display name (which is its KIND).
        """
        value = payload.get("model")
        return str(value) if value else None

    @staticmethod
    def source_id(payload: dict[str, Any]) -> str:
        """The spawning parent's id (the lane's grouping key in the console)."""
        return str(payload.get("parent_id") or "")

    @staticmethod
    def line(payload: dict[str, Any]) -> TraceLine:
        """Render one ``sub_agent`` lifecycle event into a :class:`TraceLine`.

        A ``start`` brief is condensed to its first substantive line (the
        pathway/sub-query), so the lane opens with the probe's actual target
        rather than the multi-line task header; a ``stop`` keeps its real
        outcome detail; intermediate ``message`` lines pass through verbatim.
        """
        action = str(payload.get("action") or "")
        raw = str(payload.get("detail") or action)
        is_stop = action == "stop"
        text = ProbeTrace._first_substantive(raw) if action == "start" else raw
        return TraceLine(
            t_ms=0,
            glyph="✓" if is_stop else "·",
            text=text,
            done=is_stop,
        )

    @staticmethod
    def _first_substantive(brief: str) -> str:
        """The probe's pathway/sub-query line, skipping system-prompt boilerplate.

        The EVIDENCE (run-797097e4b1): the hypervisor puts the probe's SYSTEM
        PROMPT first in the ``start`` brief ("You are an scg-path-probe leaf
        executor…") and the real ``SUB-QUERY:`` / ``PATHWAY:`` task brief lands
        ~7 KB in — so the old "first non-empty line" surfaced the prompt
        boilerplate as the lane's opening line. Prefer the first line that
        carries a ``SUB-QUERY:`` or ``PATHWAY:`` marker (the probe's actual
        target); fall back to the first substantive non-boilerplate line, then
        any non-empty line.
        """
        lines = [ln.strip() for ln in brief.splitlines() if ln.strip()]
        for line in lines:
            upper = line.upper()
            if upper.startswith("SUB-QUERY:") or upper.startswith("PATHWAY:"):
                return line[:_PROBE_LINE_CAP]
        for line in lines:
            if not ProbeTrace._is_boilerplate(line):
                return line[:_PROBE_LINE_CAP]
        return (lines[0] if lines else "probe")[:_PROBE_LINE_CAP]

    @staticmethod
    def _is_boilerplate(line: str) -> bool:
        """True for an obvious system-prompt header line (the leaf-executor preamble)."""
        lowered = line.lower()
        return lowered.startswith("you are ") or lowered.startswith("your task")

    @staticmethod
    def result_text(payload: dict[str, Any]) -> str:
        """The probe's compressed evidence block, echoed on its ``stop`` event.

        ``spawn_agent`` threads the child's ``task_result`` onto the ``stop``
        lifecycle event as ``summary`` (the lifecycle ``detail`` is only the
        ``done_reason``), so the trace can surface what each probe actually
        returned — the ``EVIDENCE (pathway: …)`` / ``NO DATA on pathway …`` block
        — not just that it finished.
        """
        return str(payload.get("summary") or "")

    @staticmethod
    def is_dead_end(result: str) -> bool:
        """True when a probe's evidence block is a ``NO DATA`` verdict (or empty).

        Reads the documented ``scg-path-probe`` contract: a probe returns either
        an ``EVIDENCE (pathway: …)`` block or a ``NO DATA on pathway …`` dead-end.
        This is the single deterministic classifier the run uses to (a) style a
        dead-ended lane distinctly and (b) derive the synthesis
        ``confidence``/``sources_count`` (see ``OrchestratedSearchRunner``).
        """
        stripped = (result or "").strip()
        return not stripped or stripped.upper().startswith("NO DATA")


# The synthetic agent id for the root coordinator's lane — a search RUN whose
# root agent inlines all work (the fast-tier path with no probe sub-agents)
# otherwise streams nothing, so the console sits empty for the whole run. The
# coordinator lane projects the ROOT's tool activity into the same ``agent_*``
# vocabulary as the probes (one extra lane, no wire change).
_COORDINATOR_AGENT_ID = "coordinator"
_COORDINATOR_NAME = "scg-search"
# The compact digest of one root tool_result — tool_id + a short input hint +
# ok/error. Hard-capped so a result snippet (which could carry secrets) is NEVER
# echoed: only the tool_id, brief input keys/values, and the success marker.
_COORDINATOR_LINE_CAP = 120


class CoordinatorTrace:
    """Pure projection of a root-agent ``tool_result`` event (atomic, DRY).

    The root coordinator's tool activity is the transparency missing from a
    root-inline run (no probe sub-agents). Both the LIVE streamer and the
    settle-time :meth:`OrchestratedSearchRunner._build_trace` render a
    ``tool_result`` the SAME way through these statics — exactly the
    :class:`ProbeTrace` stance — so a reconciled coordinator lane is byte-
    identical to one that streamed live.

    SECURITY: a tool result payload can carry connector data (snippets could
    contain secrets), so :meth:`line` NEVER echoes the raw ``result`` — only the
    ``tool_id``, a short hint of the input keys/values, and the ok/error marker,
    all hard-capped at :data:`_COORDINATOR_LINE_CAP`.
    """

    @staticmethod
    def is_root_event(payload: dict[str, Any]) -> bool:
        """True for a real root tool_result this lane should project.

        Skips ``scg_results`` (its full payload is the result-emit, projected by
        :class:`ResultsProjection`; the lane gets ONE summary line instead — see
        :meth:`emit_line`) only at the digest level: the caller still opens the
        lane on it. A blank ``tool_id`` is ignored.
        """
        return bool(str(payload.get("tool_id") or ""))

    @staticmethod
    def line(payload: dict[str, Any]) -> TraceLine:
        """Render one root ``tool_result`` into a compact, secret-free digest line.

        ``<tool_id>(<hint>) ok|error`` — the ``tool_id``, a brief hint of the
        input keys/values, and the success marker. The raw ``result`` is never
        read. ``scg_results`` gets a dedicated summary line (``emitted N results``)
        rather than its full entry payload.
        """
        tool_id = str(payload.get("tool_id") or "tool")
        success = bool(payload.get("success", True))
        marker = "ok" if success else "error"
        if tool_id == "scg_results":
            text = CoordinatorTrace._results_summary(payload)
        else:
            hint = CoordinatorTrace._input_hint(payload.get("tool_input"))
            text = f"{tool_id}({hint}) {marker}" if hint else f"{tool_id} {marker}"
        return TraceLine(
            t_ms=0,
            glyph="✓" if success else "✗",
            text=text[:_COORDINATOR_LINE_CAP],
            done=False,
        )

    @staticmethod
    def _results_summary(payload: dict[str, Any]) -> str:
        """``emitted N results`` — never the entry payload (could carry data)."""
        tool_input = payload.get("tool_input")
        entries = tool_input.get("results") if isinstance(tool_input, dict) else None
        count = len(entries) if isinstance(entries, list) else 0
        return f"emitted {count} result{'' if count == 1 else 's'}"

    @staticmethod
    def _input_hint(tool_input: Any) -> str:
        """A short, secret-free hint of a tool input's keys/values (capped).

        Stringifies a few scalar key/value pairs (``query=…, k=…``); non-scalar
        values are reduced to their key alone. Never recurses into nested
        structures, so a result blob embedded in an input can't leak.
        """
        if not isinstance(tool_input, dict) or not tool_input:
            return ""
        parts: list[str] = []
        for key, value in tool_input.items():
            if isinstance(value, (str, int, float, bool)):
                token = f"{key}={value}"
            else:
                token = str(key)
            parts.append(token)
            if len(", ".join(parts)) >= _COORDINATOR_LINE_CAP:
                break
        return ", ".join(parts)[:_COORDINATOR_LINE_CAP]


class ResultsProjection:
    """Parse an ``scg_results`` tool_result into wire :class:`SearchResult`s.

    The ``scg_results`` SessionTool is transcript-as-transport (#95): it validates
    + echoes, the api projects. This static maps each emitted entry onto a
    STABLE id — ``r-<run_id8>-<n>`` for the root's emit, ``r-<run_id8>-<agent8>-<n>``
    for a probe's (#102: probes emit their own cards; the agent suffix keeps
    concurrent emitters collision-free) — so the live stream and settle
    reconciliation mint the same ids — the dedup key that keeps a result from
    being emitted twice. Both the live ``_project`` and settle read through here
    (the ProbeTrace stance), so a result is byte-identical either way.
    """

    @staticmethod
    def is_results_event(payload: dict[str, Any]) -> bool:
        """True when this tool_result is an ``scg_results`` emit."""
        return str(payload.get("tool_id") or "") == "scg_results"

    @staticmethod
    def related_questions(payload: dict[str, Any]) -> list[str]:
        """Extract the emit's top-level ``related_questions`` (strings only).

        Carried verbatim from the ``scg_results`` tool input so the run can
        surface the agent's follow-up suggestions. Non-string entries are
        dropped; a missing/blank key yields ``[]``. Last write wins is the
        caller's concern — this only reads one emit.
        """
        tool_input = payload.get("tool_input")
        raw = tool_input.get("related_questions") if isinstance(tool_input, dict) else None
        if not isinstance(raw, list):
            return []
        return [str(q) for q in raw if isinstance(q, str) and q.strip()]

    @staticmethod
    def dedup_keys(result: SearchResult) -> set[str]:
        """The cross-emitter identity keys for *result* (a card matches on ANY).

        The EVIDENCE (run-797097e4b1): a probe emitted 3 cards WITH urls, then
        the root re-emitted 2 of the same repos reconstructed from the probe's
        prose (NO ``url`` key, ``kind`` flipped to docs) — so a url-only key
        could never match the url-less re-emit. A card therefore registers BOTH
        its normalized-url key (when a url is present) AND its
        ``(normalized title, source)`` key (when a title is present): the
        probe's url-bearing card registers both, so the root's url-less re-emit
        (carrying only the title key) collides — first emission wins.
        """
        keys: set[str] = set()
        url = (result.url or "").strip()
        if url:
            keys.add(f"url:{ResultsProjection._normalize_url(url)}")
        title = " ".join((result.title or "").lower().split())
        if title:
            keys.add(f"t:{title}|s:{(result.source or '').lower()}")
        return keys

    @staticmethod
    def dedup_key(result: SearchResult) -> str:
        """The PRIMARY identity key — normalized url, else title+source (or id).

        Kept for callers that want one stable string; :meth:`dedup_keys` is the
        full match set the streamer/settle dedup against.
        """
        keys = ResultsProjection.dedup_keys(result)
        url_keys = sorted(k for k in keys if k.startswith("url:"))
        if url_keys:
            return url_keys[0]
        return next(iter(sorted(keys)), f"id:{result.id}")

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Strip scheme + trailing slash, lowercase the host — a stable url key."""
        rest = url
        for scheme in ("https://", "http://"):
            if rest.lower().startswith(scheme):
                rest = rest[len(scheme):]
                break
        rest = rest.rstrip("/")
        # Lowercase only the host segment (path/query stay case-sensitive).
        if "/" in rest:
            host, _, tail = rest.partition("/")
            return f"{host.lower()}/{tail}"
        return rest.lower()

    @staticmethod
    def parse(
        run_id: str, payload: dict[str, Any], *, emitter: str | None = None
    ) -> list[SearchResult]:
        """Map the emit's entries onto stable-id :class:`SearchResult`s.

        ``emitter`` is the probe ``agent_id`` when the emit came from a probe
        lane (``None`` for the root / a legacy payload): it salts the stable id
        so two agents' emits never collide — and because the same transcript
        event carries the same ``agent_id`` live and at settle, the ids agree
        across both reads (the dedup invariant).

        Drops any entry that fails the wire model (an ungrounded/malformed card)
        rather than failing the whole projection — the tool already validated on
        the way in, this is the lenient read side. Fields map honestly onto
        ``SearchResult``: ``confidence`` is surfaced verbatim on the wire (#102)
        AND folded into ``relevance`` ONLY when ``relevance`` was not supplied
        (never overwriting an explicit rank) — keeping the strongest available
        signal on the projected card so the settle metrics can read it.
        """
        tool_input = payload.get("tool_input")
        entries = tool_input.get("results") if isinstance(tool_input, dict) else None
        if not isinstance(entries, list):
            return []
        prefix = run_id[:8]
        if emitter:
            prefix = f"{prefix}-{emitter[:8]}"
        out: list[SearchResult] = []
        for n, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            try:
                out.append(ResultsProjection._to_result(f"r-{prefix}-{n}", entry))
            except Exception as exc:  # noqa: BLE001 — a bad card never fails the run
                logging.debug("scg_results entry {} dropped: {}", n, exc)
        return out

    @staticmethod
    def _to_result(result_id: str, entry: dict[str, Any]) -> SearchResult:
        """Build one :class:`SearchResult` from an emitted entry dict.

        ``relevance`` carries the entry's explicit rank; absent (or 0), the
        entry's ``confidence`` is folded in as the best available signal so the
        card and the settle metrics aren't blind. ``confidence`` ALSO rides its
        own wire field verbatim (#102) so the console can render the emitting
        agent's per-card certainty beside the relevance rank.
        """
        raw_kind = str(entry.get("kind") or "docs")
        if raw_kind not in _RESULT_KINDS:
            raise ValueError(f"unknown result kind: {raw_kind}")
        kind = cast(ResultKindLiteral, raw_kind)
        url = entry.get("url")
        raw_confidence = entry.get("confidence")
        relevance = float(entry.get("relevance") or 0.0)
        if relevance <= 0.0 and raw_confidence is not None:
            relevance = float(raw_confidence or 0.0)
        return SearchResult(
            id=result_id,
            source=str(entry.get("source") or ""),
            kind=kind,
            relevance=relevance,
            confidence=float(raw_confidence) if raw_confidence is not None else None,
            title=str(entry.get("title") or ""),
            url=str(url) if url else "",
            snippet=str(entry.get("snippet") or ""),
            meta=ResultsProjection._scalar_meta(entry.get("meta")),
        )

    @staticmethod
    def _scalar_meta(raw: Any) -> dict[str, str | int | float | bool] | None:
        """Keep only scalar key/values from an emitted ``meta`` dict, else None.

        Agent-emitted ``meta`` rides verbatim BUT scalars only — a non-dict, or
        a value that is not str/int/float/bool, is dropped SILENTLY so a
        connector blob (which could carry data/PII) can never ride this field.
        ``bool`` is checked before ``int`` is irrelevant here (both are kept);
        an all-non-scalar dict collapses to ``None``.
        """
        if not isinstance(raw, dict):
            return None
        out: dict[str, str | int | float | bool] = {}
        for key, value in raw.items():
            if isinstance(value, bool) or isinstance(value, (str, int, float)):
                out[str(key)] = value
        return out or None


class RunEventStreamer:
    """Live transcript→run-event projector for one search/structured run.

    One instance per drive. Holds the run id + store + the per-agent streaming
    state (which probes have opened a lane, how many lines each has emitted) so
    the live consumer and the settle reconciliation agree on what is already on
    the wire. Subscribe → :meth:`start` the consumer thread → drive → settle →
    :meth:`stop`. Thread-safe: a single lock guards the per-agent state shared
    between the consumer thread and the settling worker thread.
    """

    def __init__(self, *, run_id: str, store: Any, bus: SessionEventBus) -> None:
        """Bind the run + store + the (already-resolved) ``SessionEventBus``.

        ``store`` is the agentic-search run store (typed ``Any`` only because the
        dual JSON/Mongo base is injected by the caller); ``bus`` is the core
        per-session pub/sub the SideStage streaming seam already uses.
        """
        self._run_id = run_id
        self._store = store
        self._bus = bus
        self._lock = threading.Lock()
        self._agents: dict[str, _LaneState] = {}
        self._order: list[str] = []
        # The root coordinator lane opens on the first root ``tool_result`` (#95);
        # its slot orders with the probe lanes by event arrival (first-seen
        # ordinal), so a root-inline run still streams transparency. ``None`` until
        # opened; ``done`` flips at settle's ``agent_done``.
        self._coordinator: _LaneState | None = None
        # Result ids already emitted (live + reconcile dedup), so a settle pass
        # never re-emits a result the live stream already wrote.
        self._results_seen: set[str] = set()
        # Cross-emitter SEMANTIC dedup (#run-797097e4b1): a normalized-url (or
        # title+source) key so the root's url-less re-emit of a probe's card
        # collapses to the probe's (first emission wins). Separate from the
        # id-set above (which only catches a re-read of the SAME emit).
        self._result_keys: set[str] = set()
        # TRUE per-emitter KEPT card count — credited to each lane's agent_done so
        # a probe that emitted 3 cards reports 3 (the old hardcoded 0 was blind).
        self._results_by_emitter: dict[str, int] = {}
        # Per-emitter RAW emit count (before cross-emitter dedup). The
        # ``returned − kept`` delta is the lane's "N filtered" — how much it
        # contributed that collapsed into another lane's card.
        self._returned_by_emitter: dict[str, int] = {}
        # Follow-up suggestions from any ``scg_results`` emit (last write wins;
        # the runner folds the final value into ``RunPayload.related_questions``).
        self._related_questions: list[str] = []
        self._subscription: Subscription | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def subscribe(self, session_id: str) -> None:
        """Subscribe to *session_id* BEFORE the drive so no early event is missed.

        Subscribing up-front (not at consume time) closes the race where a fast
        first probe spawns before the consumer thread is scheduled.
        """
        self._subscription = self._bus.subscribe(session_id)

    def start(self) -> None:
        """Spin up the daemon consumer thread draining the subscription."""
        if self._subscription is None:
            return
        self._thread = threading.Thread(
            target=self._consume, name=f"run-streamer-{self._run_id}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the consumer to drain-and-exit; join briefly.

        Called after the drive returns (the transcript is complete). The
        consumer drains whatever is still queued, then exits on the next empty
        poll — so a probe event that landed between the last drain and the drive
        return is still projected before settle reconciles.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._subscription is not None:
            try:
                self._bus.unsubscribe(self._subscription.session_id, self._subscription)
            except Exception as exc:  # noqa: BLE001 — best-effort teardown
                logging.debug("run streamer unsubscribe failed: {}", exc)

    # -- live consume ------------------------------------------------------

    def _consume(self) -> None:
        """Drain the subscription queue, projecting each event until stopped.

        Blocks on ``queue.get(timeout=...)`` so a published event wakes it
        immediately (no busy poll). Exits once :meth:`stop` is signalled AND the
        queue is drained — guaranteeing the tail events are projected.
        """
        import queue as _queue

        sub = self._subscription
        if sub is None:
            return
        while True:
            try:
                record = sub.queue.get(timeout=0.2)
            except _queue.Empty:
                if self._stop.is_set():
                    return
                continue
            try:
                self._project(record)
            except Exception as exc:  # noqa: BLE001 — a bad event never stalls the stream
                logging.debug("run streamer projection failed: {}", exc)

    def _project(self, record: EventRecord) -> None:
        """Project one transcript event onto the run event log (live).

        Three event classes share this seam: a probe's ``sub_agent`` lifecycle
        (the parent's view of its children), the ROOT coordinator's
        ``tool_result`` events (#95), and a PROBE's own ``tool_result`` events.
        The last class exists because a child loop INHERITS the parent's
        ``event_logger`` (core ``AgentContext.child``) — probe tool calls land
        on THIS session's transcript/bus stamped with the probe's ``agent_id``
        (#102; the original #95 premise that they "live in the probes' own
        sessions" was wrong, verified live). A probe ``tool_result`` must
        therefore be classified by ``payload.agent_id`` against the known probe
        lanes (the spawn's ``sub_agent`` ``start`` always precedes the child's
        first tool call, so the lane is known by the time its tools fire) —
        only its ``scg_results`` emit projects (as the probe's result cards);
        everything else stays off the coordinator lane.
        """
        kind = record.get("type")
        raw = record.get("payload")
        payload: dict[str, Any] = raw if isinstance(raw, dict) else {}
        if kind == "sub_agent":
            self._project_sub_agent(payload)
        elif kind == "tool_result":
            emitter = self._probe_emitter(payload)
            if emitter is not None:
                self._project_probe_tool(emitter, payload)
            else:
                self._project_coordinator(payload)

    def _probe_emitter(self, payload: dict[str, Any]) -> str | None:
        """The probe ``agent_id`` when this tool_result came from a probe lane.

        ``None`` for the root's own tool calls (its ``agent_id`` never opens a
        probe lane) and for legacy payloads with no ``agent_id`` — both keep
        the historical coordinator-lane path.
        """
        agent_id = str(payload.get("agent_id") or "")
        if not agent_id:
            return None
        with self._lock:
            return agent_id if agent_id in self._agents else None

    def _project_probe_tool(self, emitter: str, payload: dict[str, Any]) -> None:
        """Project a probe's own ``tool_result`` (#102).

        A probe's ``scg_results`` emit reaches the run log as ITS result cards
        (probe-salted stable ids) AND credits the probe lane. Every OTHER probe
        tool call now also projects ONE secret-free digest line onto the probe's
        lane (tool_id + capped input hint, NEVER the result payload) — the
        EVIDENCE: a probe's only real data fetch
        (``mcp_github_search_repositories``) was dropped entirely, so the lane
        showed just prompt + "completed". The probe's terminal evidence still
        rides its ``stop`` summary (#86); these digests are the in-flight steps.
        """
        if ResultsProjection.is_results_event(payload):
            self._record_related_questions(payload)
            results = ResultsProjection.parse(self._run_id, payload, emitter=emitter)
            self._emit_results(results, emitter=emitter)
            return
        # A non-results probe tool call → one digest line on the probe's lane.
        if CoordinatorTrace.is_root_event(payload):
            self._append_probe_line(emitter, CoordinatorTrace.line(payload))

    def _append_probe_line(self, emitter: str, line: TraceLine) -> None:
        """Append one trace line to an already-open probe lane (live digests)."""
        with self._lock:
            state = self._agents.get(emitter)
            if state is None:
                return
            state.lines += 1
        self._store.append_run_event(
            self._run_id, events.agent_line(agent_id=emitter, line=line)
        )

    def _record_related_questions(self, payload: dict[str, Any]) -> None:
        """Capture an emit's ``related_questions`` (last write wins)."""
        questions = ResultsProjection.related_questions(payload)
        if questions:
            with self._lock:
                self._related_questions = questions

    def related_questions(self) -> list[str]:
        """The last-written follow-up suggestions across all emits (for settle)."""
        with self._lock:
            return list(self._related_questions)

    def _project_sub_agent(self, payload: dict[str, Any]) -> None:
        """Project one probe ``sub_agent`` lifecycle event onto a probe lane."""
        agent_id = str(payload.get("agent_id") or "")
        if not agent_id:
            return
        is_stop = str(payload.get("action") or "") == "stop"

        with self._lock:
            state = self._agents.get(agent_id)
            opened = state is not None
            if state is None:
                state = _LaneState(slot=len(self._order))
                self._order.append(agent_id)
                self._agents[agent_id] = state
            already_done = state.done

        if not opened:
            self._store.append_run_event(
                self._run_id,
                events.agent_start(
                    agent_id=agent_id,
                    source_id=ProbeTrace.source_id(payload),
                    name=ProbeTrace.lane_name(payload),
                    slot=state.slot,
                ),
            )

        # Every lifecycle line becomes a trace line; ``stop`` marks it done.
        self._store.append_run_event(
            self._run_id,
            events.agent_line(agent_id=agent_id, line=ProbeTrace.line(payload)),
        )
        with self._lock:
            state.lines += 1
            if is_stop and not already_done:
                state.done = True

        if is_stop and not already_done:
            result_text = ProbeTrace.result_text(payload)
            # Credit the lane its TRUE card count — the playbook has each probe
            # emit once BEFORE its evidence block, so the emit's tool_result has
            # already been classified onto this lane by stop time.
            self._store.append_run_event(
                self._run_id,
                events.agent_done(
                    agent_id=agent_id,
                    results_count=self.results_for_emitter(agent_id),
                    returned_count=self.returned_for_emitter(agent_id),
                    empty=ProbeTrace.is_dead_end(result_text),
                    result=result_text,
                ),
            )

    def _project_coordinator(self, payload: dict[str, Any]) -> None:
        """Project one root ``tool_result`` onto the coordinator lane (#95).

        Opens the lane on the FIRST root tool_result (its slot orders with the
        probe lanes by event arrival). Each event → one secret-free digest line
        via :class:`CoordinatorTrace`. An ``scg_results`` emit ALSO mints the run's
        ``result`` events here (live), deduped by stable id, and marks the
        coordinator data-bearing. The lane's ``agent_done`` fires at settle.
        """
        if not CoordinatorTrace.is_root_event(payload):
            return

        with self._lock:
            opened = self._coordinator is not None
            if self._coordinator is None:
                self._coordinator = _LaneState(slot=len(self._order))
                self._order.append(_COORDINATOR_AGENT_ID)
            slot = self._coordinator.slot

        if not opened:
            self._store.append_run_event(
                self._run_id,
                events.agent_start(
                    agent_id=_COORDINATOR_AGENT_ID,
                    source_id="",
                    name=_COORDINATOR_NAME,
                    slot=slot,
                ),
            )

        self._store.append_run_event(
            self._run_id,
            events.agent_line(
                agent_id=_COORDINATOR_AGENT_ID, line=CoordinatorTrace.line(payload)
            ),
        )
        with self._lock:
            self._coordinator.lines += 1

        if ResultsProjection.is_results_event(payload):
            self._record_related_questions(payload)
            self._emit_results(
                ResultsProjection.parse(self._run_id, payload),
                emitter=_COORDINATOR_AGENT_ID,
            )

    def _emit_results(
        self, results: list[SearchResult], *, emitter: str | None = None
    ) -> None:
        """Append a ``result`` event per NEW result + credit the emitting lane.

        Dedup is BOTH the stable id (a re-read of the same emit) AND the
        cross-emitter SEMANTIC key (normalized url, else title+source) so the
        root's url-less re-emit of a probe's card never doubles it — FIRST
        emission wins (probe cards beat later root re-emissions because the
        probe's ``sub_agent`` events precede the root synthesis). A card that
        survives both is written once and credited to *emitter* (the lane whose
        ``agent_done.results_count`` reports it).
        """
        for item in results:
            with self._lock:
                # Credit RAW emit first (every parsed card the lane returned,
                # before dedup) so the lane's "N filtered" delta is honest.
                if emitter is not None:
                    self._returned_by_emitter[emitter] = (
                        self._returned_by_emitter.get(emitter, 0) + 1
                    )
                if item.id in self._results_seen:
                    continue
                keys = ResultsProjection.dedup_keys(item)
                if keys & self._result_keys:
                    continue
                self._results_seen.add(item.id)
                self._result_keys |= keys
                if emitter is not None:
                    self._results_by_emitter[emitter] = (
                        self._results_by_emitter.get(emitter, 0) + 1
                    )
            self._store.append_run_event(self._run_id, events.result(item=item))

    def results_for_emitter(self, emitter: str) -> int:
        """The count of UNIQUE (kept) cards credited to *emitter* (for agent_done)."""
        with self._lock:
            return self._results_by_emitter.get(emitter, 0)

    def returned_for_emitter(self, emitter: str) -> int:
        """The RAW emit count for *emitter* (kept + deduped-away — for agent_done)."""
        with self._lock:
            return self._returned_by_emitter.get(emitter, 0)

    # -- settle reconciliation --------------------------------------------

    def streamed_agent_ids(self) -> set[str]:
        """The agent ids that have already been opened on the wire (for settle)."""
        with self._lock:
            return set(self._agents)

    def reconcile_missing(self, trace: list[TraceAgent]) -> None:
        """Flush any trace agent the live stream did not already emit.

        The settle path builds the full trace from the finished transcript; this
        back-stops a fast run whose ``sub_agent`` events were never drained
        live (the ``completion`` landed first) or a bus drop. Each missing agent
        gets the full ``agent_start`` → ``agent_line*`` → ``agent_done`` it would
        have streamed, so the snapshot is always complete even if the live path
        was bypassed. Already-streamed agents are left untouched — no duplicates.
        """
        streamed = self.streamed_agent_ids()
        for agent in trace:
            if agent.agent_id in streamed:
                continue
            self._store.append_run_event(
                self._run_id,
                events.agent_start(
                    agent_id=agent.agent_id,
                    source_id=agent.source_id,
                    name=agent.name,
                    slot=agent.slot,
                ),
            )
            for line in agent.lines:
                self._store.append_run_event(
                    self._run_id,
                    events.agent_line(agent_id=agent.agent_id, line=line),
                )
            self._store.append_run_event(
                self._run_id,
                events.agent_done(
                    agent_id=agent.agent_id,
                    # The settle-built trace carries the lane's true card count
                    # (the runner credited it from the transcript) — never the
                    # old hardcoded 0.
                    results_count=agent.results_count,
                    returned_count=agent.returned_count,
                    empty=not agent.lines or ProbeTrace.is_dead_end(agent.result),
                    result=agent.result,
                ),
            )

    # -- coordinator + results settle (the root-inline lane, #95) ----------

    def coordinator_opened(self) -> bool:
        """True when the root coordinator lane opened (any root tool_result ran)."""
        with self._lock:
            return self._coordinator is not None

    def reconcile_coordinator(
        self,
        lines: list[TraceLine],
        *,
        slot: int | None = None,
        has_data: bool,
        results_count: int | None = None,
        returned_count: int | None = None,
    ) -> None:
        """Close the coordinator lane at settle (and back-stop a live-missed lane).

        ``agent_done`` always fires here (done at terminal — the coordinator has no
        ``stop`` lifecycle event of its own). If the live stream never opened the
        lane (a fast run whose ``tool_result`` events were never drained), this
        flushes the full ``agent_start`` → ``agent_line*`` it would have streamed
        first, from the settle-built *lines*, at *slot* (the coordinator's
        merged-stream first-seen ordinal computed by the runner) so the lane
        orders identically to a live open. Already-streamed lines are left
        untouched (no duplicates). ``empty`` is False iff the run produced data
        (results emitted, or any data-bearing probe — the caller folds both).
        """
        with self._lock:
            opened = self._coordinator is not None
            if self._coordinator is None:
                resolved = slot if slot is not None else len(self._order)
                self._coordinator = _LaneState(slot=resolved)
                self._order.append(_COORDINATOR_AGENT_ID)
            if self._coordinator.done:
                return
            self._coordinator.done = True
            slot = self._coordinator.slot
            already_lined = self._coordinator.lines

        if not opened:
            self._store.append_run_event(
                self._run_id,
                events.agent_start(
                    agent_id=_COORDINATOR_AGENT_ID,
                    source_id="",
                    name=_COORDINATOR_NAME,
                    slot=slot,
                ),
            )
            for line in lines[already_lined:]:
                self._store.append_run_event(
                    self._run_id,
                    events.agent_line(agent_id=_COORDINATOR_AGENT_ID, line=line),
                )

        # The coordinator's OWN cards (root-inlined emits), not the run total —
        # a probe's cards are credited to its probe lane. Prefer the runner's
        # settle-computed count (covers a fast run the live stream missed); fall
        # back to the live per-emitter tally.
        coordinator_count = (
            results_count
            if results_count is not None
            else self.results_for_emitter(_COORDINATOR_AGENT_ID)
        )
        coordinator_returned = (
            returned_count
            if returned_count is not None
            else self.returned_for_emitter(_COORDINATOR_AGENT_ID)
        )
        self._store.append_run_event(
            self._run_id,
            events.agent_done(
                agent_id=_COORDINATOR_AGENT_ID,
                results_count=coordinator_count,
                returned_count=coordinator_returned,
                empty=not has_data,
                result="",
            ),
        )

    def reconcile_results(self, results: list[SearchResult]) -> None:
        """Emit any settle-built result not already streamed live (dedup by id+key).

        A back-stop for a fast run whose ``scg_results`` tool_results were never
        drained live. Per-lane crediting is NOT done here (emitter unknown at
        this flat-list seam) — the authority is the settle-built trace's
        per-lane ``results_count``, which the runner computes from the
        transcript and ``reconcile_missing`` / ``reconcile_coordinator`` honour.
        """
        self._emit_results(results)


__all__ = [
    "RunEventStreamer",
    "ProbeTrace",
    "CoordinatorTrace",
    "ResultsProjection",
]
