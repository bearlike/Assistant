"""``WorkspaceSourceSync`` — refresh a workspace's virtual MCP config + auto-map.

The save/attach hook (#75): whenever a workspace's source selection changes
(``POST`` / ``PATCH`` workspace), this atomic class

1. **refreshes the persisted virtual MCP config** (:class:`WorkspaceMcpConfig`) so
   the DB-backed source-of-truth tracks the new selection, and
2. **auto-maps newly-enabled live sources** into the GLOBAL SCG via the SAME
   ``MapSourceJob`` pipeline the Sources landing page uses (``docs/features-search.md``
   → "Enabling search"), best-effort and **idempotent**: a source already mapped
   (a content-addressed entry in the SCG sources) or already mapping (a live
   ``queued``/``running`` map job) is skipped, and a demo-fixture / unconfigured
   source is never mapped (only a live MCP connector is).

Auto-map is gated on ``scg.enabled`` and a wired runtime — a disabled deployment
or a config-only install just refreshes the virtual config and returns. Every map
start is wrapped so one failing source never blocks the workspace save (the save
already succeeded by the time we run); failures are logged, not raised.

The fan-out (step 2 — the ``_mappable``/``_drifted``/``_reenrich`` resolution +
the ``_start_map`` loop) runs on a **background daemon thread** so the
``POST``/``PATCH`` route returns the 201/200 response immediately.  Step 1
(config refresh + NL-fingerprint stamp) stays synchronous because it is cheap
store I/O and the response body should reflect the refreshed state.

Why an atomic class: the route handlers stay thin (one call), and the
"which sources are newly mappable" decision lives in ONE place reused by both
create and update — no duplicated, drifting logic across two routes.
"""

from __future__ import annotations

import hashlib
import threading
from typing import Any

from mewbo_core.common import get_logger

from .mcp_config import WorkspaceMcpConfig
from .scg.config import ScgConfig
from .scg.map_job import SourceNlContext
from .store import AgenticSearchStoreBase

logging = get_logger(name="api.agentic_search.source_sync")

# Map-job statuses that mean "a map for this source is already in flight" — don't
# start a duplicate. The terminal buckets (completed/failed) do NOT block a
# re-map: a previously-failed source (e.g. it was unreachable) must be re-mappable
# once its connector URL is fixed, per the deploy-reachability requirement.
_IN_FLIGHT: frozenset[str] = frozenset({"queued", "running"})

# The digest length kept for the NL-context fingerprint — 16 hex chars (64 bits),
# matching ``ManifestHash`` so the two map-lifecycle digests read alike.
_NL_DIGEST_CHARS = 16


class NlContextFingerprint:
    """Deterministic digest of a workspace's NL-context prose (#83).

    The NL-context sibling of :class:`~mewbo_graph.scg.manifest.ManifestHash`:
    where ``ManifestHash`` fingerprints a connector's tool-list *schema* to gate a
    structural re-map, this fingerprints the workspace ``instructions`` + ``desc``
    that seed the map-time enrich step (#81-B) to gate a re-*enrich*. It lives here
    (not on ``ManifestHash``) because it digests untrusted operator prose, a
    different domain from a tool-list schema — extending ``ManifestHash`` would
    couple the two unrelated drift signals.

    Stateless + pure. ``instructions`` and ``desc`` are folded with their roles so
    moving text between the two fields is a change (they reach the enrich block
    under distinct labels). Whitespace-only differences are normalised away (a
    trailing newline is not an enrich-worthy edit). An all-blank prose hashes to
    the empty sentinel so a prose-less workspace compares equal across saves.
    """

    _EMPTY = ""

    @classmethod
    def of(cls, *, instructions: str, desc: str) -> str:
        """Fingerprint the (instructions, desc) prose pair, or ``""`` if blank."""
        norm_instructions = " ".join((instructions or "").split())
        norm_desc = " ".join((desc or "").split())
        if not norm_instructions and not norm_desc:
            return cls._EMPTY
        blob = f"instructions:{norm_instructions}\x00desc:{norm_desc}"
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:_NL_DIGEST_CHARS]


