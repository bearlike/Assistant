"""Contract tests for the speech request/result union and the container sniffer.

Every rule asserted here was measured against the deployed gateway, and every one
of them exists to turn a failure the gateway reports opaquely into a validation
error raised before the call. The gateway answers a missing voice, a bogus voice,
and an unsupported container with the SAME byte-identical HTTP 500 whose body is
the literal string "Internal server error" — so if these validators regress, the
symptom is not a clear error, it is an undiagnosable one.
"""

import pytest
from mewbo_speech import (
    DEFAULT_STT_MODEL,
    DEFAULT_TTS_MODEL,
    SUGGESTED_VOICES,
    AudioContainer,
    SpeechMode,
    SpeechModel,
    SpeechOperation,
    SynthesisRequest,
    TranscriptionRequest,
    parse_speech_request,
)
from pydantic import ValidationError

# The leading bytes of a real ``supertonic-3`` response: RIFF/WAVE, 16-bit mono
# 44.1 kHz, served under a ``Content-Type: audio/mpeg`` header that describes
# none of it.
WAV_HEAD = b"RIFF\x24\x08\x00\x00WAVEfmt "


class TestVoiceValidation:
    """``voice`` is required, and the accepted set is ours to state."""

    def test_missing_voice_is_rejected_and_suggests_the_common_ones(self):
        with pytest.raises(ValidationError) as excinfo:
            SynthesisRequest(model="supertonic-3", text="hello")
        message = str(excinfo.value)
        assert "voice is required" in message
        # The gateway enumerates nothing in its own error, so ours must at least
        # suggest something — while saying the set is not closed.
        for voice in SUGGESTED_VOICES:
            assert voice in message

    @pytest.mark.parametrize("voice", SUGGESTED_VOICES)
    def test_every_measured_voice_is_accepted(self, voice):
        request = SynthesisRequest(model="supertonic-3", text="hello", voice=voice)
        assert request.voice == voice

    def test_an_operator_defined_voice_is_accepted(self):
        """A self-hosted backend's own voice style must reach the gateway.

        The suggestion list holds OpenAI's canonical names, and a deployment
        that trained its own style has a name no list here can predict. This
        once raised, refusing a voice that works while asserting it knew the
        accepted set — the failure this whole validator was relaxed for.
        """
        request = SynthesisRequest(model="supertonic-3", text="hello", voice="Boss")
        assert request.voice == "Boss"

    def test_voice_case_is_preserved(self):
        """Only surrounding whitespace is stripped.

        Lower-casing presumed the names were OpenAI's. An operator-defined style
        is free to be capitalised, and folding its case sends the gateway a name
        it may not recognise.
        """
        request = SynthesisRequest(model="supertonic-3", text="hi", voice="  Boss ")
        assert request.voice == "Boss"

    def test_a_name_the_gateway_rejects_is_no_longer_refused_here(self):
        """The gateway owns which names are real, so these now reach it.

        `male`/`female`/`default` each returned a 500 from the deployed backend,
        and this file previously pinned them as locally refused. That pinning is
        what made a working operator-defined voice impossible: a validator with
        no way to ask cannot tell an unsupported name from an unfamiliar one.
        Their 500 is a worse error message than a local refusal and the correct
        trade — a wrong rejection loses a capability outright.
        """
        for voice in ("male", "female", "en_male", "en_female", "default"):
            assert SynthesisRequest(model="supertonic-3", text="hi", voice=voice).voice == voice


class TestModelIdIsBare:
    """A model id is the BARE gateway id — a prefix on the wire is a 403."""

    @pytest.mark.parametrize(
        "prefixed",
        ["openai/supertonic-3", "deepgram/nova-3", "openai/nova-3"],
    )
    def test_a_provider_prefixed_id_is_refused(self, prefixed):
        with pytest.raises(ValidationError) as excinfo:
            SynthesisRequest(model=prefixed, text="hi", voice="alloy")
        message = str(excinfo.value)
        assert "bare gateway id" in message
        # The message names the fix, not just the fault.
        assert prefixed.rsplit("/", 1)[-1] in message

    def test_a_bare_id_survives_verbatim(self):
        request = SynthesisRequest(model="supertonic-3", text="hi", voice="alloy")
        assert request.model == "supertonic-3"

    def test_the_route_prefix_is_applied_only_at_the_sdk_call(self):
        request = SynthesisRequest(model="supertonic-3", text="hi", voice="alloy")
        kwargs = request.litellm_kwargs("openai")
        # The SDK argument carries the prefix (it refuses a bare id locally with
        # "LLM Provider NOT provided"), and strips it before the wire.
        assert kwargs["model"] == "openai/supertonic-3"
        # The id the contract holds — and that a raw REST caller must send — does
        # not.
        assert request.model == "supertonic-3"

    def test_transcription_follows_the_same_rule_as_synthesis(self):
        # `deepgram/nova-3` routes the SDK to Deepgram's own API and 404s against
        # the proxy; `openai/nova-3` is the correct SDK form for BOTH modes.
        request = TranscriptionRequest(model="nova-3", audio=b"\x00\x01")
        assert request.litellm_kwargs("openai")["model"] == "openai/nova-3"


