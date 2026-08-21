"""Gateway tests — real request building and parsing, stubbed I/O only.

The scripted transport swaps ONE thing: the socket. Every request the gateway
builds, every response it parses, and the dispatch that connects them is the
production code. The last class here goes further and exercises the DEFAULT
transport against a real local listener, because an injected-transport suite
proves nothing about the transport a deployment actually constructs.
"""

import asyncio
import json
import subprocess
import sys
import threading
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
from mewbo_speech import (
    DEFAULT_SYNTHESIS_MAX_RETRIES,
    DEFAULT_TRANSCRIPTION_MAX_RETRIES,
    AudioContainer,
    LiteLlmSpeechTransport,
    SpeechGateway,
    SpeechGatewayError,
    SpeechMode,
    SpeechUnavailableError,
    SynthesisRequest,
    SynthesisResult,
    TranscriptionRequest,
    TranscriptionResult,
    transport as transport_module,
)

WAV_HEAD = b"RIFF\x24\x08\x00\x00WAVEfmt "

# A trimmed copy of the deployed proxy's `/model/info` payload: two TTS routes,
# one STT route, and the chat/embedding routes that share the document.
MODEL_INFO = [
    {"model_name": "claude-opus-5", "model_info": {"mode": "chat", "key": "openai/claude-opus-5"}},
    {
        "model_name": "supertonic-3",
        "model_info": {"mode": "audio_speech", "key": "openai/supertonic-3"},
    },
    {
        "model_name": "text-embedding-3-small",
        "model_info": {"mode": "embedding", "key": "openai/text-embedding-3-small"},
    },
    {
        "model_name": "supertonic-3-hd",
        "model_info": {"mode": "audio_speech", "key": "openai/supertonic-3-hd"},
    },
    {
        "model_name": "nova-3",
        "model_info": {"mode": "audio_transcription", "key": "deepgram/nova-3"},
    },
    {"model_name": "broken-route", "model_info": {}},
]


