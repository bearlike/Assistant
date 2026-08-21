"""Route tests for the speech namespace.

Everything is exercised through the real Flask app and the real controller; the
only thing swapped out is the SOCKET. ``mewbo_speech`` is built around a
``SpeechTransport`` protocol precisely so a test can script the gateway's
responses while every request built, every validator run and every response
parsed stays production code — so these tests stub the transport, never the
gateway, the controller or the routes.

The registered controller is re-pointed by reassigning its FIELDS, which is what
the module-level composition-root handle exists for: Flask bakes
``resource_class_kwargs`` into the view closure at registration, so replacing the
module global would change nothing about what the live routes hold.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from typing import Any
from unittest import mock

import pytest
from mewbo_api.speech import init_speech, routes as speech_routes
from mewbo_core.config import SpeechConfig
from mewbo_speech import SpeechGateway, SpeechGatewayError

# A minimal but real RIFF/WAVE header — ``AudioContainer.sniff`` reads the first
# twelve bytes, so this is genuinely sniffable rather than a placeholder.
WAV_BYTES = b"RIFF$\x00\x00\x00WAVEfmt " + b"\x00" * 32
FLAC_BYTES = b"fLaC" + b"\x00" * 32

MODEL_INFO = [
    {"model_name": "supertonic-3", "model_info": {"mode": "audio_speech"}},
    {"model_name": "supertonic-3-hd", "model_info": {"mode": "audio_speech"}},
    {"model_name": "nova-3", "model_info": {"mode": "audio_transcription"}},
    # A chat route in the same document: the classifier must drop it rather than
    # raise, or a healthy gateway reports zero speech models.
    {"model_name": "gpt-4o", "model_info": {"mode": "chat"}},
]


class ScriptedTransport:
    """A ``SpeechTransport`` that answers from a script and records its calls.

    The gateway COORDINATES are recorded separately from the operation kwargs,
    mirroring the package's own double. A transport that swallowed them into
    ``**kwargs`` is what let a credential-free call ship green once already: an
    injected double proves the call shape, never that the call was addressed
    anywhere.
    """

    def __init__(
        self,
        *,
        model_info: list[Mapping[str, Any]] | None = None,
        invoke_result: Mapping[str, Any] | None = None,
        model_info_error: Exception | None = None,
        invoke_error: Exception | None = None,
    ) -> None:
        self.model_info = MODEL_INFO if model_info is None else model_info
        self.invoke_result = invoke_result
        self.model_info_error = model_info_error
        self.invoke_error = invoke_error
        self.invocations: list[tuple[str, dict[str, Any]]] = []
        self.coordinates: list[tuple[str, str, float]] = []
        self.model_info_calls = 0

    @staticmethod
    def is_available() -> bool:
        return True

    def fetch_model_info(
        self, *, api_base: str, api_key: str, timeout: float
    ) -> list[Mapping[str, Any]]:
        self.model_info_calls += 1
        if self.model_info_error is not None:
            raise self.model_info_error
        return list(self.model_info)

    async def invoke(
        self,
        operation: str,
        /,
        *,
        api_base: str,
        api_key: str,
        timeout: float,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        self.invocations.append((operation, dict(kwargs)))
        self.coordinates.append((api_base, api_key, timeout))
        if self.invoke_error is not None:
            raise self.invoke_error
        return self.invoke_result or {"audio": WAV_BYTES, "content_type": "audio/mpeg"}


def speech_config(**overrides: Any) -> SpeechConfig:
    """A ``speech`` config section with both directions configured by default."""
    payload: dict[str, Any] = {
        "api_base": "https://gateway.example.com/v1",
        "api_key": "sk-test",
        "tts": {"model": "supertonic-3", "voice": "nova", "response_format": "wav"},
        "stt": {"model": "nova-3"},
    }
    payload.update(overrides)
    return SpeechConfig.model_validate(payload)


@pytest.fixture()
def controller(transport: ScriptedTransport):
    """Point the ONE registered controller at a scripted gateway, then restore.

    Fields are reassigned rather than the module handle replaced: production
    holds the instance, not the name. The catalogue caches are cleared too, since
    they live for the life of the process and would otherwise leak a previous
    test's model list — or its 60s failure window — into this one.
    """
    live = speech_routes._controller
    assert live is not None, "init_speech_routes did not run; is mewbo_speech installed?"
    saved = (live.gateway_reader, live.config_reader, live.monotonic)
    live.gateway_reader = lambda: SpeechGateway(
        api_base="https://gateway.example.com/v1", api_key="sk-test", transport=transport
    )
    live.config_reader = speech_config
    live._catalogue = None
    live._catalogue_failed_until = 0.0
    yield live
    live.gateway_reader, live.config_reader, live.monotonic = saved
    live._catalogue = None
    live._catalogue_failed_until = 0.0


@pytest.fixture()
def transport() -> ScriptedTransport:
    return ScriptedTransport()


def envelope(response: Any) -> dict[str, Any]:
    """The ``{"code", "reason", "retryable"}`` object out of a refusal body."""
    body = json.loads(response.data)
    assert "error" in body, f"expected the error envelope, got {body!r}"
    return body["error"]


# ---------------------------------------------------------------------------
# The mount IS the availability signal
# ---------------------------------------------------------------------------


class TestMountGuard:
    """``init_speech`` probes the library, not the route module."""

    def test_absent_library_mounts_nothing_and_reports_false(self) -> None:
        """A deployment without ``mewbo_speech`` registers no namespace."""
        api = mock.Mock()
        with mock.patch.dict(sys.modules, {"mewbo_speech": None}):
            assert init_speech(api) is False
        api.add_namespace.assert_not_called()

    # There is deliberately no mirror-image "init_speech(mock) returns True" test.
    # Re-running the composition root REBINDS the module-level ``_controller``
    # while the live Resources keep the instance Flask baked into their view
    # closures at registration — so every later test would configure one object
    # and exercise another, which is exactly the trap that handle's docstring
    # warns about. The positive case is proven below against the REAL app, which
    # is the stronger claim anyway: a mock cannot tell you a URL rule exists.

    def test_routes_are_reachable_on_the_composed_app(self, client, auth_headers) -> None:
        """The three paths exist on the real app — not merely on a mock."""
        for path in (
            "/api/speech/capabilities",
            "/api/speech/synthesize",
            "/api/speech/transcribe",
        ):
            assert client.open(path, method="OPTIONS", headers=auth_headers).status_code != 404


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


class TestCapabilities:
    """Derived from config, bounded, and never a 500."""

    def test_reports_models_voices_formats_defaults_and_limits(
        self, client, auth_headers, controller, transport
    ) -> None:
        response = client.get("/api/speech/capabilities", headers=auth_headers)
        assert response.status_code == 200
        body = response.get_json()

        synthesis = body["synthesis"]
        assert synthesis["available"] is True
        assert [m["id"] for m in synthesis["models"]] == ["supertonic-3", "supertonic-3-hd"]
        assert {m["mode"] for m in synthesis["models"]} == {"audio_speech"}
        assert len(synthesis["voices"]) == 11
        assert "nova" in synthesis["voices"]
        assert synthesis["formats"] == ["wav", "flac"]
        assert synthesis["defaults"] == {
            "model": "supertonic-3",
            "voice": "nova",
            "response_format": "wav",
        }
        assert synthesis["limits"] == {"max_text_chars": 2000}

        transcription = body["transcription"]
        assert transcription["available"] is True
        assert [m["id"] for m in transcription["models"]] == ["nova-3"]
        assert transcription["defaults"] == {"model": "nova-3"}
        assert transcription["limits"] == {"max_audio_bytes": 10485760}

        assert body["limits"] == {"max_concurrent_calls": 4}

    def test_unconfigured_gateway_reports_unavailable_without_a_probe(
        self, client, auth_headers, controller, transport
    ) -> None:
        """No coordinates means unavailable, and nothing is asked of the network."""
        controller.gateway_reader = lambda: SpeechGateway(
            api_base="", api_key="", transport=transport
        )
        body = client.get("/api/speech/capabilities", headers=auth_headers).get_json()
        assert body["synthesis"]["available"] is False
        assert body["transcription"]["available"] is False
        assert transport.model_info_calls == 0

    def test_missing_model_id_disables_only_that_direction(
        self, client, auth_headers, controller
    ) -> None:
        """An empty ``speech.stt.model`` turns dictation off, not read-aloud."""
        controller.config_reader = lambda: speech_config(stt={"model": ""})
        body = client.get("/api/speech/capabilities", headers=auth_headers).get_json()
        assert body["synthesis"]["available"] is True
        assert body["transcription"]["available"] is False
        assert body["transcription"]["defaults"] == {"model": ""}

    def test_dead_gateway_degrades_lists_and_never_500s(
        self, client, auth_headers, controller
    ) -> None:
        """A failed listing empties the models; everything else still answers."""
        dead = ScriptedTransport(model_info_error=SpeechGatewayError("connection refused"))
        controller.gateway_reader = lambda: SpeechGateway(
            api_base="https://gateway.example.com/v1", api_key="sk-test", transport=dead
        )
        response = client.get("/api/speech/capabilities", headers=auth_headers)
        assert response.status_code == 200
        body = response.get_json()
        # Discovery contributes nothing, but the CONFIGURED model is still
        # offered — an outage must not empty the picker, because on this
        # deployment discovery is empty at the best of times (the route
        # carrying a model's mode is closed to the runtime key).
        assert [m["id"] for m in body["synthesis"]["models"]] == ["supertonic-3"]
        assert [m["id"] for m in body["transcription"]["models"]] == ["nova-3"]
        # Availability is derived from config, so it is unaffected by the outage.
        assert body["synthesis"]["available"] is True
        assert body["synthesis"]["voices"]
        assert body["synthesis"]["defaults"]["voice"] == "nova"

    def test_a_failed_listing_is_cached_so_a_dead_gateway_is_asked_once(
        self, client, auth_headers, controller
    ) -> None:
        """The failure window is what stops every poll paying the 3s stall."""
        dead = ScriptedTransport(model_info_error=SpeechGatewayError("connection refused"))
        controller.gateway_reader = lambda: SpeechGateway(
            api_base="https://gateway.example.com/v1", api_key="sk-test", transport=dead
        )
        now = 1000.0
        controller.monotonic = lambda: now
        for _ in range(3):
            client.get("/api/speech/capabilities", headers=auth_headers)
        assert dead.model_info_calls == 1

        # Past the TTL the gateway is retried exactly once more.
        now = 1000.0 + controller.CATALOGUE_FAILURE_TTL_S + 1
        client.get("/api/speech/capabilities", headers=auth_headers)
        client.get("/api/speech/capabilities", headers=auth_headers)
        assert dead.model_info_calls == 2

    def test_a_successful_listing_is_cached_for_process_life(
        self, client, auth_headers, controller, transport
    ) -> None:
        for _ in range(3):
            client.get("/api/speech/capabilities", headers=auth_headers)
        assert transport.model_info_calls == 1

    def test_the_catalogue_read_carries_the_short_deadline(
        self, client, auth_headers, controller
    ) -> None:
        """A listing on an interactive path must not inherit the 90s call timeout."""
        seen: list[float] = []

        class RecordingTransport(ScriptedTransport):
            def fetch_model_info(self, *, api_base: str, api_key: str, timeout: float):
                seen.append(timeout)
                return super().fetch_model_info(api_base=api_base, api_key=api_key, timeout=timeout)

        recorder = RecordingTransport()
        controller.gateway_reader = lambda: SpeechGateway(
            api_base="https://gateway.example.com/v1", api_key="sk-test", transport=recorder
        )
        client.get("/api/speech/capabilities", headers=auth_headers)
        assert seen == [controller.CATALOGUE_TIMEOUT_S]

    def test_requires_a_credential(self, client) -> None:
        assert client.get("/api/speech/capabilities").status_code == 401


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------


class TestSynthesize:
    """Bytes out, sniffed content type, and refusals before the gateway."""

    def test_returns_raw_bytes_with_a_sniffed_content_type(
        self, client, auth_headers, controller, transport
    ) -> None:
        """The gateway's declared ``audio/mpeg`` must never reach the client."""
        response = client.post(
            "/api/speech/synthesize",
            headers=auth_headers,
            json={"text": "The build finished."},
        )
        assert response.status_code == 200
        assert response.mimetype == "audio/wav"
        assert response.data == WAV_BYTES

    def test_flac_is_labelled_from_the_payload_not_the_request(
        self, client, auth_headers, controller, transport
    ) -> None:
        transport.invoke_result = {"audio": FLAC_BYTES, "content_type": "audio/mpeg"}
        response = client.post(
            "/api/speech/synthesize",
            headers=auth_headers,
            json={"text": "hello", "response_format": "flac"},
        )
        assert response.mimetype == "audio/flac"

    def test_omitted_fields_resolve_to_the_configured_defaults(
        self, client, auth_headers, controller, transport
    ) -> None:
        client.post("/api/speech/synthesize", headers=auth_headers, json={"text": "hello"})
        operation, kwargs = transport.invocations[-1]
        assert operation == "aspeech"
        # Bare id on the wire, prefixed for the SDK's local provider dispatch.
        assert kwargs["model"] == "openai/supertonic-3"
        assert kwargs["voice"] == "nova"
        assert kwargs["response_format"] == "wav"
        # Verbalization is on by default and closes each spoken unit with a full
        # stop. On a single unit that is inaudible — "hello" and "hello."
        # measured byte-identical clip lengths — but the engine takes its pauses
        # from punctuation alone, so between units it is what stops one running
        # into the next.
        assert kwargs["input"] == "hello."

    def test_the_gateway_coordinates_reach_the_sdk_call(
        self, client, auth_headers, controller, transport
    ) -> None:
        """A call has to be ADDRESSED somewhere, and only a real arg proves it.

        The package shipped green once with the coordinates never forwarded — the
        double ignored what it was never sent. Asserting them here is the API-side
        half of that lesson.
        """
        client.post("/api/speech/synthesize", headers=auth_headers, json={"text": "hi"})
        api_base, api_key, timeout = transport.coordinates[-1]
        assert api_base == "https://gateway.example.com/v1"
        assert api_key == "sk-test"
        assert timeout > 0

    def test_explicit_fields_win_over_the_defaults(
        self, client, auth_headers, controller, transport
    ) -> None:
        client.post(
            "/api/speech/synthesize",
            headers=auth_headers,
            json={
                "text": "hello",
                "model": "supertonic-3-hd",
                "voice": "ash",
                "response_format": "flac",
            },
        )
        _, kwargs = transport.invocations[-1]
        assert kwargs["model"] == "openai/supertonic-3-hd"
        assert kwargs["voice"] == "ash"
        assert kwargs["response_format"] == "flac"

    @pytest.mark.parametrize(
        ("body", "field", "expected_in_reason"),
        [
            ({"text": "hi", "response_format": "mp3"}, "response_format", "wav, flac"),
            ({"text": "   "}, "text", "blank"),
            ({"text": "x" * 2001}, "text", "2000"),
            ({"text": "hi", "audio_format": "wav"}, "audio_format", "audio_format"),
            ({}, "text", "text"),
        ],
    )
    def test_bad_input_is_refused_before_any_gateway_call(
        self, client, auth_headers, controller, transport, body, field, expected_in_reason
    ) -> None:
        """Every one of these reaches the gateway as an undiagnosable 500 if let through."""
        response = client.post("/api/speech/synthesize", headers=auth_headers, json=body)
        assert response.status_code == 400
        error = envelope(response)
        assert error["code"] == "invalid_request"
        assert error["retryable"] is False
        assert field in error["reason"]
        assert expected_in_reason in error["reason"]
        assert transport.invocations == [], "a refused request must not reach the gateway"

    def test_an_unfamiliar_voice_reaches_the_gateway(
        self, client, auth_headers, controller, transport
    ) -> None:
        """A voice this side does not recognise is forwarded, not refused.

        It once 400'd against a closed list of OpenAI's names, which made a
        self-hosted backend's own trained style unusable: the refusal named an
        accepted set it had no way to know. Which voices exist is the gateway's
        fact, so an unknown one fails THERE, where the truth is.
        """
        response = client.post(
            "/api/speech/synthesize", headers=auth_headers, json={"text": "hi", "voice": "Boss"}
        )
        assert response.status_code == 200
        _, kwargs = transport.invocations[-1]
        assert kwargs["voice"] == "Boss"

    def test_gateway_failure_maps_to_502_carrying_its_message(
        self, client, auth_headers, controller, transport
    ) -> None:
        transport.invoke_error = SpeechGatewayError("aspeech failed: Internal server error")
        response = client.post("/api/speech/synthesize", headers=auth_headers, json={"text": "hi"})
        assert response.status_code == 502
        error = envelope(response)
        assert error["code"] == "speech_gateway_error"
        assert error["retryable"] is True
        assert "Internal server error" in error["reason"]

    def test_our_deadline_maps_to_a_distinct_502(
        self, client, auth_headers, controller, transport
    ) -> None:
        """A timeout is told apart from a refusal: the work may still be running."""
        controller.SYNTHESIS_DEADLINE_S = 0.01

        async def _never(operation: str, /, **kwargs: Any):
            import asyncio

            await asyncio.sleep(5)
            return {}

        transport.invoke = _never  # type: ignore[method-assign]
        try:
            response = client.post(
                "/api/speech/synthesize", headers=auth_headers, json={"text": "hi"}
            )
        finally:
            del controller.SYNTHESIS_DEADLINE_S
        assert response.status_code == 502
        error = envelope(response)
        assert error["code"] == "speech_gateway_timeout"
        assert error["retryable"] is True

    def test_unconfigured_gateway_refuses_with_503(
        self, client, auth_headers, controller, transport
    ) -> None:
        controller.gateway_reader = lambda: SpeechGateway(
            api_base="", api_key="", transport=transport
        )
        response = client.post("/api/speech/synthesize", headers=auth_headers, json={"text": "hi"})
        assert response.status_code == 503
        error = envelope(response)
        assert error["code"] == "speech_unavailable"
        assert error["retryable"] is False
        assert transport.invocations == []

    def test_an_invalid_config_document_says_so_rather_than_blaming_speech(
        self, client, auth_headers, controller
    ) -> None:
        """``from_config`` validates the WHOLE document, so the 503 must not misdirect.

        Telling an operator to "set speech.api_base" when the real failure is an
        unset ``${ENV_VAR}`` in another section sends them to read a block that
        is already correct.
        """

        def _explode() -> Any:
            raise ValueError("1 validation error for AppConfig: langfuse.host")

        controller.gateway_reader = _explode
        response = client.post("/api/speech/synthesize", headers=auth_headers, json={"text": "hi"})
        assert response.status_code == 503
        error = envelope(response)
        assert error["code"] == "speech_unavailable"
        assert "failed to validate" in error["reason"]
        assert "speech.api_base" not in error["reason"]

    def test_requires_a_credential(self, client) -> None:
        assert client.post("/api/speech/synthesize", json={"text": "hi"}).status_code == 401