class WorkspaceSourceSync:
    """Refresh the virtual MCP config + best-effort auto-map newly-enabled sources."""

    # The most recent fan-out thread (#97). Route-level tests can't reach the
    # Thread returned by ``on_workspace_saved`` (the routes ignore it), so they
    # synchronize through :meth:`join_last_fan_out` instead of sleeping.
    _last_fan_out: threading.Thread | None = None

    @classmethod
    def join_last_fan_out(cls, timeout: float = 5.0) -> None:
        """Test seam: block until the most recent fan-out finishes (no-op when none)."""
        t = cls._last_fan_out
        if t is not None:
            t.join(timeout=timeout)

    @classmethod
    def on_workspace_saved(
        cls,
        *,
        store: AgenticSearchStoreBase,
        workspace_id: str,
        new_sources: list[str],
        prev_sources: list[str] | None = None,
        runtime: Any = None,
        project: str | None = None,
    ) -> threading.Thread | None:
        """Refresh the virtual config, then auto-map newly-enabled + drifted sources.

        *prev_sources* is the selection BEFORE this save (``None`` on create);
        sources in *new_sources* that weren't already enabled — and that aren't
        already mapped / in-flight — are mapped. Additionally, already-mapped
        enabled sources whose **live tool list drifted** from the mapped
        :class:`ManifestHash` are re-mapped (idempotent — #81-C), AND — when the
        workspace's NL-context prose (``instructions`` + ``desc``) changed since the
        last enrich — already-mapped enabled sources are re-driven to re-seed the
        map-time enrich step (#83). Always refreshes the virtual MCP config first
        (stamping the new NL fingerprint) so it tracks the new selection even when
        auto-map is disabled or a source can't be mapped.

        Step 1 (virtual-config refresh + NL-fingerprint stamp) runs synchronously
        because it is cheap store I/O and the response body should reflect the
        refreshed state.  Step 2 (the ``_mappable``/``_drifted``/``_reenrich``
        resolution + ``_start_map`` loop — involves live MCP introspection) runs on
        a background daemon thread so the route returns immediately.  The thread is
        returned (not ``None``) when the fan-out was actually launched, ``None``
        when the SCG gate is off or no runtime is wired (i.e. fan-out never runs).
        Callers that need to synchronise (tests) can ``thread.join(timeout=…)``; the
        route ignores the return value.
        """
        # Compute the new NL-context fingerprint and read the prior one BEFORE the
        # save overwrites it — the change is what gates the re-enrich (#83). Both
        # reads are best-effort: a store hiccup degrades to "no prose change", so a
        # save is never blocked by the fingerprint plumbing.
        new_fingerprint = cls._nl_fingerprint_for(store, workspace_id)
        try:
            prev_fingerprint = WorkspaceMcpConfig.nl_fingerprint_of(store, workspace_id)
        except Exception as exc:  # noqa: BLE001 — best-effort; treat as unchanged
            logging.warning(
                "workspace %s NL-fingerprint read failed: %s", workspace_id, exc
            )
            prev_fingerprint = new_fingerprint
        nl_changed = new_fingerprint != prev_fingerprint

        # 1. Always refresh the persisted virtual config (cheap, no LLM), stamping
        #    the new NL fingerprint so the next save compares against it.
        try:
            WorkspaceMcpConfig.save(
                store,
                workspace_id,
                new_sources,
                project=project,
                nl_fingerprint=new_fingerprint,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort; never block the save
            logging.warning(
                "workspace %s virtual MCP config refresh failed: %s",
                workspace_id,
                exc,
            )

        # 2. Auto-map newly-enabled + drifted + re-enrich live sources (gated).
        #    Resolve the inputs NOW (on the request thread — cheap store reads /
        #    set operations) so the thread captures concrete values, not live
        #    request-context references that may be torn down.
        if not ScgConfig.enabled() or runtime is None:
            return None
        prev = set(prev_sources or [])
        newly = [s for s in new_sources if s not in prev]
        already = [s for s in new_sources if s in prev]

        # Start the fan-out on a daemon thread so the route returns immediately.
        # All per-source MCP introspection (SourceDescriptorBuilder.build + the
        # live ManifestHash comparisons in _drifted) happen inside the thread.
        t = threading.Thread(
            target=cls._fan_out,
            kwargs=dict(
                store=store,
                workspace_id=workspace_id,
                newly=newly,
                already=already,
                nl_changed=nl_changed,
                runtime=runtime,
                project=project,
            ),
            name=f"workspace-automap-{workspace_id}",
            daemon=True,
        )
        cls._last_fan_out = t
        t.start()
        return t

    @classmethod
    def _fan_out(
        cls,
        *,
        store: AgenticSearchStoreBase,
        workspace_id: str,
        newly: list[str],
        already: list[str],
        nl_changed: bool,
        runtime: Any,
        project: str | None,
    ) -> None:
        """Background fan-out: resolve mappable/drifted/reenrich sources and start jobs.

        Runs on a daemon thread started by :meth:`on_workspace_saved`.  The entire
        body is best-effort: any unhandled exception is logged and swallowed — the
        workspace save already succeeded before this thread was started.  Per-source
        error isolation is preserved: ``_start_map`` has its own try/except, so one
        broken source never affects the others.

        Thread-safety notes:
        - ``store`` is the shared ``AgenticSearchStoreBase`` instance.
          ``JsonAgenticSearchStore`` uses a ``threading.Lock`` for all writes
          (``create_map_job``, ``update_map_job``) and atomic JSON rewrites, so
          concurrent calls from this thread and the request thread are safe.
          ``MongoAgenticSearchStore`` uses MongoDB's document-level atomics; safe.
        - ``SourceDescriptorBuilder.build()`` opens a fresh MCP connection per call
          (stateless, no shared mutable state) — safe off-thread.
        - ``MapSourceJob.start()`` is already called from worker contexts
          (``SessionRuntime`` background threads); safe off-thread.
        - ``get_scg_store()`` returns a module-level singleton; its list/read methods
          are read-only and safe to call from any thread.
        """
        try:
            # Already-mapped enabled sources whose live tool surface drifted need a
            # re-map even though they aren't newly enabled (a tool was added/removed
            # or an arg changed since the last map).
            drifted = cls._drifted(store, already, project=project)
            # An instructions/desc edit changed no sources and perturbed no tool list,
            # so neither _mappable nor _drifted fires — yet the enrich notes are now
            # stale. Re-drive the map (idempotent, in-flight-guarded) for the
            # workspace's enabled, already-mapped sources so the map-time enrich
            # re-seeds against the new prose (#83). Only on a real fingerprint change.
            reenrich = cls._reenrich_targets(store, already) if nl_changed else []
            to_map = list(
                dict.fromkeys(
                    cls._mappable(store, newly, project=project) + drifted + reenrich
                )
            )
            if not to_map:
                return
            # The workspace prose that triggered this map seeds the enrich step — it
            # is UNTRUSTED and rides the user turn only (#81-B). Read it once and
            # pass it to every mapped source (anchored to that source's caps).
            # Best-effort like every other step here: enrich plumbing must never
            # fail the workspace save that carried the prose.
            try:
                nl_context = cls._nl_context_for(store, workspace_id)
            except Exception as exc:  # noqa: BLE001 — degrade to descriptor-only map
                logging.warning(
                    "workspace %s NL-context read failed (mapping without enrich prose): %s",
                    workspace_id,
                    exc,
                )
                nl_context = None
            for source_id in to_map:
                cls._start_map(
                    store, source_id, runtime=runtime, project=project, nl_context=nl_context
                )
        except Exception as exc:  # noqa: BLE001 — best-effort; never crash the daemon
            logging.warning(
                "workspace %s background automap fan-out failed: %s", workspace_id, exc
            )

    @staticmethod
    def _nl_fingerprint_for(
        store: AgenticSearchStoreBase, workspace_id: str
    ) -> str:
        """The :class:`NlContextFingerprint` of the workspace's current prose.

        ``""`` when the workspace is gone or carries no prose — equal to a
        prose-less prior config, so a no-op save never spuriously re-enriches.
        """
        ws = store.get_workspace(workspace_id)
        if ws is None:
            return ""
        return NlContextFingerprint.of(
            instructions=ws.instructions or "", desc=ws.desc or ""
        )

    @staticmethod
    def _nl_context_for(
        store: AgenticSearchStoreBase, workspace_id: str
    ) -> SourceNlContext | None:
        """Build the untrusted NL-context block from the workspace's own prose.

        Reads the workspace's ``instructions`` + ``desc`` so the map-time enrich
        step can distil them into anchored memory notes. ``None`` when the
        workspace is gone or carries no prose (so the map contract stays
        byte-identical to the pre-enrich, descriptor-only path).
        """
        ws = store.get_workspace(workspace_id)
        if ws is None:
            return None
        ctx = SourceNlContext(
            workspace_instructions=ws.instructions or "",
            workspace_description=ws.desc or "",
        )
        return None if ctx.is_empty else ctx

    # -- which of the newly-enabled sources are worth mapping ---------------

    @classmethod
    def _mappable(
        cls,
        store: AgenticSearchStoreBase,
        source_ids: list[str],
        *,
        project: str | None,
    ) -> list[str]:
        """Filter to source ids not already mapped AND not already in-flight.

        A source already present in the GLOBAL SCG sources is skipped (the
        content-addressed mapping is shared across workspaces — re-mapping on
        every save would be wasteful churn); a source with a live ``queued`` /
        ``running`` map job is skipped (don't stack duplicate jobs). A terminal
        (completed/failed) job does NOT block — a failed/unreachable source must
        be re-mappable once fixed.
        """
        mapped = cls._mapped_source_ids()
        out: list[str] = []
        for sid in source_ids:
            if sid in mapped:
                continue
            jobs = store.list_map_jobs(source_id=sid)
            if any(j.status in _IN_FLIGHT for j in jobs):
                continue
            out.append(sid)
        return out

    # -- which already-mapped enabled sources need a prose re-enrich (#83) ----

    @classmethod
    def _reenrich_targets(
        cls, store: AgenticSearchStoreBase, source_ids: list[str]
    ) -> list[str]:
        """Of *source_ids*, the already-mapped, not-in-flight ones to re-enrich.

        Called only when the workspace NL-context fingerprint changed. A source
        must already be present in the GLOBAL SCG (an unmapped source is handled
        by ``_mappable`` on first enable, not here) and must not have a live
        ``queued`` / ``running`` map job (a re-map is already coming — it will pick
        up the fresh prose). Re-driving the map for these re-seeds the map-time
        enrich notes against the new instructions/desc without touching the
        connector's structural graph (a content-addressed re-map is idempotent).
        """
        mapped = cls._mapped_source_ids()
        if not mapped:
            return []
        out: list[str] = []
        for sid in source_ids:
            if sid not in mapped:
                continue
            jobs = store.list_map_jobs(source_id=sid)
            if any(j.status in _IN_FLIGHT for j in jobs):
                continue
            out.append(sid)
        return out

    @staticmethod
    def _mapped_source_ids() -> set[str]:
        """Source ids already present in the GLOBAL SCG (empty if SCG absent)."""
        try:
            from mewbo_graph.scg.store import get_scg_store
        except ImportError:
            return set()
        try:
            return {s.source_id for s in get_scg_store().list_sources()}
        except Exception as exc:  # noqa: BLE001 — read is best-effort
            logging.warning("SCG mapped-source read failed: %s", exc)
            return set()

    # -- which already-mapped enabled sources drifted (#81-C) ----------------

    @classmethod
    def _drifted(
        cls,
        store: AgenticSearchStoreBase,
        source_ids: list[str],
        *,
        project: str | None,
    ) -> list[str]:
        """Of *source_ids*, those whose live tool list differs from the mapped hash.

        For each already-mapped enabled source, hash the connector's LIVE tool
        list (:class:`ManifestHash`) and compare it to the
        :attr:`SourceDescriptor.schema_version` stamped at map time. A mismatch is
        drift → re-map. Skips a source with a live ``queued`` / ``running`` map job
        (a re-map is already coming) and any source whose live list can't be
        fetched (unreachable / unconfigured — left as-is, never block the save).
        Entirely best-effort: any error reads as "no drift" so a workspace save is
        never blocked by a flaky introspection.
        """
        stored = cls._stored_manifest_hashes()
        if not stored:
            return []
        out: list[str] = []
        for sid in source_ids:
            mapped_hash = stored.get(sid)
            if mapped_hash is None:  # not actually mapped — handled by _mappable
                continue
            jobs = store.list_map_jobs(source_id=sid)
            if any(j.status in _IN_FLIGHT for j in jobs):
                continue  # a re-map is already coming; don't stack a duplicate
            live_hash = cls._live_manifest_hash(sid, project=project)
            if live_hash is not None and live_hash != mapped_hash:
                out.append(sid)
        return out

    @staticmethod
    def _stored_manifest_hashes() -> dict[str, str]:
        """``source_id -> stamped manifest hash`` for every mapped source.

        Empty when the SCG library is absent or unreadable (best-effort). A
        source whose ``schema_version`` is unset (mapped before #81-C, or a
        non-tool-list source) is omitted — it simply never reports drift until its
        next clean re-map stamps a hash.
        """
        try:
            from mewbo_graph.scg.store import get_scg_store
        except ImportError:
            return {}
        try:
            return {
                s.source_id: s.schema_version
                for s in get_scg_store().list_sources()
                if s.schema_version
            }
        except Exception as exc:  # noqa: BLE001 — read is best-effort
            logging.warning("SCG manifest-hash read failed: %s", exc)
            return {}

    @staticmethod
    def _live_manifest_hash(source_id: str, *, project: str | None) -> str | None:
        """Hash the connector's LIVE tool list, or None if it can't be fetched.

        Reuses the same :class:`SourceDescriptorBuilder` the map path uses, so the
        live shape that feeds the hash is identical to the shape a re-map would
        persist — the comparison can never be a false-positive from two different
        introspection routes. None on any fetch failure (unreachable / unconfigured
        / deps absent): an undetectable live surface is treated as "no drift".
        """
        from mewbo_graph.scg.manifest import ManifestHash

        from .scg.descriptors import SourceDescriptorBuilder

        try:
            built = SourceDescriptorBuilder(source_id, project=project).build()
        except (LookupError, RuntimeError):
            return None
        except Exception as exc:  # noqa: BLE001 — never block a save on one source
            logging.warning("drift-check descriptor build failed for %s: %s", source_id, exc)
            return None
        return ManifestHash.of_descriptor_raw(built.raw)

    # -- start one map job (mirrors POST /sources/<id>/map) -----------------

    @staticmethod
    def _start_map(
        store: AgenticSearchStoreBase,
        source_id: str,
        *,
        runtime: Any,
        project: str | None,
        nl_context: SourceNlContext | None = None,
    ) -> None:
        """Build a live descriptor + start a ``MapSourceJob`` for *source_id*.

        Mirrors the ``POST /sources/<id>/map`` auto-build path: a configured MCP
        server's live tool list → a schema-only descriptor → the map drive. A
        source with no configured MCP connector (a demo fixture or an
        unconfigured id) raises :class:`LookupError` from the builder and is
        skipped — auto-map only touches real connectors. Every failure is logged,
        never raised: the workspace save already succeeded. ``nl_context`` (the
        workspace's untrusted prose) seeds the map-time enrich step (#81-B).
        """
        from .scg.descriptors import SourceDescriptorBuilder
        from .scg.map_job import MapSourceJob, SourceMapInput

        try:
            built = SourceDescriptorBuilder(source_id, project=project).build()
        except LookupError:
            # No configured MCP connector — a demo fixture / unconfigured id.
            return
        except RuntimeError as exc:
            logging.warning("auto-map descriptor build failed for %s: %s", source_id, exc)
            return
        except Exception as exc:  # noqa: BLE001 — never block on one source
            logging.warning("auto-map skipped for %s: %s", source_id, exc)
            return

        try:
            source = SourceMapInput(
                source_id=source_id,
                source_type=SourceDescriptorBuilder.SOURCE_TYPE,
                descriptor=built.raw,
                nl_context=nl_context,
            )
            MapSourceJob.start(source, store=store, runtime=runtime)
            logging.info("auto-map started for newly-enabled source %s", source_id)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logging.warning("auto-map start failed for %s: %s", source_id, exc)


__all__ = ["NlContextFingerprint", "WorkspaceSourceSync"]
