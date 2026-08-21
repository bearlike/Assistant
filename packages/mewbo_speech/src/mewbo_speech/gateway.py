#!/usr/bin/env python3
"""The speech gateway client — one atomic class, collaborators injected.

State (where the gateway is, which key opens it, how long to wait) and behaviour
(list, synthesise, transcribe) live together; the transport is a FIELD, so a test
scripts it and the request-building and response-parsing code under test is the
real one.

Dispatch is data, not a branch: :meth:`run` reads ``request.SDK_OPERATION`` and
calls ``request.parse_response``, both owned by the request variant. Adding a
third speech mode touches :mod:`mewbo_speech.operations` and nothing here.

This is a plain class rather than a Pydantic model deliberately: it holds an
injected collaborator and a mutable catalogue cache, crosses no trust boundary,
and validating every attribute write would buy nothing. The things that DO cross
a boundary — the requests and results it moves — are the Pydantic models.
"""

from __future__ import annotations

import asyncio

from mewbo_speech.models import SpeechMode, SpeechModel
from mewbo_speech.operations import SpeechRequest, SpeechResult
from mewbo_speech.transport import (
    LiteLlmSpeechTransport,
    SpeechGatewayError,
    SpeechTransport,
)

#: Generous enough for the slowest measured synthesis with headroom. A ~410
#: character paragraph took 7.7s on ``supertonic-3-hd``, and a call destined to
#: fail on an upstream credential burned 17s inside the SDK's own retry loop
#: before surfacing — so a tight timeout turns a slow success into a failure.
DEFAULT_SPEECH_TIMEOUT: float = 90.0

#: The prefix litellm's LOCAL provider dispatch needs, and strips before writing
#: the request body. Not read from ``llm.proxy_model_prefix``: that knob steers
#: chat completions, where the prefix also reaches the wire. Here it must not.
DEFAULT_ROUTE_PREFIX: str = "openai"

#: How many :meth:`SpeechGateway.run` calls may be in flight at once, PER
#: GATEWAY INSTANCE. **This bound is REASONED, not measured** — repeated
#: interleaved trials (n=12 requests/bound, 4 rounds each) comparing
#: client-side bounds of 2 and 4 against the deployed TTS backend produced
#: overlapping medians (22.4s vs 22.8s) with per-trial variance (18-30s) far
#: larger than the gap between bounds, on a box shared with other work. A
#: `bound=1` vs `bound=2` control DID separate cleanly (median 10.9s vs 8.9s,
#: n=3), so concurrency above 1 measurably helps — the deployed backend's
#: advertised ``max_parallel_requests: 2`` per TTS route (`CLAUDE.md` →
#: "Cancellation") is the only number anyone has actually confirmed against
#: the operator's own config, and 4 leaves headroom for the two-route split
#: (`supertonic-3` / `supertonic-3-hd`) without the client ever being the
#: thing that queues first. Re-measure on a quiet box before trusting a
#: change to this number either way.
DEFAULT_MAX_CONCURRENT_CALLS: int = 4

#: litellm's own fallback is ``litellm.num_retries or openai.DEFAULT_MAX_RETRIES``
#: (2, i.e. 3 attempts) when nothing is passed — chosen for a chat completion,
#: where a single attempt is cheap. It is wrong for either speech leg: measured
#: against a call guaranteed to fail (a rejected ``response_format``), each
#: retry costs roughly the FULL request latency (0.30s at 0 retries, 1.00s at
#: 1, 2.02s at 2 — no fast-fail path). Transcription is cheap even doubled
#: (~0.2-0.8s per attempt measured in `CLAUDE.md`), so it keeps litellm's
#: default. Synthesis is a different trade: a failing call burns 4-8s PER
#: RETRY at cold-start (`CLAUDE.md` → "Synthesis is not an interactive-latency
#: call"), and a synthesis that is eventually abandoned parks one of the
#: backend's two shared slots for ~14s regardless of how it fails
#: (`CLAUDE.md` → "Cancellation") — so a caller who gives up mid-retry has
#: already paid for slot occupancy the retries cannot recover from. One retry
#: (not zero) is kept because the deployed proxy has shown transient failures
#: unrelated to the request (`CLAUDE.md` → "gateway TTS leg 500s"), and zero
#: would turn every one of those into a user-visible failure with nothing to
#: gain from asking again.
DEFAULT_SYNTHESIS_MAX_RETRIES: int = 1
DEFAULT_TRANSCRIPTION_MAX_RETRIES: int = 2