class ScriptedTransport:
    """A transport double that records calls and replays scripted payloads.

    Each ``invoke`` consumes the next scripted payload, so a second call in one
    test cannot silently replay the first one's response.
    """

    def __init__(
        self,
        *,
        entries: list[Mapping[str, Any]] | None = None,
        payloads: list[Mapping[str, Any]] | None = None,
        fail: Exception | None = None,
    ) -> None:
        self.entries: list[Mapping[str, Any]] = list(entries or MODEL_INFO)
        self.payloads: list[Mapping[str, Any]] = list(payloads or [])
        self.fail = fail
        self.model_info_calls: list[dict[str, Any]] = []
        self.invocations: list[tuple[str, dict[str, Any]]] = []
        self.connections: list[dict[str, Any]] = []

    def fetch_model_info(
        self, *, api_base: str, api_key: str, timeout: float
    ) -> list[Mapping[str, Any]]:
        self.model_info_calls.append(
            {"api_base": api_base, "api_key": api_key, "timeout": timeout}
        )
        if self.fail:
            raise self.fail
        return list(self.entries)

    async def invoke(
        self,
        operation: str,
        /,
        *,
        api_base: str,
        api_key: str,
        timeout: float,
        max_retries: int,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        # Recorded SEPARATELY from kwargs so a test can assert the gateway
        # coordinates were forwarded at all. An earlier version of this double
        # swallowed them into **kwargs and the assertions below spelled out the
        # credential-free dict as if it were correct — which is how a live call
        # failing with `OpenAIException - Missing credentials` shipped behind a
        # green suite. `max_retries` joined this list rather than `kwargs` for
        # the identical reason: it is a required, explicit protocol parameter,
        # not something a caller may bury among SDK arguments.
        self.connections.append(
            {
                "api_base": api_base,
                "api_key": api_key,
                "timeout": timeout,
                "max_retries": max_retries,
            }
        )
        self.invocations.append((operation, kwargs))
        if self.fail:
            raise self.fail
        if not self.payloads:
            raise AssertionError(f"no scripted payload left for {operation!r}")
        return self.payloads.pop(0)


def _gateway(transport, **kwargs):
    return SpeechGateway(
        api_base="http://gateway.invalid/v1", api_key="sk-test", transport=transport, **kwargs
    )


class TestModelListing:
    """Listing classifies by mode and caches the way core caches proxy info."""

    def test_only_speech_routes_survive_the_listing(self):
        transport = ScriptedTransport()
        models = _gateway(transport).list_models()
        assert [model.id for model in models] == [
            "supertonic-3",
            "supertonic-3-hd",
            "nova-3",
        ]

    def test_modes_split_tts_from_stt(self):
        gateway = _gateway(ScriptedTransport())
        tts = gateway.models_for(SpeechMode.SYNTHESIS)
        stt = gateway.models_for(SpeechMode.TRANSCRIPTION)
        assert [model.id for model in tts] == ["supertonic-3", "supertonic-3-hd"]
        assert [model.id for model in stt] == ["nova-3"]
        # No id is carried in its operator-facing prefixed form.
        assert all("/" not in model.id for model in tts + stt)

    def test_the_catalogue_is_fetched_once_and_refreshed_on_demand(self):
        transport = ScriptedTransport()
        gateway = _gateway(transport)
        gateway.list_models()
        gateway.list_models()
        gateway.models_for(SpeechMode.SYNTHESIS)
        assert len(transport.model_info_calls) == 1
        gateway.list_models(refresh=True)
        assert len(transport.model_info_calls) == 2

    def test_a_mutated_listing_cannot_corrupt_the_cache(self):
        gateway = _gateway(ScriptedTransport())
        gateway.list_models().clear()
        assert len(gateway.list_models()) == 3

    def test_gateway_coordinates_reach_the_transport(self):
        transport = ScriptedTransport()
        _gateway(transport, timeout=12.5).list_models()
        call = transport.model_info_calls[0]
        assert call == {
            "api_base": "http://gateway.invalid/v1",
            "api_key": "sk-test",
            "timeout": 12.5,
        }

    def test_an_unconfigured_gateway_refuses_rather_than_returning_empty(self):
        gateway = SpeechGateway(api_base="", api_key="", transport=ScriptedTransport())
        with pytest.raises(SpeechGatewayError, match="not configured"):
            gateway.list_models()
        assert gateway.is_available() is False


class TestRunDispatch:
    """One call path; the variant supplies the operation and the result shape."""

    def test_synthesis_runs_aspeech_and_returns_sniffed_audio(self):
        transport = ScriptedTransport(
            payloads=[{"audio": WAV_HEAD, "content_type": "audio/mpeg"}]
        )
        request = SynthesisRequest(model="supertonic-3", text="Hello.", voice="alloy")
        result = asyncio.run(_gateway(transport).run(request))

        operation, kwargs = transport.invocations[0]
        assert operation == "aspeech"
        assert kwargs == {
            "model": "openai/supertonic-3",
            "input": "Hello.",
            "voice": "alloy",
        }
        assert isinstance(result, SynthesisResult)
        assert result.container is AudioContainer.WAV
        assert result.content_type == "audio/wav"
        assert result.declared_type_was_wrong is True
        assert result.model == "supertonic-3"

    def test_transcription_runs_atranscription_and_returns_text(self):
        transport = ScriptedTransport(payloads=[{"text": "hello there"}])
        request = TranscriptionRequest(model="nova-3", audio=b"\x00\x01", filename="turn.webm")
        result = asyncio.run(_gateway(transport).run(request))

        operation, kwargs = transport.invocations[0]
        assert operation == "atranscription"
        assert kwargs == {"model": "openai/nova-3", "file": ("turn.webm", b"\x00\x01")}
        assert isinstance(result, TranscriptionResult)
        assert result.text == "hello there"

    def test_the_route_prefix_is_configurable_and_never_reaches_the_id(self):
        transport = ScriptedTransport(payloads=[{"audio": WAV_HEAD}])
        gateway = _gateway(transport, route_prefix="litellm_proxy")
        request = SynthesisRequest(model="supertonic-3", text="hi", voice="nova")
        result = asyncio.run(gateway.run(request))
        assert transport.invocations[0][1]["model"] == "litellm_proxy/supertonic-3"
        assert result.model == "supertonic-3"

    def test_the_gateway_coordinates_REACH_the_sdk_call(self):
        # THE REGRESSION GUARD. Without this the audio legs carried no
        # credentials at all: litellm fell back to its own provider resolution
        # and failed with `OpenAIException - Missing credentials`, naming an
        # OPENAI_API_KEY nobody set and never mentioning our proxy. The suite
        # was green throughout, because the double ignored what it was not sent.
        transport = ScriptedTransport(payloads=[{"audio": WAV_HEAD}, {"text": "hi"}])
        gateway = _gateway(transport, timeout=42.0)
        asyncio.run(gateway.run(SynthesisRequest(model="supertonic-3", text="hi", voice="alloy")))
        asyncio.run(gateway.run(TranscriptionRequest(model="nova-3", audio=b"\x00")))
        assert transport.connections == [
            {
                "api_base": "http://gateway.invalid/v1",
                "api_key": "sk-test",
                "timeout": 42.0,
                "max_retries": DEFAULT_SYNTHESIS_MAX_RETRIES,
            },
            {
                "api_base": "http://gateway.invalid/v1",
                "api_key": "sk-test",
                "timeout": 42.0,
                "max_retries": DEFAULT_TRANSCRIPTION_MAX_RETRIES,
            },
        ]

    def test_synthesis_and_transcription_use_their_own_retry_counts(self):
        # The asymmetry is the point: a failing synthesis burns 4-8s of cold
        # backend time PER RETRY and can park a shared backend slot for ~14s
        # regardless of how it ends, while transcription is cheap even doubled.
        # See DEFAULT_SYNTHESIS_MAX_RETRIES's docstring for the measurement.
        transport = ScriptedTransport(payloads=[{"audio": WAV_HEAD}, {"text": "hi"}])
        gateway = _gateway(
            transport, synthesis_max_retries=0, transcription_max_retries=3
        )
        asyncio.run(gateway.run(SynthesisRequest(model="supertonic-3", text="hi", voice="alloy")))
        asyncio.run(gateway.run(TranscriptionRequest(model="nova-3", audio=b"\x00")))
        assert transport.connections[0]["max_retries"] == 0
        assert transport.connections[1]["max_retries"] == 3

    def test_credentials_stay_out_of_the_request_contract(self):
        # They ride the gateway, never the Pydantic model — a contract that
        # carried a key would serialise it into any payload built from it.
        request = SynthesisRequest(model="supertonic-3", text="hi", voice="alloy")
        assert "api_key" not in request.litellm_kwargs("openai")
        assert "api_key" not in request.model_dump()

    def test_cancelling_the_caller_aborts_the_in_flight_call(self):
        """Stopping a read must abort the call, not normalise it into an error.

        ``invoke`` wraps the SDK call in ``except Exception`` to turn provider
        failures into ``SpeechGatewayError``. ``CancelledError`` derives from
        ``BaseException``, not ``Exception``, so it slips past that handler and
        propagates — which is what makes ``asyncio.CancelledError`` the whole
        cancellation mechanism and means the package needs no cancel token of
        its own. Widening that handler to ``BaseException`` would silently
        convert a stop into a failed synthesis, and the caller would keep going.
        """
        started = asyncio.Event()

        class HangingTransport(ScriptedTransport):
            async def invoke(
                self, operation, /, *, api_base, api_key, timeout, max_retries, **kwargs
            ):
                started.set()
                await asyncio.sleep(30)
                raise AssertionError("should have been cancelled")

        async def scenario() -> None:
            gateway = _gateway(HangingTransport())
            task = asyncio.create_task(
                gateway.run(SynthesisRequest(model="supertonic-3", text="hi", voice="alloy"))
            )
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())

    def test_a_gateway_failure_surfaces_as_is(self):
        transport = ScriptedTransport(fail=SpeechGatewayError("aspeech failed: HTTP 500"))
        request = SynthesisRequest(model="supertonic-3", text="hi", voice="alloy")
        with pytest.raises(SpeechGatewayError, match="HTTP 500"):
            asyncio.run(_gateway(transport).run(request))