class TestSynthesisKwargs:
    """The SDK argument shape, including what is deliberately NOT sent."""

    def test_response_format_is_omitted_when_unset(self):
        request = SynthesisRequest(model="supertonic-3", text="hi", voice="alloy")
        assert "response_format" not in request.litellm_kwargs("openai")

    def test_supported_formats_are_forwarded(self):
        request = SynthesisRequest(
            model="supertonic-3", text="hi", voice="alloy", audio_format=AudioContainer.FLAC
        )
        assert request.litellm_kwargs("openai")["response_format"] == "flac"

    @pytest.mark.parametrize("bad", [AudioContainer.MP3, AudioContainer.OGG, AudioContainer.MP4])
    def test_formats_the_backend_500s_on_are_refused(self, bad):
        with pytest.raises(ValidationError) as excinfo:
            SynthesisRequest(model="supertonic-3", text="hi", voice="alloy", audio_format=bad)
        assert "unsupported synthesis format" in str(excinfo.value)

    def test_blank_text_is_refused(self):
        with pytest.raises(ValidationError):
            SynthesisRequest(model="supertonic-3", text="", voice="alloy")


class TestTranscriptionKwargs:
    """The multipart shape, which never touches the filesystem."""

    def test_file_is_a_name_and_bytes_tuple(self):
        request = TranscriptionRequest(
            model="nova-3", audio=b"RIFFxxxxWAVE", filename="turn.webm"
        )
        kwargs = request.litellm_kwargs("openai")
        assert kwargs["file"] == ("turn.webm", b"RIFFxxxxWAVE")

    def test_language_is_omitted_unless_given(self):
        assert "language" not in TranscriptionRequest(
            model="nova-3", audio=b"\x00"
        ).litellm_kwargs("openai")
        assert (
            TranscriptionRequest(model="nova-3", audio=b"\x00", language="en").litellm_kwargs(
                "openai"
            )["language"]
            == "en"
        )

    def test_empty_audio_is_refused(self):
        with pytest.raises(ValidationError):
            TranscriptionRequest(model="nova-3", audio=b"")


class TestUnionParseSeam:
    """One discriminated union, one parse seam, and `extra="forbid"` on it."""

    def test_the_discriminator_selects_the_variant(self):
        synthesis = parse_speech_request(
            {"kind": "synthesis", "model": "supertonic-3", "text": "hi", "voice": "alloy"}
        )
        transcription = parse_speech_request(
            {"kind": "transcription", "model": "nova-3", "audio": b"\x00"}
        )
        assert isinstance(synthesis, SynthesisRequest)
        assert isinstance(transcription, TranscriptionRequest)

    def test_each_variant_carries_its_own_required_mode_and_operation(self):
        assert SynthesisRequest.REQUIRED_MODE is SpeechMode.SYNTHESIS
        assert SynthesisRequest.SDK_OPERATION == "aspeech"
        assert TranscriptionRequest.REQUIRED_MODE is SpeechMode.TRANSCRIPTION
        assert TranscriptionRequest.SDK_OPERATION == "atranscription"

    def test_an_unknown_field_is_a_clean_rejection(self):
        with pytest.raises(ValidationError):
            parse_speech_request(
                {
                    "kind": "synthesis",
                    "model": "supertonic-3",
                    "text": "hi",
                    "voice": "alloy",
                    "speed": 2.0,
                }
            )

    def test_an_unknown_kind_is_refused(self):
        with pytest.raises(ValidationError):
            parse_speech_request({"kind": "diarisation", "model": "nova-3"})


class TestModeClassification:
    """`model_info.mode` is what splits TTS from STT — never the model's name."""

    def test_a_speech_model_is_classified_by_its_declared_mode(self):
        tts = SpeechModel.from_model_info(
            {"model_name": "supertonic-3", "model_info": {"mode": "audio_speech"}}
        )
        stt = SpeechModel.from_model_info(
            {"model_name": "nova-3", "model_info": {"mode": "audio_transcription"}}
        )
        assert tts is not None and tts.mode is SpeechMode.SYNTHESIS
        assert stt is not None and stt.mode is SpeechMode.TRANSCRIPTION

    @pytest.mark.parametrize("mode", ["chat", "embedding", "rerank", None])
    def test_a_non_speech_route_classifies_to_none(self, mode):
        entry = {"model_name": "claude-opus-5", "model_info": {"mode": mode}}
        assert SpeechModel.from_model_info(entry) is None

    def test_the_id_is_model_name_not_the_operator_facing_key(self):
        # `model_info.key` reads `deepgram/nova-3` and describes which upstream
        # the proxy's route consumes; sending it is a 403.
        model = SpeechModel.from_model_info(
            {
                "model_name": "nova-3",
                "model_info": {"mode": "audio_transcription", "key": "deepgram/nova-3"},
            }
        )
        assert model is not None
        assert model.id == "nova-3"

    def test_a_malformed_entry_is_skipped_rather_than_raising(self):
        # A listing that raised on one bad row would report zero speech models on
        # an otherwise healthy gateway.
        assert SpeechModel.from_model_info({}) is None
        assert SpeechModel.from_model_info({"model_name": "x"}) is None
        assert SpeechModel.from_model_info({"model_name": "", "model_info": {}}) is None

    def test_display_name_falls_back_to_the_id(self):
        assert SpeechModel(id="supertonic-3", mode=SpeechMode.SYNTHESIS).display_name == (
            "supertonic-3"
        )


