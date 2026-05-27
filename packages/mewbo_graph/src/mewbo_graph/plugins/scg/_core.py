"""The import boundary between the ``scg`` plugin and the SCG substrate.

The SCG deterministic core (store, parser, router, aligner, memory bridge,
providers) lives **down** in the same library at ``mewbo_graph.scg``; the wiki
memory substrate this bridge also touches lives down at ``mewbo_graph.wiki``.
Both are imported DOWN — no longer a one-way boundary UP into an app. The wiki
tools each carry a private ``_resolve_runtime()`` / ``_make_embedder()`` late
import; this module consolidates that seam into ONE atomic resolver class so the
six SCG tools share a single, test-patchable bridge (DRY).

Every accessor late-imports inside the call so a CORE-ONLY install — one where
the optional ``mewbo-graph`` library is present but its ``treesitter`` /
``retrieval`` extras (or the API run store) are absent — never fails at import
time; the tool degrades to a structured error instead (mirrors
``wiki._resolve_runtime`` returning ``None``).

Security invariant (spec §6): nothing here copies a token, credential, or
record value — only the redacted structure the core already persists.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mewbo_core.common import MockSpeaker

if TYPE_CHECKING:  # type-only — never imported at runtime in a core-only install
    from collections.abc import Callable

    from mewbo_core.types import Event

    from mewbo_graph.scg.entity_resolution import TypeAligner
    from mewbo_graph.scg.memory_bridge import ScgMemoryBridge
    from mewbo_graph.scg.parser import ScgParser
    from mewbo_graph.scg.router import ScgRouter
    from mewbo_graph.scg.store import ScgStore
    from mewbo_graph.scg.types import SourceDescriptor
    from mewbo_graph.wiki.embedder import Embedder
    from mewbo_graph.wiki.store import WikiStoreBase


# The single message every scg tool returns when the optional SCG core can't be
# imported (a core-only install missing the graph extras). Defined once so the
# six tools stay in sync and point operators at the right package.
SCG_CORE_UNAVAILABLE = "SCG core unavailable (install mewbo-graph[treesitter,retrieval])"


def err_result(code: str, message: str) -> MockSpeaker:
    """Return a ``MockSpeaker`` carrying a structured error (wiki ``_err_result``)."""
    return MockSpeaker(content=str({"error": {"code": code, "message": message}}))


def ok_result(payload: dict[str, object]) -> MockSpeaker:
    """Return a ``MockSpeaker`` carrying a successful payload dict (wiki shape)."""
    return MockSpeaker(content=str(payload))


class SessionToolBase:
    """Shared ``SessionTool`` scaffold for the scg tools (one ``__init__``).

    Every scg tool needs the same per-agent state (owning session id + optional
    event logger) and the same one-shot ``should_terminate_run`` flag. Holding it
    once here lets each tool subclass declare only its ``tool_id`` / ``schema`` /
    ``handle`` — no repeated boilerplate (the SessionTool factory feeds the
    ``session_id`` / ``event_logger`` keywords this ``__init__`` accepts).
    """

    def __init__(
        self,
        session_id: str,
        event_logger: Callable[[Event], None] | None = None,
    ) -> None:
        """Initialise with the owning session id and optional event logger."""
        self._session_id = session_id
        self._event_logger = event_logger
        self._terminate = False

    def should_terminate_run(self) -> bool:
        """Return True once if the run should terminate; resets the flag."""
        v, self._terminate = self._terminate, False
        return v


class ScgCore:
    """Late-binding accessor for the SCG substrate (one seam, all tools).

    Resolves the SCG core from ``mewbo_graph.scg`` (down) and the wiki memory
    substrate from ``mewbo_graph.wiki`` (down). Stateless: every method
    late-imports the requested symbol so an unmet ``treesitter`` / ``retrieval``
    extra (a core-only install) raises :class:`ImportError` *here* (caught by
    callers), never at plugin load. Tests monkeypatch these classmethods to
    inject fakes without touching the network or constructing a real embedder.
    """

    # -- structure store ----------------------------------------------------

    @staticmethod
    def store() -> ScgStore:
        """Return the process-wide SCG structure store (created on first use)."""
        from mewbo_graph.scg.store import get_scg_store  # noqa: PLC0415

        return get_scg_store()

    # -- map-time collaborators --------------------------------------------

    @classmethod
    def parser(cls, store: ScgStore) -> ScgParser:
        """Build an :class:`ScgParser` over *store* with the default providers.

        Schema-bearing providers (OpenAPI + MCP tool list) are auto-registered;
        the embedder is the wiki default (best-effort — a missing backend
        degrades to a structure-only SCG, never a hard failure).
        """
        from mewbo_graph.scg.parser import ScgParser  # noqa: PLC0415
        from mewbo_graph.scg.providers import (  # noqa: PLC0415
            StructureProviderRegistry,
        )

        registry = StructureProviderRegistry.with_defaults()
        return ScgParser(
            store=store,
            providers=registry.providers(),
            aligner=cls.aligner(store),
        )

    @staticmethod
    def aligner(store: ScgStore) -> TypeAligner:
        """Build a :class:`TypeAligner` over *store* (abstain-by-default, no LLM)."""
        from mewbo_graph.scg.entity_resolution import (  # noqa: PLC0415
            TypeAligner,
        )

        return TypeAligner(store=store)

    @staticmethod
    def source_descriptor(
        *, source_id: str, source_type: str, raw: dict[str, object]
    ) -> SourceDescriptor:
        """Validate a :class:`SourceDescriptor` (raises on a malformed payload)."""
        from mewbo_graph.scg.types import (  # noqa: PLC0415
            SourceDescriptor,
        )

        return SourceDescriptor(
            source_id=source_id, source_type=source_type, raw=raw
        )

    # -- query-time collaborators ------------------------------------------

    @staticmethod
    def router(store: ScgStore) -> ScgRouter:
        """Build an :class:`ScgRouter` over *store* with the wiki query embedder."""
        from mewbo_graph.scg.router import ScgRouter  # noqa: PLC0415
        from mewbo_graph.wiki.embedder import make_embedder  # noqa: PLC0415

        return ScgRouter(store=store, embedder=make_embedder())

    @staticmethod
    def embedder() -> Embedder:
        """Build the wiki :class:`Embedder` (query embedding for memory reads)."""
        from mewbo_graph.wiki.embedder import make_embedder  # noqa: PLC0415

        return make_embedder()

    # -- learned-layer flywheel (memory bridge) ----------------------------

    @classmethod
    def memory_bridge(cls, store: ScgStore) -> ScgMemoryBridge:
        """Build an :class:`ScgMemoryBridge` over the wiki memory substrate.

        The bridge needs the wiki memory store (the shared #13 substrate) + an
        embedder. The store is built via the wiki STORE FACTORY directly, NOT
        read off ``wiki.routes._runtime`` — that module global is ``None`` for any
        deployment that never initialised the wiki API, which would make every
        ``scg_memory`` write fail. The factory constructs the configured backend
        unconditionally, so the flywheel works without the wiki API runtime.

        Its anchor resolver is pinned to *store* so connector ``source_key``
        anchors resolve against THIS SCG (the live structure), not the
        process-wide singleton — keeping a test-injected store authoritative.
        Raises :class:`ImportError` only when the optional ``mewbo-graph``
        extras are absent (a core-only install), surfaced as a structured error
        by callers.
        """
        from mewbo_graph.scg.memory_bridge import (  # noqa: PLC0415
            ScgAnchorResolver,
            ScgMemoryBridge,
        )

        wiki_store = cls._wiki_store()
        bridge = ScgMemoryBridge(wiki_store=wiki_store, embedder=cls.embedder())
        bridge.resolver = ScgAnchorResolver(store)
        return bridge

    @staticmethod
    def _wiki_store() -> WikiStoreBase:
        """Construct the configured wiki memory store via its factory.

        Late-imports the wiki store factory so a core-only install (the optional
        ``mewbo-graph`` extras absent) raises :class:`ImportError` here — caught
        by callers and surfaced as a structured error — rather than at plugin
        load.
        """
        from mewbo_graph.wiki.store import create_wiki_store  # noqa: PLC0415

        return create_wiki_store()

    # -- map-job progress (emit_phase) -------------------------------------

    @staticmethod
    def emit_phase(job_id: str, phase: str) -> int | None:
        """Emit a cosmetic map-job ``phase`` through the injected sink; idx or None.

        Map-job progress is persisted in the API *run* store (so it rides the
        SSE plumbing) — a transport concern the API registers as a
        :class:`~mewbo_graph.scg.map_phase.MapPhaseSink` writer at startup.
        Returns ``None`` when no writer is registered (a graph-only install, or
        the API never initialised) so the mapper keeps running: the SCG
        structure write already happened, the phase is purely cosmetic.
        """
        from mewbo_graph.scg.map_phase import MapPhaseSink  # noqa: PLC0415

        return MapPhaseSink.emit(job_id, phase)


__all__ = [
    "SCG_CORE_UNAVAILABLE",
    "ScgCore",
    "SessionToolBase",
    "err_result",
    "ok_result",
]
