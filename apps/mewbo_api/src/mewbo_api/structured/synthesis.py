"""No-loop structured synthesis for ``POST /v1/structured`` (``mode: "synthesis"``).

The degenerate, single-round-trip execution strategy on ``/v1/structured``: the
core :class:`~mewbo_core.structured_synthesis.StructuredSynthesizer` drives ONE
grounded LLM call (+ one optional reask) — no ``ToolUseLoop``, no probes — so a
caller that wants cheap, fast, retrieval-only structured output gets ~1–3s
latency on the SAME endpoint as the agentic lane. This folds in the former
``POST /v1/structured/fast`` sibling (#85): one synthesis engine, surfaced as a
*mode* on ``/v1/structured`` instead of a parallel endpoint with its own request
model, stamp seam, and session-backing path.

Session-backing reuses the write-behind :class:`RealtimeSessionRecorder` (shared
with ``/v1/draft/stream``): the synthesis runs inside a pre-minted Langfuse trace
and the single-turn transcript is persisted AFTER the response on a daemon thread
— the latency path never pays a store write. The run is tagged ``structured:fast``
so its Langfuse ``session_type`` facet stays ``structured_fast`` (provenance
parity with the removed sibling), and the persisted ``structured_output`` event
makes ``GET /v1/structured/<run_id>`` resolve it like any agentic run.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from mewbo_core.common import get_logger
from mewbo_core.structured_synthesis import StructuredSynthesizer

from mewbo_api.realtime.recorder import RealtimeSessionRecorder

logging = get_logger(name="api.structured.synthesis")


@dataclass
class SynthesisRunner:
    """Run one no-loop structured synthesis, session-backed write-behind.

    Construct per request with the session runtime (the session-store seam);
    :meth:`run` performs the single grounded round-trip and returns the wire body
    for the ``/v1/structured`` POST response — status ``completed`` with the
    validated output, grounding citations, and a ``<session_id>:r1`` run handle
    that ``GET /v1/structured/<run_id>`` can re-resolve.

    Args:
        runtime: The session runtime; ``None`` degrades to trace-only (no
            transcript persisted), mirroring the realtime routes.
    """

    runtime: Any

    def run(
        self,
        *,
        query: str,
        schema: dict[str, Any],
        workspace: str | None,
        model: str | None,
        surface: str,
    ) -> dict[str, Any]:
        """Synthesize once → ``{run_id, status, output, citations, workspace}``.

        Raises :class:`~mewbo_core.structured_response.StructuredResponseError`
        when the answer fails schema validation after the bounded reask (the route
        maps it to a 422 envelope); any other exception bubbles to the route's 500.
        """
        synthesizer = StructuredSynthesizer(
            model_name=model,
            grounding_provider=self._grounding_provider(),
        )
        # Session-back the run: mint a session, run the synthesis inside its
        # Langfuse trace, then persist write-behind AFTER the response is built.
        recorder = RealtimeSessionRecorder.for_fast(
            self.runtime,
            query,
            surface=surface,
            workspace=workspace,
            model=model,
        )
        with recorder.trace():
            output, citations = asyncio.run(
                synthesizer.synthesize(query, schema, workspace=workspace)
            )
        if self.runtime is not None:
            # Materialise the session RECORD synchronously (cheap, O(1) — no
            # transcript write) so the returned ``<session_id>:r1`` handle resolves
            # immediately via GET /v1/structured/<run_id>, matching the agentic
            # path's invariant that the session exists before the run handle is
            # returned. Without it, the record only appeared inside the write-behind
            # persist on a daemon thread, so an immediate GET raced to a 404.
            self.runtime.ensure_session(recorder.session_id)
            # The transcript itself stays write-behind: the response is fully built,
            # so the small event appends never block it.
            recorder.persist_async(output=output)
        return {
            "run_id": f"{recorder.session_id}:r1",
            "status": "completed",
            "output": output,
            "citations": [
                {
                    "id": c.id,
                    "kind": c.kind,
                    "snippet": c.snippet,
                    "score": c.score,
                    "source": c.source,
                }
                for c in citations
            ],
            "workspace": workspace,
        }

    @staticmethod
    def _grounding_provider() -> Any | None:
        """The concrete wiki grounding provider, or ``None`` when unavailable.

        Lazily imported — an optional dependency kept out of the import graph so a
        graph-less install still serves un-grounded synthesis (the synthesizer
        simply runs without retrieval context).
        """
        try:
            from mewbo_api.realtime.grounding import WikiGroundingProvider  # noqa: PLC0415
            return WikiGroundingProvider()
        except Exception as exc:  # noqa: BLE001
            logging.debug("WikiGroundingProvider unavailable: {}", exc)
            return None


__all__ = ["SynthesisRunner"]