# ---------------------------------------------------------------------------
# Markdown verbalization — the text that actually reaches the gateway
# ---------------------------------------------------------------------------


class TestSynthesizeVerbalizesMarkdown:
    """What the gateway is SENT, which is the only thing a listener hears.

    Asserting on the response bytes cannot see this — the scripted transport
    returns the same WAV whatever it is asked to say. The evidence is the
    ``input`` kwarg, so every test here reads ``transport.invocations``.
    """

    def test_markdown_is_read_as_markdown_by_default(
        self, client, auth_headers, controller, transport
    ) -> None:
        """No opt-in: a client posting an assistant's reply gets speech, not markup."""
        source = (
            "## Results\n\nSee the [docs](https://example.com/a_(b)).\n\n```\nrm -rf /\n```"
        )
        client.post("/api/speech/synthesize", headers=auth_headers, json={"text": source})
        _, kwargs = transport.invocations[-1]
        spoken = kwargs["input"]
        assert "##" not in spoken and "```" not in spoken
        assert "example.com" not in spoken
        assert "rm -rf" not in spoken
        assert spoken.startswith("Results.")

    def test_verbalize_false_sends_the_string_exactly_as_written(
        self, client, auth_headers, controller, transport
    ) -> None:
        source = "## Not a heading, just text with `backticks`."
        client.post(
            "/api/speech/synthesize",
            headers=auth_headers,
            json={"text": source, "verbalize": False},
        )
        _, kwargs = transport.invocations[-1]
        assert kwargs["input"] == source

    def test_a_table_reaches_the_gateway_with_its_header_announced_once(
        self, client, auth_headers, controller, transport
    ) -> None:
        client.post(
            "/api/speech/synthesize",
            headers=auth_headers,
            json={"text": "| Name | Cost |\n|---|---|\n| alpha | 5 |\n| beta | 12 |"},
        )
        _, kwargs = transport.invocations[-1]
        assert kwargs["input"] == "Columns: Name, Cost.\nalpha, 5.\nbeta, 12."

    def test_markup_only_text_falls_back_to_the_source(
        self, client, auth_headers, controller, transport
    ) -> None:
        """Empty input is one more thing the gateway answers with an opaque 500.

        A document that is entirely markup verbalizes to nothing, and sending
        that would produce a failure naming no field. Speaking the source is the
        honest fallback — the caller asked for audio and gets audio.
        """
        response = client.post(
            "/api/speech/synthesize", headers=auth_headers, json={"text": "---"}
        )
        assert response.status_code == 200
        _, kwargs = transport.invocations[-1]
        assert kwargs["input"] == "---"

    def test_an_unknown_body_key_is_still_refused(
        self, client, auth_headers, controller, transport
    ) -> None:
        """``extra="forbid"`` must still hold with a new field in the model."""
        response = client.post(
            "/api/speech/synthesize",
            headers=auth_headers,
            json={"text": "hi", "verbalise": True},
        )
        assert response.status_code == 400
        assert "verbalise" in envelope(response)["reason"]
        assert not transport.invocations

    def test_text_that_expands_past_the_ceiling_is_refused_naming_the_opt_out(
        self, client, auth_headers, controller, transport
    ) -> None:
        """Verbalization can LENGTHEN text; the ceiling is what keeps the bound true.

        Each empty fence pair is three characters that become a nineteen-
        character sentence. The refusal must name ``verbalize=false`` rather than
        the published ``max_text_chars``: the caller respected that cap, and
        telling them to shorten already-short text sends them nowhere.
        """
        response = client.post(
            "/api/speech/synthesize",
            headers=auth_headers,
            json={"text": "```\n```\n" * 220},
        )
        assert response.status_code == 400
        reason = envelope(response)["reason"]
        assert "verbalize=false" in reason
        assert not transport.invocations, "an over-long text reached the gateway"

    def test_the_same_text_passes_with_verbalization_off(
        self, client, auth_headers, controller, transport
    ) -> None:
        """The opt-out the refusal names has to actually work."""
        response = client.post(
            "/api/speech/synthesize",
            headers=auth_headers,
            json={"text": "```\n```\n" * 220, "verbalize": False},
        )
        assert response.status_code == 200
        assert transport.invocations

    def test_capabilities_advertises_that_the_server_verbalizes(
        self, client, auth_headers, controller, transport
    ) -> None:
        """A client cannot drop its own stripper without being told this is here."""
        body = json.loads(
            client.get("/api/speech/capabilities", headers=auth_headers).data
        )
        assert body["synthesis"]["verbalizes_markdown"] is True


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------