class SpeechGateway:
    """Speech-capable models on the LiteLLM gateway: list, synthesise, transcribe."""

    def __init__(
        self,
        *,
        api_base: str,
        api_key: str,
        transport: SpeechTransport | None = None,
        timeout: float = DEFAULT_SPEECH_TIMEOUT,
        route_prefix: str = DEFAULT_ROUTE_PREFIX,
        max_concurrent_calls: int = DEFAULT_MAX_CONCURRENT_CALLS,
        synthesis_max_retries: int = DEFAULT_SYNTHESIS_MAX_RETRIES,
        transcription_max_retries: int = DEFAULT_TRANSCRIPTION_MAX_RETRIES,
        concurrency: asyncio.Semaphore | None = None,
    ) -> None:
        """Bind gateway coordinates, retry policy and the transport that reaches them.

        *concurrency* is the injectable collaborator (the atomic-class rule:
        state as fields, collaborators by DI) — pass one to SHARE a bound
        across gateway instances, or leave it unset to build a fresh
        :class:`asyncio.Semaphore` sized from *max_concurrent_calls*.
        Constructing a ``Semaphore`` needs no running event loop (verified:
        Python's implementation only touches the loop inside ``acquire()``),
        so this is safe to call from synchronous setup code. *max_concurrent_calls*
        is ignored when *concurrency* is given explicitly — the semaphore's own
        bound is then the truth.
        """
        self.api_base = api_base.strip()
        self.api_key = api_key.strip()
        self.timeout = timeout
        self.route_prefix = route_prefix
        self.synthesis_max_retries = synthesis_max_retries
        self.transcription_max_retries = transcription_max_retries
        self.transport: SpeechTransport = transport or LiteLlmSpeechTransport()
        self.concurrency = concurrency or asyncio.Semaphore(max_concurrent_calls)
        self._catalogue: list[SpeechModel] | None = None

    @classmethod
    def from_config(
        cls,
        *,
        transport: SpeechTransport | None = None,
        concurrency: asyncio.Semaphore | None = None,
    ) -> SpeechGateway:
        """Build a gateway from app config, tolerating an absent ``speech`` block.

        Reads ``speech.api_base`` / ``speech.api_key`` when a ``speech`` section
        exists and falls back to ``llm.*`` otherwise, because the speech models
        are routes on the SAME LiteLLM proxy the chat models come from. The
        fallback is what lets this package work before — or without — a config
        section being added: ``get_config_value`` walks a missing field to its
        default rather than raising, so an absent section is indistinguishable
        from an empty one, which is exactly the behaviour wanted here.

        No config field feeds ``max_concurrent_calls`` or either retry count —
        this classmethod signature matches ``Callable[[], SpeechGateway]``, the
        shape ``SpeechRoutesController.gateway_reader`` expects, so it takes no
        argument beyond the two DI seams. **A caller building a FRESH gateway
        per call (as the API controller deliberately does, so a re-pointed
        ``speech.api_base`` takes effect without a restart) must pass the SAME
        *concurrency* semaphore on every call, or the bound is inert** — a new
        ``Semaphore`` per call never accumulates waiters across calls, so
        concurrency four callers deep would still all pass through immediately.
        The API guards its own total in-flight speech calls separately via
        ``SpeechRoutesController.MAX_CONCURRENT_CALLS``/``StreamCapacity``; a
        caller that instead holds ONE long-lived gateway instance (a CLI
        session, an Aura bridge process) gets the bound for free from the
        default per-instance semaphore and needs no *concurrency* argument.

        Cost class: ``O(1)`` — reads the process-cached config, no I/O.
        """
        from mewbo_core.config import get_config_value

        api_base = str(
            get_config_value("speech", "api_base", default="")
            or get_config_value("llm", "api_base", default="")
            or ""
        )
        api_key = str(
            get_config_value("speech", "api_key", default="")
            or get_config_value("llm", "api_key", default="")
            or ""
        )
        timeout = float(get_config_value("speech", "timeout", default=DEFAULT_SPEECH_TIMEOUT))
        return cls(
            api_base=api_base,
            api_key=api_key,
            transport=transport,
            timeout=timeout,
            concurrency=concurrency,
        )

    def is_available(self) -> bool:
        """Whether this gateway is both configured and installed.

        The check a host runs before advertising a speech capability: false means
        the feature is absent, which is a state to render, not an error to raise.

        Cost class: ``O(1)`` — no network; the dependency probe hits
        ``sys.modules`` after the first call.
        """
        if not self.api_base or not self.api_key:
            return False
        probe = getattr(self.transport, "is_available", None)
        return bool(probe()) if callable(probe) else True

    def list_models(self, *, refresh: bool = False) -> list[SpeechModel]:
        """Return every speech-capable model the gateway advertises.

        Classified by ``model_info.mode``: chat and embedding routes in the same
        document are dropped by :meth:`SpeechModel.from_model_info`, so this list
        is speech-only by construction rather than by a name heuristic.

        Cached for the life of this gateway, mirroring how core hydrates proxy
        capabilities once per process per ``api_base`` — the document is ~120 KB
        and changes only when an operator edits the proxy's routes. Pass
        ``refresh=True`` after such an edit.

        **This currently 403s on the deployed proxy and the reason is not
        transient.** The runtime virtual key is scoped to ``llm_api_routes``,
        and ``/model/info`` is a management route outside that set::

            {"detail": "Virtual key is not allowed to call this route.
             Only allowed to call routes: ['llm_api_routes'].
             Tried to call route: /v1/model/info"}

        Measured identically from the host and from inside the api container, so
        it is the key's route allowlist and not a network path. ``/v1/models``
        still answers 200 for the same key, but returns bare ids with no ``mode``
        — which is precisely the field that separates a TTS route from an STT
        route, so it cannot substitute.

        The fix is operational (grant the key the route, or hand discovery a
        separate admin key). **Do not paper over it with a name heuristic** —
        classifying ``supertonic-3`` as TTS because of what it is called is the
        guess this whole module exists to avoid. Until it is granted, an operator
        naming the models in config is the honest fallback.

        Note the same 403 silently degrades core's
        ``register_proxy_model_capabilities``, which swallows the failure.

        Cost class: ``O(collection)`` in the gateway's advertised models on a
        cache miss (one HTTP round trip); ``O(1)`` on a hit.
        """
        if self._catalogue is not None and not refresh:
            return list(self._catalogue)
        if not self.api_base or not self.api_key:
            raise SpeechGatewayError(
                "speech gateway is not configured — set speech.api_base/api_key "
                "or llm.api_base/api_key."
            )
        entries = self.transport.fetch_model_info(
            api_base=self.api_base, api_key=self.api_key, timeout=self.timeout
        )
        catalogue = [
            model for model in (SpeechModel.from_model_info(entry) for entry in entries) if model
        ]
        catalogue.sort(key=lambda model: (model.mode.value, model.id))
        self._catalogue = catalogue
        return list(catalogue)

    def models_for(self, mode: SpeechMode, *, refresh: bool = False) -> list[SpeechModel]:
        """Return the advertised models serving one capability.

        ``mode`` is required rather than defaulted: a default of "every speech
        model" would hand a voice picker the transcription routes, which fail
        opaquely when synthesised against.

        Cost class: same as :meth:`list_models` — ``O(collection)`` on a cache
        miss, ``O(1)`` on a hit.
        """
        return [model for model in self.list_models(refresh=refresh) if model.mode is mode]

    async def run(self, request: SpeechRequest) -> SpeechResult:
        """Perform one speech operation and return its parsed result.

        The single call path for every variant. The operation name and the result
        shape both come off the request, so this method has no knowledge of which
        variant it is running and no branch to keep in step with the union.

        The result variant always matches the request variant — a
        :class:`SynthesisRequest` yields a :class:`SynthesisResult` — because the
        request's own ``parse_response`` builds it. That is stated rather than
        expressed as an overload pair: no package in this workspace ships a
        ``py.typed`` marker, so under the repo's mypy config a sibling module's
        types resolve to ``Any`` and an overload set collapses to "the first
        signature matches everything". A caller narrows with ``isinstance``,
        which a discriminated union wants anyway.

        Cost class: ``O(input length)``, and the FIRST call is the expensive one.
        Warm, a one-sentence synthesis on ``supertonic-3`` takes ~0.8-1.0s and a
        410-char paragraph ~4s; ``supertonic-3-hd`` roughly doubles both for
        byte-identical output. **Cold — the first call in a fresh process — the
        same one-sentence synthesis has measured between ~4s and ~8s.** Quote the
        cold number to anyone building a spinner: the warm figure is true of
        every call except the one the user notices.

        The backend buffers the whole file before sending a byte (``stream=true``
        changed time-to-first-byte not at all), so there is nothing to render
        progressively; budget for the full duration.

        **Cancellation: cancel the awaiting task; there is no cancel token.**
        ``asyncio.CancelledError`` derives from ``BaseException``, so it passes
        straight through the transport's ``except Exception`` normalisation
        instead of being converted into a ``SpeechGatewayError``. Measured: a
        ~4s synthesis cancelled at 0.30s raised ``CancelledError`` at 0.31s. A
        cancel token here would only re-wrap a mechanism the language already
        provides.

        **But cancelling does NOT free the gateway's backend slot, and that is
        the expensive part.** The cancellation is not propagated upstream: the
        abandoned synthesis keeps running and holds one of the TTS backend's two
        ``max_parallel_requests``. Measured against a ~0.9s baseline, the next
        call after a cancel took 11.9-14.9s, reproducibly, and a FRESH gateway
        with a fresh client was equally slow — so the occupancy is server-side,
        not a poisoned local connection pool. Waiting 6s only halved it.

        The consequence for any read-aloud UI: **short requests make
        cancellation cheap and long ones make it expensive**, because an
        abandoned request costs roughly what it had left to do. Synthesising
        sentence-sized chunks is therefore not only a payload-cap workaround, it
        is what keeps a barge-in from parking a shared backend slot — of which
        there are two, for every caller of the gateway.

        **Bounded by :attr:`concurrency`** (default
        :data:`DEFAULT_MAX_CONCURRENT_CALLS`) — a caller past the bound waits on
        the semaphore rather than adding pressure to an already-saturated
        backend. ``async with`` is what makes this safe under cancellation: a
        ``CancelledError`` raised while WAITING never acquired a permit
        (:class:`asyncio.Semaphore` un-does its own bookkeeping on that path),
        and one raised while HOLDING the permit still runs ``__aexit__`` and
        releases it — so a cancelled synthesis cannot leak a slot on the CLIENT
        side. The abandoned call on the BACKEND side still costs what "Measured
        against a ~0.9s baseline..." above describes; this bound only concerns
        the local semaphore, and the two limits are independent.

        Retries use :attr:`synthesis_max_retries` or
        :attr:`transcription_max_retries`, selected by ``request.REQUIRED_MODE``
        — see :data:`DEFAULT_SYNTHESIS_MAX_RETRIES` for why the two directions
        differ.
        """
        max_retries = (
            self.synthesis_max_retries
            if request.REQUIRED_MODE is SpeechMode.SYNTHESIS
            else self.transcription_max_retries
        )
        async with self.concurrency:
            payload = await self.transport.invoke(
                request.SDK_OPERATION,
                api_base=self.api_base,
                api_key=self.api_key,
                timeout=self.timeout,
                max_retries=max_retries,
                **request.litellm_kwargs(self.route_prefix),
            )
        return request.parse_response(payload)
