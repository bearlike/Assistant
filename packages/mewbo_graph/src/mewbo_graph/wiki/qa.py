"""QA answer finalization — reconcile the snapshot from the event log and close it.

The wiki-qa hypervisor renders its answer purely through ``wiki_emit_block`` events.
The terminal ``sources`` block is the answer's *accept state* (the
``EmitStructuredResponseTool`` pattern): emitting it drives :meth:`QaFinalizer.close`,
which rebuilds the ``QaAnswer`` snapshot from the append-only log and appends the
terminal ``complete`` event in one clean step — so a reloaded/shared answer is never
empty and the SSE stream ends cleanly instead of by idle-timeout.

This lives in the library (next to ``QaAnswer`` + the store it mutates), down-only, so
BOTH the terminal ``wiki_emit_block`` (happy path, same layer) and the API's
``on_session_end`` net (imports down) can call it. The deterministic provenance —
every graph node / file / page a probe touched — is captured as ``access`` events by
the probe tools and folded here, so ``accessed_sources`` needs no transport-layer help.
``models_used`` is the one field that needs the session transcript, so it arrives via
:meth:`enrich` from the API hook.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mewbo_core.common import get_logger

from mewbo_graph.wiki.memory_types import MAX_INSIGHT_CHARS
from mewbo_graph.wiki.types import QaAnswer

if TYPE_CHECKING:
    from mewbo_graph.wiki.store import WikiStoreBase
    from mewbo_graph.wiki.structure_provider import CodeStructureProvider
    from mewbo_graph.wiki.types import InlineNode

logging = get_logger(name="mewbo_graph.wiki.qa")


class QaFinalizer:
    """Reconcile a QA answer snapshot from its event log and close it.

    Stateless façade over an injected store (the wiki store owns the QA state).
    :meth:`close` is idempotent — a second call after a terminal event is a no-op —
    so it is safe under the happy path (terminal ``wiki_emit_block``) racing the
    ``on_session_end`` net, and under SSE reconnects.
    """

    _TERMINAL: frozenset[str] = frozenset({"complete", "cancelled", "error"})

    @classmethod
    def close(cls, store: WikiStoreBase, answer_id: str, error: str | None = None) -> bool:
        """Reconcile the snapshot and append the terminal event. False if already closed.

        Folds three things off the append-only log into the persisted snapshot:
        the emitted ``blocks`` (so reload/share works), the curated
        ``summary_sources`` (the LLM's sources block), and ``accessed_sources``
        (the deterministic probe trail). Then appends ``complete`` (or ``error``).
        """
        events = store.load_qa_events(answer_id)
        if any(ev.get("type") in cls._TERMINAL for ev in events):
            return False  # already terminal — idempotent

        blocks = cls._blocks_from_events(events)
        snap = store.get_qa(answer_id)
        if snap is not None:
            data = snap.model_dump(by_alias=True)
            data["blocks"] = blocks
            data["summarySources"] = cls._summary_sources(events, blocks)
            data["accessedSources"] = cls._accessed_from_events(events)
            # The terminal status on the snapshot, so a non-streaming consumer
            # (the MCP ``ask_wiki`` poll) sees an authoritative done-signal.
            data["status"] = "error" if error else "complete"
            # NON-destructive: save_qa would reset the Mongo event_count (→ the
            # ``complete`` append collides at idx 0) and drop session_id.
            store.update_qa_fields(QaAnswer.model_validate(data))

        if error:
            store.append_qa_event(
                answer_id, {"type": "error", "error": {"code": "internal", "message": error}}
            )
        else:
            store.append_qa_event(answer_id, {"type": "complete", "totalBlocks": len(blocks)})
        return True

    @classmethod
    def enrich(
        cls,
        store: WikiStoreBase,
        answer_id: str,
        *,
        models: list[str] | None = None,
    ) -> None:
        """Stamp transcript-derived metadata (``models_used``) onto the snapshot.

        Independent of the terminal state: the API ``on_session_end`` net fires
        AFTER the terminal ``wiki_emit_block`` already closed the happy path, so
        models must be writable post-close. No-op when nothing is supplied.
        """
        if not models:
            return
        snap = store.get_qa(answer_id)
        if snap is None:
            return
        data = snap.model_dump(by_alias=True)
        data["modelsUsed"] = cls._dedup(models)
        store.update_qa_fields(QaAnswer.model_validate(data))

    # ── log → snapshot projections (static, deterministic) ──────────────────

    @staticmethod
    def _blocks_from_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Collect ``block_open`` payloads in strictly-increasing index order."""
        by_index: dict[int, dict[str, Any]] = {}
        for ev in events:
            if ev.get("type") == "block_open" and isinstance(ev.get("block"), dict):
                by_index[int(ev.get("index", len(by_index)))] = ev["block"]
        return [by_index[i] for i in sorted(by_index)]

    @staticmethod
    def _summary_sources(
        events: list[dict[str, Any]], blocks: list[dict[str, Any]]
    ) -> list[str]:
        """Prefer an explicit ``summary_ready``; else derive from the final sources block."""
        for ev in events:
            if ev.get("type") == "summary_ready" and ev.get("sources"):
                return [str(s) for s in ev["sources"]]
        for blk in reversed(blocks):
            if blk.get("kind") == "sources":
                return [str(i) for i in blk.get("items", []) if str(i).startswith("wiki:")]
        return []

    @classmethod
    def _accessed_from_events(cls, events: list[dict[str, Any]]) -> list[str]:
        """De-duplicate every ref the probes recorded via ``access`` events."""
        refs: list[str] = []
        for ev in events:
            if ev.get("type") == "access":
                refs.extend(str(r) for r in ev.get("refs", []))
        return cls._dedup(refs)

    @staticmethod
    def _dedup(items: list[str]) -> list[str]:
        """Order-preserving de-duplication."""
        seen: set[str] = set()
        out: list[str] = []
        for it in items:
            if it and it not in seen:
                seen.add(it)
                out.append(it)
        return out


class QaMemoryDepositor:
    """Distill a finalized Q&A answer into memory note(s) and graft them onto the multiplex.

    The post-QA half of the memory flywheel (Gitea #13 "Flywheel"): the indexer
    deposits a few atomic insights *while indexing*, and every finalized answer
    deposits one more — so the memory layer is useful from day one and graph
    connections strengthen with each question. This runs in the API's
    ``on_session_end`` net **after** the answer is delivered, so it is entirely
    off the user's latency path.

    Stateless façade over the injected store + the shared :class:`InsightIngestor`
    (the ONE memory writer — no second fan-out, no parallel writer). Distillation
    reuses the ingestor's condenser when one is configured (the same "distill →
    atomic claims" path the human REST/MCP insight surface uses); when no
    condenser is available it degrades to a single deterministic claim from the
    question + the answer's lead paragraph. Anchors are derived from the answer's
    cited/accessed sources so the note grounds to the real code entities it cites.

    Idempotent: the memory node id is content-addressed
    (:meth:`MemoryNode.compute_node_id`), so a retry/recovery that re-deposits the
    same answer collapses onto the same node (the exact-dup dedup tier) — safe to
    call repeatedly. Best-effort: any failure is logged and swallowed; enrichment
    must NEVER fail or block the Q&A flow.
    """

    @classmethod
    def deposit(
        cls,
        store: WikiStoreBase,
        answer: QaAnswer,
        *,
        question: str | None = None,
    ) -> int:
        """Distill *answer* into memory note(s) and ingest them. Returns count ingested (0 on skip).

        Never raises — wraps the body so a failing deposit logs and returns 0.
        Skips (returns 0) when the answer has no slug (a slug-less answer would
        pollute the empty-string corpus) or no distillable claim.
        """
        try:
            slug = (answer.slug or "").strip()
            if not slug:
                return 0  # never ingest into the "" corpus

            body = cls._answer_text(answer)
            if not body:
                return 0

            anchors = cls._anchors_from_sources(store, answer)
            from mewbo_graph.wiki.memory import InsightIngestor  # noqa: PLC0415

            ingestor = InsightIngestor.from_store(store)
            # Prefer the ingestor's condenser (distill → atomic refined claims).
            # ``condense=True`` is safe with NO condenser configured: the ingestor
            # degrades to a single ≤200-char claim, so we feed the deterministic
            # one-line distillation (question + lead answer) as the raw input and
            # let the condenser split it further when one is wired.
            claim = cls._distill(question, body)
            if not claim:
                return 0
            result = ingestor.ingest(
                slug,
                raw=claim,
                anchors=anchors,
                kind="propositional",
                labels=["qa"],
                corpus="code",
                condense=True,
                source="qa",
                author_agent="wiki-qa",
                session_id=answer.answer_id,
            )
            return sum(1 for c in result.claims if c.action != "rejected")
        except Exception:  # pragma: no cover — best-effort; must never block QA
            logging.warning("QA memory deposit failed", exc_info=True)
            return 0

    # ── distillation + anchor derivation (static, deterministic) ─────────────

    @classmethod
    def _distill(cls, question: str | None, body: str) -> str:
        """One refined claim: ``Q: … A: …`` when the question is known, else the answer body.

        Kept compact so that when NO condenser is configured the single-claim
        fallback stays within :data:`MAX_INSIGHT_CHARS`; a wired condenser
        decomposes the fuller text into atomic claims of its own.
        """
        body = body.strip()
        q = (question or "").strip()
        if not q:
            return cls._truncate(body, MAX_INSIGHT_CHARS)
        # Reserve room for the "Q: <q> A: " scaffold within the single-claim cap.
        scaffold = "Q:  A: "
        budget = MAX_INSIGHT_CHARS - len(scaffold)
        q_part = cls._truncate(q, budget // 2)
        a_part = cls._truncate(body, budget - len(q_part))
        return f"Q: {q_part} A: {a_part}".strip()

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        """Hard-clip *text* to *limit* chars (single-claim fallback safety)."""
        text = " ".join(text.split())
        return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"

    @classmethod
    def _answer_text(cls, answer: QaAnswer) -> str:
        """The direct answer: the lead ``p`` block flattened to plain text."""
        for block in answer.blocks:
            root = getattr(block, "root", block)
            if getattr(root, "kind", None) == "p":
                text = cls._flatten_inline(getattr(root, "text", ""))
                if text.strip():
                    return text
        return ""

    @classmethod
    def _flatten_inline(cls, node: InlineNode | str | list[InlineNode] | dict[str, object]) -> str:
        """Flatten a recursive ``InlineNode`` (str | list | dict) to plain text."""
        node = getattr(node, "root", node)  # unwrap InlineNode RootModel
        if isinstance(node, str):
            return node
        if isinstance(node, list):
            return "".join(cls._flatten_inline(n) for n in node)
        if isinstance(node, dict):
            # {code:str} | {link,text} | {kind:src,path,...} — prefer human text.
            for key in ("text", "code", "path"):
                val = node.get(key)
                if isinstance(val, str):
                    return val
        return ""

    @classmethod
    def _anchors_from_sources(cls, store: WikiStoreBase, answer: QaAnswer) -> list[str]:
        """Map the answer's cited/accessed refs to code ``entity_key`` anchors.

        The probe trail records refs as ``graph:<node_id>``, ``<path>#L<a>-<b>``,
        bare ``<path>``, ``entity:<id>``, or ``wiki:<page-id>``.
        ``InsightIngestor.ingest`` anchors via :class:`CodeStructureProvider`,
        which resolves a code **entity_key** (``path#Symbol`` / bare ``path``) —
        NOT a raw graph node id — so we normalize each ref here:

        * ``graph:<node_id>`` — resolve to its ``entity_key`` via the structure
          provider (the only place that maps a node id → key); drop on a miss.
        * ``<path>#L<lines>`` — strip the line range → the bare File ``entity_key``.
        * ``<path>`` / ``<path>#Symbol`` — already an ``entity_key``; pass through.
        * ``entity:<id>`` — pass through pre-split (an abstract-entity anchor).
        * ``wiki:<page-id>`` — SKIP (a page ref, not a code entity).

        Unresolvable keys are harmless: ``InsightIngestor`` drops anchors that
        don't resolve to a live node (with a warning), so this is best-effort.
        """
        from mewbo_graph.wiki.structure_provider import CodeStructureProvider  # noqa: PLC0415

        provider = CodeStructureProvider(store)
        seen: set[str] = set()
        out: list[str] = []
        # accessed_sources first (the deterministic trail), then the curated set.
        for ref in list(answer.accessed_sources) + list(answer.summary_sources):
            key = cls._anchor_key(provider, answer.slug, ref)
            if key and key not in seen:
                seen.add(key)
                out.append(key)
        return out

    @staticmethod
    def _anchor_key(provider: CodeStructureProvider, slug: str, ref: str) -> str | None:
        """Normalize one source ref to an anchor ``entity_key``, or None to skip."""
        ref = (ref or "").strip()
        if not ref or ref.startswith("wiki:"):
            return None  # page ref — not a code entity
        if ref.startswith("entity:"):
            return ref  # pre-split abstract-entity anchor
        if ref.startswith("graph:"):
            # A raw graph node id → resolve to its entity_key (the only mapping).
            return provider.entity_key_of(slug, ref[len("graph:"):])
        # A ``path#L<a>-<b>`` slice ref → the bare File entity_key (the line range
        # is not part of an entity_key). A ``path#Symbol`` ref keeps its fragment
        # (it IS an entity_key). Distinguish by the ``L<n>`` shape.
        if "#L" in ref:
            head, _, frag = ref.partition("#")
            if frag.startswith("L") and frag[1:2].isdigit():
                return head
        return ref


__all__ = ["QaFinalizer", "QaMemoryDepositor"]