class TestTranscribe:
    """Multipart in, text out — and the upload cap checked twice."""

    def test_returns_the_transcript_and_the_model(
        self, client, auth_headers, controller, transport
    ) -> None:
        import io

        transport.invoke_result = {"text": "the build finished"}
        response = client.post(
            "/api/speech/transcribe",
            headers=auth_headers,
            data={"file": (io.BytesIO(WAV_BYTES), "clip.wav")},
            content_type="multipart/form-data",
        )
        assert response.status_code == 200
        assert response.get_json() == {"text": "the build finished", "model": "nova-3"}
        operation, kwargs = transport.invocations[-1]
        assert operation == "atranscription"
        assert kwargs["model"] == "openai/nova-3"
        assert kwargs["file"] == ("clip.wav", WAV_BYTES)

    @pytest.mark.parametrize(
        ("filename", "mimetype", "expected"),
        [
            ("recording.webm", "audio/webm;codecs=opus", "recording.webm"),
            ("clip.wav", "audio/webm", "clip.wav"),
            # `mimetypes.guess_extension` answers None for BOTH of these, so a
            # stdlib-only fallback would label a browser recording `audio.wav`
            # and tell the gateway the wrong container.
            ("blob", "audio/webm", "audio.webm"),
            ("blob", "audio/webm;codecs=opus", "audio.webm"),
            ("blob", "audio/wav", "audio.wav"),
            ("blob", "audio/ogg", "audio.ogg"),
            ("", "", "audio.wav"),
            ("clip.", "", "audio.wav"),
            # A path never survives: only the basename can reach the outbound
            # multipart field, and an extensionless one falls through entirely.
            ("../../etc/passwd.wav", "", "passwd.wav"),
            ("../../etc/passwd", "", "audio.wav"),
            (r"C:\Users\me\clip.wav", "", "clip.wav"),
        ],
    )
    def test_the_format_hint_comes_from_the_extension_then_the_mimetype(
        self, controller, filename, mimetype, expected
    ) -> None:
        """The declared type is the FALLBACK; a browser codec string is not an extension."""
        assert controller.format_hint(filename, mimetype) == expected

    def test_missing_file_part_is_a_400_naming_it(
        self, client, auth_headers, controller, transport
    ) -> None:
        response = client.post(
            "/api/speech/transcribe",
            headers=auth_headers,
            data={},
            content_type="multipart/form-data",
        )
        assert response.status_code == 400
        error = envelope(response)
        assert error["code"] == "invalid_request"
        assert "file" in error["reason"]

    def test_declared_length_over_the_cap_is_refused_before_buffering(
        self, client, auth_headers, controller, transport
    ) -> None:
        """``Content-Length`` is checked first, so nothing is read into memory.

        The cap is lowered so the multipart ENVELOPE exceeds it while the file
        part alone stays under. That is what makes the assertion discriminating:
        a 413 here can only have come from the declared-length pre-check, since
        the real byte count (100) is inside the limit (200) and the second check
        would have passed.
        """
        import io

        controller.MAX_AUDIO_BYTES = 200
        try:
            response = client.post(
                "/api/speech/transcribe",
                headers=auth_headers,
                data={"file": (io.BytesIO(b"\x00" * 100), "clip.wav")},
                content_type="multipart/form-data",
            )
        finally:
            del controller.MAX_AUDIO_BYTES
        assert response.status_code == 413
        error = envelope(response)
        assert error["code"] == "audio_too_large"
        assert error["retryable"] is False
        assert "200" in error["reason"]
        assert transport.invocations == []

    def test_real_byte_count_over_the_cap_is_refused_too(
        self, client, auth_headers, controller, transport
    ) -> None:
        """The second check is the one that cannot be lied about.

        A chunked upload arrives with no ``Content-Length`` at all, so the
        pre-check has nothing to read and the byte count is the only bound left.
        """
        from mewbo_api.speech.routes import AudioTooLarge

        controller.ensure_within_size_limit(None)
        controller.ensure_within_size_limit(controller.MAX_AUDIO_BYTES)
        with pytest.raises(AudioTooLarge):
            controller.ensure_within_size_limit(controller.MAX_AUDIO_BYTES + 1)

    def test_unconfigured_direction_refuses_with_503(
        self, client, auth_headers, controller, transport
    ) -> None:
        import io

        controller.config_reader = lambda: speech_config(stt={"model": ""})
        response = client.post(
            "/api/speech/transcribe",
            headers=auth_headers,
            data={"file": (io.BytesIO(WAV_BYTES), "clip.wav")},
            content_type="multipart/form-data",
        )
        assert response.status_code == 503
        assert envelope(response)["code"] == "speech_unavailable"
        assert transport.invocations == []

    def test_requires_a_credential(self, client) -> None:
        assert client.post("/api/speech/transcribe").status_code == 401