class TestConcurrencyBound:
    """The semaphore actually BINDS, and cancellation never leaks a permit.

    Every assertion here is on OBSERVED BEHAVIOUR — a counter that watches how
    many coroutines are simultaneously inside the transport call — never on
    the semaphore object's existence or construction. An injected double that
    merely proves the bound was passed through would leave this exactly as
    unverified as the credential bug the package's own CLAUDE.md warns about:
    an injected double proves the call SHAPE, never that the behaviour it
    names actually happened.
    """

    class ConcurrencyTrackingTransport(ScriptedTransport):
        """Counts how many ``invoke`` calls are in flight AT ONCE.

        Each call increments on entry, records the running peak, awaits a
        shared gate (so every call is genuinely overlapping rather than
        finishing before the next starts), then decrements on the way out —
        even when cancelled, because the decrement sits in a ``finally``.
        """

        def __init__(self, *, release_after: float = 0.05, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.release_after = release_after
            self.in_flight = 0
            self.max_in_flight = 0
            self._lock = asyncio.Lock()

        async def invoke(
            self, operation, /, *, api_base, api_key, timeout, max_retries, **kwargs
        ):
            async with self._lock:
                self.in_flight += 1
                self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                await asyncio.sleep(self.release_after)
                return {"audio": WAV_HEAD}
            finally:
                async with self._lock:
                    self.in_flight -= 1

    def test_the_bound_caps_concurrent_transport_calls(self):
        # Six requests, semaphore sized 2: at most 2 may ever be inside
        # `invoke` at once, proven by the tracked PEAK, not by the elapsed
        # time (a wall-clock assertion on a shared box is exactly what this
        # repo's testing guidance warns is flaky and can pass for the wrong
        # reason).
        transport = self.ConcurrencyTrackingTransport()
        gateway = _gateway(transport, max_concurrent_calls=2)

        async def scenario() -> None:
            requests = [
                SynthesisRequest(model="supertonic-3", text=f"hi {i}", voice="alloy")
                for i in range(6)
            ]
            await asyncio.gather(*(gateway.run(r) for r in requests))

        asyncio.run(scenario())
        assert transport.max_in_flight == 2
        assert transport.in_flight == 0

    def test_a_bound_of_one_serialises_every_call(self):
        transport = self.ConcurrencyTrackingTransport()
        gateway = _gateway(transport, max_concurrent_calls=1)

        async def scenario() -> None:
            requests = [
                SynthesisRequest(model="supertonic-3", text=f"hi {i}", voice="alloy")
                for i in range(4)
            ]
            await asyncio.gather(*(gateway.run(r) for r in requests))

        asyncio.run(scenario())
        assert transport.max_in_flight == 1

    def test_a_higher_bound_admits_more_concurrency(self):
        # The negative control for the n=2 case above: raising the bound
        # raises the observed peak too, so the cap above is provably the
        # semaphore's doing and not some other accidental serialisation
        # (e.g. the event loop, or the lock inside the tracking double).
        transport = self.ConcurrencyTrackingTransport()
        gateway = _gateway(transport, max_concurrent_calls=4)

        async def scenario() -> None:
            requests = [
                SynthesisRequest(model="supertonic-3", text=f"hi {i}", voice="alloy")
                for i in range(6)
            ]
            await asyncio.gather(*(gateway.run(r) for r in requests))

        asyncio.run(scenario())
        assert transport.max_in_flight == 4

    def test_a_shared_semaphore_bounds_ACROSS_gateway_instances(self):
        # The shape ``SpeechRoutesController`` relies on: a fresh SpeechGateway
        # per call, but ONE injected semaphore threaded into every one of
        # them (`init_speech_routes` in the api). Two separate gateway
        # instances sharing a `concurrency=` object must still cap combined
        # in-flight calls at the semaphore's own bound — if they did not,
        # the "build fresh per call" pattern the api controller documents
        # would silently defeat this entire feature at the one real
        # production call site.
        transport = self.ConcurrencyTrackingTransport()
        shared = asyncio.Semaphore(2)
        gateways = [_gateway(transport, concurrency=shared) for _ in range(3)]

        async def scenario() -> None:
            requests = [
                SynthesisRequest(model="supertonic-3", text=f"hi {i}", voice="alloy")
                for i in range(6)
            ]
            await asyncio.gather(
                *(gateway.run(r) for gateway, r in zip(gateways * 2, requests, strict=True))
            )

        asyncio.run(scenario())
        assert transport.max_in_flight == 2

    def test_cancelling_a_waiter_does_not_leak_a_permit(self):
        # Bound of 1: the first call holds the only permit; a SECOND task
        # blocks waiting for it and is cancelled WHILE WAITING (never
        # acquired). If cancellation of a *waiter* corrupted the semaphore's
        # bookkeeping, the bound would silently widen or narrow for every
        # call after it — asyncio.Semaphore's own cancellation branch is what
        # this test exercises, not code this package wrote, but the whole
        # design leans on that guarantee holding.
        transport = self.ConcurrencyTrackingTransport(release_after=0.2)
        gateway = _gateway(transport, max_concurrent_calls=1)
        holder_started = asyncio.Event()

        async def scenario() -> None:
            async def hold_the_permit() -> None:
                async with gateway.concurrency:
                    holder_started.set()
                    await asyncio.sleep(0.3)

            holder = asyncio.create_task(hold_the_permit())
            await holder_started.wait()

            waiter = asyncio.create_task(
                gateway.run(SynthesisRequest(model="supertonic-3", text="hi", voice="alloy"))
            )
            await asyncio.sleep(0.02)  # let it start queueing on the semaphore
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            await holder

            # The permit the holder released must still be usable — a leaked
            # waiter cancellation would either strand it at 0 forever or
            # double-count it.
            assert gateway.concurrency.locked() is False
            await gateway.run(SynthesisRequest(model="supertonic-3", text="after", voice="alloy"))

        asyncio.run(scenario())
        assert transport.max_in_flight == 1

    def test_cancelling_a_holder_still_releases_the_permit(self):
        # Bound of 1: the in-flight call is cancelled WHILE HOLDING the
        # permit (mid-transport-call, not while queueing). `async with`
        # guarantees `__aexit__` -> `release()` runs on ANY exit, cancellation
        # included, which is the property this package's cancellation design
        # depends on — a leaked permit here would permanently shrink the
        # gateway's own concurrency by one for the rest of the process, with
        # nothing failing loudly to say so.
        started = asyncio.Event()

        class SignallingTransport(self.ConcurrencyTrackingTransport):
            async def invoke(self, *args, **kwargs):
                started.set()
                return await super().invoke(*args, **kwargs)

        transport = SignallingTransport(release_after=30)
        gateway = _gateway(transport, max_concurrent_calls=1)

        async def scenario() -> None:
            task = asyncio.create_task(
                gateway.run(SynthesisRequest(model="supertonic-3", text="hi", voice="alloy"))
            )
            await started.wait()
            # The task is now INSIDE `async with self.concurrency:`, awaiting
            # the transport's 30s sleep — holding the one permit.
            assert gateway.concurrency.locked() is True
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            # The permit must be back, not stranded on the cancelled task. Swap
            # in a fast transport for the proof call — the cancelled one still
            # has a 30s sleep queued and reusing it would only prove the wait
            # itself works, not the permit.
            assert gateway.concurrency.locked() is False
            gateway.transport = self.ConcurrencyTrackingTransport()
            await asyncio.wait_for(
                gateway.run(SynthesisRequest(model="supertonic-3", text="after", voice="alloy")),
                timeout=1.0,
            )

        asyncio.run(scenario())


class _BlockingImportlib:
    """An importlib stand-in whose named modules are missing.

    Patched onto the transport module's own `importlib` reference rather than
    into `sys.modules`. Nulling `sys.modules["litellm"]` looks like the more
    honest seam and is a trap: litellm's SUBMODULES stay cached, so the next real
    `import litellm` re-executes its `__init__` against a half-populated package
    and dies with a circular-import AttributeError — in a LATER test, which then
    fails for a reason that has nothing to do with what it asserts.
    """

    def __init__(self, *blocked: str) -> None:
        self.blocked = set(blocked)

    def import_module(self, name: str):
        if name in self.blocked:
            raise ImportError(f"No module named {name!r}")
        return __import__(name)


class TestGracefulAbsence:
    """Without the extra the feature is absent, never a crash."""

    def test_the_package_imports_and_works_with_the_extra_uninstalled(self):
        # A fresh interpreter with both dependencies blocked BEFORE any import,
        # so nothing is half-cached: this is the deployment that installed
        # `mewbo-speech` without `[gateway]`. Run out of process because that
        # state cannot be created inside a worker that already imported litellm.
        script = (
            "import sys\n"
            "sys.modules['litellm'] = None\n"
            "sys.modules['httpx'] = None\n"
            "from mewbo_speech import AudioContainer, SynthesisRequest\n"
            "assert AudioContainer.sniff(b'RIFF\\x00\\x00\\x00\\x00WAVE') "
            "is AudioContainer.WAV\n"
            "assert SynthesisRequest(model='supertonic-3', text='hi', "
            "voice='alloy').voice == 'alloy'\n"
            "print('ok')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().endswith("ok")

    def test_the_transport_reports_itself_unavailable(self, monkeypatch):
        monkeypatch.setattr(transport_module, "importlib", _BlockingImportlib("litellm"))
        assert LiteLlmSpeechTransport.is_available() is False

    def test_a_call_raises_a_message_naming_the_extra(self, monkeypatch):
        monkeypatch.setattr(transport_module, "importlib", _BlockingImportlib("litellm"))
        request = SynthesisRequest(model="supertonic-3", text="hi", voice="alloy")
        gateway = SpeechGateway(
            api_base="http://gateway.invalid/v1",
            api_key="sk-test",
            transport=LiteLlmSpeechTransport(),
        )
        with pytest.raises(SpeechUnavailableError) as excinfo:
            asyncio.run(gateway.run(request))
        message = str(excinfo.value)
        assert "mewbo-speech[gateway]" in message
        assert "litellm" in message

    def test_the_listing_leg_does_not_need_litellm(self, monkeypatch):
        # Per-leg probing: a missing litellm must not fail a listing that only
        # ever touches httpx.
        monkeypatch.setattr(transport_module, "importlib", _BlockingImportlib("litellm"))
        assert LiteLlmSpeechTransport._require("httpx")["httpx"] is not None

    def test_an_unavailable_transport_makes_the_gateway_report_absent(self, monkeypatch):
        monkeypatch.setattr(transport_module, "importlib", _BlockingImportlib("httpx"))
        gateway = SpeechGateway(
            api_base="http://gateway.invalid/v1",
            api_key="sk-test",
            transport=LiteLlmSpeechTransport(),
        )
        assert gateway.is_available() is False


class _ModelInfoHandler(BaseHTTPRequestHandler):
    """Serves one `/model/info` document and records what was asked for."""

    seen: list[tuple[str, str | None]] = []

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's contract
        type(self).seen.append((self.path, self.headers.get("Authorization")))
        body = json.dumps({"data": MODEL_INFO}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silence the handler's stderr logging.

        Signature matches `BaseHTTPRequestHandler.log_message` exactly, shadowed
        builtin name included — a narrower override is an LSP violation the base
        class can call through.
        """


class TestDefaultTransportAgainstARealListener:
    """The DEFAULT transport, exercised for real — no mock in the path.

    An injected-transport suite says nothing about the transport a deployment
    constructs: the URL join, the Bearer header and the `data` unwrap are all
    only in the default implementation. A loopback listener exercises every one
    of them without leaving the machine.
    """

    @pytest.fixture()
    def listener(self):
        _ModelInfoHandler.seen = []
        server = HTTPServer(("127.0.0.1", 0), _ModelInfoHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_the_default_transport_is_the_litellm_one(self):
        gateway = SpeechGateway(api_base="http://gateway.invalid/v1", api_key="sk-test")
        assert isinstance(gateway.transport, LiteLlmSpeechTransport)

    def test_it_hits_model_info_with_a_bearer_header_and_classifies_the_result(self, listener):
        gateway = SpeechGateway(api_base=listener, api_key="sk-live", timeout=5.0)
        models = gateway.list_models()

        path, authorization = _ModelInfoHandler.seen[0]
        assert path == "/v1/model/info"
        assert authorization == "Bearer sk-live"
        assert [model.id for model in models] == ["supertonic-3", "supertonic-3-hd", "nova-3"]
        assert gateway.is_available() is True

    def test_a_trailing_slash_on_api_base_does_not_double_up(self, listener):
        SpeechGateway(api_base=listener + "/", api_key="sk-live", timeout=5.0).list_models()
        assert _ModelInfoHandler.seen[0][0] == "/v1/model/info"

    def test_an_unreachable_gateway_is_a_normalised_error(self):
        gateway = SpeechGateway(
            api_base="http://127.0.0.1:1/v1", api_key="sk-live", timeout=2.0
        )
        with pytest.raises(SpeechGatewayError, match="model listing failed"):
            gateway.list_models()


class TestFromConfig:
    """Config reads tolerate a missing `speech` section entirely."""

    def test_it_falls_back_to_the_llm_gateway_when_no_speech_section_exists(self, monkeypatch):
        from mewbo_core import config as core_config

        values = {("llm", "api_base"): "http://llm.invalid/v1", ("llm", "api_key"): "sk-llm"}

        def fake_get_config_value(*keys, default=None):
            return values.get(tuple(keys), default)

        monkeypatch.setattr(core_config, "get_config_value", fake_get_config_value)
        gateway = SpeechGateway.from_config(transport=ScriptedTransport())
        assert gateway.api_base == "http://llm.invalid/v1"
        assert gateway.api_key == "sk-llm"

    def test_a_speech_section_wins_when_present(self, monkeypatch):
        from mewbo_core import config as core_config

        values = {
            ("speech", "api_base"): "http://speech.invalid/v1",
            ("speech", "api_key"): "sk-speech",
            ("speech", "timeout"): 30.0,
            ("llm", "api_base"): "http://llm.invalid/v1",
            ("llm", "api_key"): "sk-llm",
        }

        def fake_get_config_value(*keys, default=None):
            return values.get(tuple(keys), default)

        monkeypatch.setattr(core_config, "get_config_value", fake_get_config_value)
        gateway = SpeechGateway.from_config(transport=ScriptedTransport())
        assert gateway.api_base == "http://speech.invalid/v1"
        assert gateway.api_key == "sk-speech"
        assert gateway.timeout == 30.0

    def test_an_entirely_unconfigured_process_yields_an_absent_gateway(self, monkeypatch):
        from mewbo_core import config as core_config

        monkeypatch.setattr(
            core_config, "get_config_value", lambda *keys, default=None: default
        )
        gateway = SpeechGateway.from_config(transport=ScriptedTransport())
        assert gateway.is_available() is False