class TestContainerSniffing:
    """The declared Content-Type is wrong on every successful synthesis."""

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            (WAV_HEAD, AudioContainer.WAV),
            (b"fLaC\x00\x00\x00\x22", AudioContainer.FLAC),
            (b"OggS\x00\x02\x00\x00", AudioContainer.OGG),
            (b"ID3\x04\x00\x00\x00\x00", AudioContainer.MP3),
            (b"\xff\xfb\x90\x64", AudioContainer.MP3),
            (b"\x00\x00\x00\x20ftypM4A ", AudioContainer.MP4),
            (b"not audio at all", AudioContainer.UNKNOWN),
            (b"", AudioContainer.UNKNOWN),
            (b"RIFF\x24\x08\x00\x00AVI ", AudioContainer.UNKNOWN),
        ],
    )
    def test_sniff_identifies_the_real_container(self, payload, expected):
        assert AudioContainer.sniff(payload) is expected

    def test_the_wav_payload_is_labelled_wav_not_the_declared_mpeg(self):
        request = SynthesisRequest(model="supertonic-3", text="hi", voice="alloy")
        result = request.parse_response({"audio": WAV_HEAD, "content_type": "audio/mpeg"})
        assert result.container is AudioContainer.WAV
        assert result.content_type == "audio/wav"
        assert result.declared_content_type == "audio/mpeg"
        assert result.declared_type_was_wrong is True

    def test_a_matching_declared_type_is_not_flagged(self):
        request = SynthesisRequest(model="supertonic-3", text="hi", voice="alloy")
        result = request.parse_response(
            {"audio": WAV_HEAD, "content_type": "audio/wav; charset=binary"}
        )
        assert result.declared_type_was_wrong is False

    def test_an_absent_declared_type_is_not_a_mismatch(self):
        request = SynthesisRequest(model="supertonic-3", text="hi", voice="alloy")
        result = request.parse_response({"audio": WAV_HEAD})
        assert result.declared_content_type is None
        assert result.declared_type_was_wrong is False

    def test_a_synthesis_payload_without_audio_is_an_error(self):
        request = SynthesisRequest(model="supertonic-3", text="hi", voice="alloy")
        with pytest.raises(ValueError, match="no audio bytes"):
            request.parse_response({"text": "surprise"})

    def test_a_transcription_payload_without_text_is_an_error(self):
        request = TranscriptionRequest(model="nova-3", audio=b"\x00")
        with pytest.raises(ValueError, match="no text"):
            request.parse_response({"audio": WAV_HEAD})

    @pytest.mark.parametrize(
        "buffer", [WAV_HEAD, bytearray(WAV_HEAD), memoryview(WAV_HEAD)]
    )
    def test_sniff_accepts_any_buffer(self, buffer):
        # Audio reaches this from an SDK response, a multipart upload and a
        # fixture; narrowing to `bytes` would push a copy onto each call site.
        assert AudioContainer.sniff(buffer) is AudioContainer.WAV

    def test_a_bytearray_payload_still_produces_bytes_on_the_result(self):
        request = SynthesisRequest(model="supertonic-3", text="hi", voice="alloy")
        result = request.parse_response({"audio": bytearray(WAV_HEAD)})
        assert isinstance(result.audio, bytes)
        assert result.container is AudioContainer.WAV


class TestOwnerDecisions:
    """Choices the owner locked, pinned so a later edit has to be deliberate."""

    def test_the_default_tts_model_is_the_plain_route_not_hd(self):
        # -hd measured ~2x slower for byte-identical output at the same voice
        # and format, so it is selectable but never the default.
        assert DEFAULT_TTS_MODEL == "supertonic-3"
        assert SynthesisRequest(model=DEFAULT_TTS_MODEL, text="hi", voice="alloy")

    def test_the_default_stt_model_is_accepted_by_the_contract(self):
        assert DEFAULT_STT_MODEL == "nova-3"
        assert TranscriptionRequest(model=DEFAULT_STT_MODEL, audio=b"\x00")

    def test_the_operation_base_cannot_be_instantiated(self):
        # Abstract rather than stubs that raise, so a variant forgetting one
        # fails at construction instead of at call time.
        with pytest.raises(TypeError):
            # A static checker refusing this IS the property under test; the
            # suppression is pyright-specific so mypy's warn_unused_ignores does
            # not then flag an unused `type: ignore` it never needed.
            SpeechOperation(model="supertonic-3")  # pyright: ignore[reportAbstractUsage]