# ---------------------------------------------------------------------------
# The shared in-flight bound
# ---------------------------------------------------------------------------


class TestConcurrencyBound:
    """Four in flight across both routes; the fifth caller is refused, not queued."""

    def test_the_bound_is_shared_and_the_overflow_is_a_retryable_503(
        self, client, auth_headers, controller
    ) -> None:
        # Hold every slot without a thread: the bound is arithmetic over a
        # counter, so occupying it directly tests the admission decision rather
        # than a race between workers.
        for _ in range(controller.MAX_CONCURRENT_CALLS):
            assert controller.capacity.try_acquire() is True
        try:
            for path, kwargs in (
                ("/api/speech/synthesize", {"json": {"text": "hi"}}),
                (
                    "/api/speech/transcribe",
                    {"data": {"file": (__import__("io").BytesIO(WAV_BYTES), "c.wav")}},
                ),
            ):
                response = client.post(path, headers=auth_headers, **kwargs)
                assert response.status_code == 503, path
                error = envelope(response)
                assert error["code"] == "speech_capacity_exhausted"
                assert error["retryable"] is True
                assert str(controller.MAX_CONCURRENT_CALLS) in error["reason"]
                assert response.headers["Retry-After"] == "5"
        finally:
            for _ in range(controller.MAX_CONCURRENT_CALLS):
                controller.capacity.release()

    def test_a_slot_is_returned_after_a_successful_call(
        self, client, auth_headers, controller, transport
    ) -> None:
        before = controller.capacity.active
        client.post("/api/speech/synthesize", headers=auth_headers, json={"text": "hi"})
        assert controller.capacity.active == before

    def test_a_slot_is_returned_after_a_failed_call(
        self, client, auth_headers, controller, transport
    ) -> None:
        """A leaked slot would shut the bound under exactly the churn it survives."""
        transport.invoke_error = SpeechGatewayError("boom")
        before = controller.capacity.active
        response = client.post("/api/speech/synthesize", headers=auth_headers, json={"text": "hi"})
        assert response.status_code == 502
        assert controller.capacity.active == before
