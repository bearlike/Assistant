"""The ``speech`` config section: defaults, refusals, and the accessor path.

The section names which gateway model reads an answer aloud and which one
transcribes a recording. Three of its four knobs are refused AT DEFINITION
rather than at the gateway, and that is the point of the section: the gateway
answers an unusable ``voice`` or ``response_format`` with a bare HTTP 500 whose
body names no field, so a mistake made here is otherwise discovered by a user
pressing play and getting nothing.

What the cases below are actually guarding:

* **absent section** — most installs will never write a ``speech`` block, so
  every field has to resolve from the model's own defaults. A section that only
  worked once someone declared it would be a section nobody has.
* **the refusals** — each asserts the MESSAGE names the accepted set, not merely
  that something was raised. A refusal a user cannot act on sends them to the
  gateway's opaque 500 by a longer route.
* **the accessor** — ``get_config_value`` returns its ``default`` for a key that
  does not exist, so a test asserting only "it returned the right value" passes
  identically whether the typed field is wired or missing. The negative control
  beside it is what makes the positive mean anything.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest
from mewbo_core.config import (
    AppConfig,
    SpeechConfig,
    SpeechTtsConfig,
    get_config_value,
    reset_config,
    set_config_override,
)
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]
FACETS_TS = (
    REPO_ROOT / "apps" / "mewbo_console" / "src" / "components" / "settings" / "facets.ts"
)


class TestDefaultsWithNoSectionDeclared:
    """An ``app.json`` with no ``speech`` block still yields a usable section."""

    def test_absent_section_resolves_every_field(self):
        speech = AppConfig.model_validate({}).speech
        assert speech.tts.model == "supertonic-3"
        assert speech.tts.voice == "nova"
        assert speech.tts.response_format == "wav"
        assert speech.stt.model == "nova-3"
        # Blank on purpose: these three are what let a deployment point speech
        # at a different gateway, and blank is what keeps the llm fallback live.
        assert speech.api_base == ""
        assert speech.api_key == ""
        assert speech.timeout == 90.0

    def test_half_declared_section_fills_the_rest(self):
        # A user editing one knob in the Settings pane sends only that subtree.
        speech = AppConfig.model_validate({"speech": {"tts": {"voice": "sage"}}}).speech
        assert speech.tts.voice == "sage"
        assert speech.tts.model == "supertonic-3"
        assert speech.stt.model == "nova-3"


class TestTheSectionRoundTrips:
    """A fully declared section survives validate → dump unchanged."""

    def test_valid_section_round_trips(self):
        payload = {
            "api_base": "https://speech.example.com/v1",
            "api_key": "sk-speech",
            "timeout": 30.0,
            "tts": {"model": "supertonic-3-hd", "voice": "coral", "response_format": "flac"},
            "stt": {"model": "nova-3"},
        }
        assert SpeechConfig.model_validate(payload).model_dump() == payload

    def test_padding_is_trimmed_and_voice_case_is_preserved(self):
        """Case survives, because a custom voice style may be capitalised.

        Folding it was safe only while the accepted set was OpenAI's eleven
        lowercase names. A self-hosted style is the operator's own string, and
        lowercasing it sends the gateway a name it need not recognise.
        """
        speech = SpeechConfig.model_validate(
            {"tts": {"model": "  supertonic-3  ", "voice": "  Boss  "}}
        )
        assert speech.tts.model == "supertonic-3"
        assert speech.tts.voice == "Boss"

    def test_a_pasted_key_or_url_is_trimmed(self):
        # Whitespace does not make a value blank, so an untrimmed paste beats
        # the llm fallback and is then sent verbatim to the gateway.
        speech = SpeechConfig.model_validate(
            {"api_base": "  https://speech.example.com/v1\n", "api_key": " sk-speech\n"}
        )
        assert speech.api_base == "https://speech.example.com/v1"
        assert speech.api_key == "sk-speech"

    def test_a_whitespace_only_key_collapses_to_blank_so_the_fallback_still_fires(self):
        speech = SpeechConfig.model_validate({"api_base": "   ", "api_key": "  "})
        assert speech.api_base == ""
        assert speech.api_key == ""

    def test_an_empty_model_is_allowed_and_means_the_leg_is_off(self):
        # There is no separate enable switch: an empty model id is how a
        # deployment whose gateway has no speech model turns the surface off.
        speech = SpeechConfig.model_validate({"tts": {"model": ""}, "stt": {"model": ""}})
        assert speech.tts.model == ""
        assert speech.stt.model == ""


class TestRefusalsNameWhatIsAccepted:
    """Each refusal has to tell the operator what to write instead."""

    def test_an_operator_defined_voice_is_accepted(self):
        """A self-hosted backend's own voice style must be configurable.

        This field was a closed enum of OpenAI's eleven names, which made a
        deployment's own trained style unconfigurable — the operator could not
        write it down, and the API refused it if they sent it per request. The
        gateway owns which voices exist.
        """
        speech = SpeechConfig.model_validate({"tts": {"voice": "boss"}})
        assert speech.tts.voice == "boss"

    def test_an_empty_voice_falls_back_to_the_default(self):
        # Omitting the voice is an HTTP 500 at the gateway, so a blank config
        # value must resolve to something rather than travel as empty.
        assert SpeechConfig.model_validate({"tts": {"voice": ""}}).tts.voice == "nova"

    @pytest.mark.parametrize("fmt", ["mp3", "opus", "aac", "pcm"])
    def test_a_format_the_gateway_rejects_is_refused_here(self, fmt):
        with pytest.raises(ValidationError) as exc:
            SpeechConfig.model_validate({"tts": {"response_format": fmt}})
        message = str(exc.value)
        assert "'wav'" in message and "'flac'" in message

    def test_an_unknown_key_is_refused_rather_than_dropped(self):
        # The sub-models are extra="forbid" on purpose: AppConfig itself is
        # extra="ignore", so a typo inside the section is the one that would
        # otherwise vanish without a word.
        with pytest.raises(ValidationError):
            SpeechConfig.model_validate({"tts": {"voice_name": "nova"}})


class TestTheAccessorPathIsReal:
    """``get_config_value("speech", …)`` reaches the typed fields.

    ``get_config_value`` walks one ``getattr`` per key and returns ``default``
    the moment one is missing, so it cannot distinguish "the field holds the
    default" from "the field does not exist". Every positive below is paired
    with a control that fails if the walk is silently falling through.
    """

    @pytest.fixture(autouse=True)
    def _clean_override(self):
        yield
        reset_config()

    def test_defaults_are_reachable_through_the_accessor(self):
        assert get_config_value("speech", "tts", "model") == "supertonic-3"
        assert get_config_value("speech", "tts", "voice") == "nova"
        assert get_config_value("speech", "tts", "response_format") == "wav"
        assert get_config_value("speech", "stt", "model") == "nova-3"

    def test_the_three_keys_the_package_reads_are_reachable(self):
        # The exact reads in mewbo_speech.gateway.SpeechGateway.from_config.
        # api_base/api_key resolve to "" (falsy, so its `or llm.*` fallback
        # still fires); timeout has no fallback, so this is the whole contract.
        assert get_config_value("speech", "api_base", default="MISSING") == ""
        assert get_config_value("speech", "api_key", default="MISSING") == ""
        assert get_config_value("speech", "timeout", default="MISSING") == 90.0

    def test_an_overridden_gateway_is_read_back_through_the_accessor(self):
        set_config_override(
            {"speech": {"api_base": "https://speech.example.com/v1", "timeout": 20.0}}
        )
        assert get_config_value("speech", "api_base") == "https://speech.example.com/v1"
        assert get_config_value("speech", "timeout") == 20.0

    def test_the_accessor_is_not_merely_returning_its_default(self):
        # The control: a key that genuinely does not exist DOES fall through,
        # which is what proves the assertions above walked real attributes.
        sentinel = object()
        assert get_config_value("speech", "tts", "sample_rate", default=sentinel) is sentinel
        assert get_config_value("speech", "nonexistent", default=sentinel) is sentinel

    def test_an_override_is_read_back_through_the_accessor(self):
        set_config_override({"speech": {"tts": {"model": "supertonic-3-hd", "voice": "ash"}}})
        assert get_config_value("speech", "tts", "model") == "supertonic-3-hd"
        assert get_config_value("speech", "tts", "voice") == "ash"
        # Untouched siblings still resolve rather than disappearing.
        assert get_config_value("speech", "stt", "model") == "nova-3"


class TestTheCurationMetadata:
    """The facet annotation and its console counterpart.

    A section whose ``x-group`` the console does not declare is bucketed into
    the "Other" fallback facet with no error and no warning, so the only thing
    standing between a shipped section and an invisible one is this pair
    agreeing. ``speech`` reuses the existing ``models`` facet rather than
    minting a new id, which is what makes that agreement cheap.
    """

    def test_the_section_declares_its_facet_on_the_class(self):
        # On the class, never the field: a submodel field serializes to a bare
        # $ref and Pydantic drops sibling json_schema_extra.
        definition = AppConfig.model_json_schema()["$defs"]["SpeechConfig"]
        assert definition["x-group"] == "models"
        assert definition["x-order"] == 5
        assert definition["title"] == "Speech"

    def test_the_facet_id_exists_in_the_console_union(self):
        source = FACETS_TS.read_text(encoding="utf-8")
        match = re.search(r"export type FacetId =([^;]+);", source)
        assert match, (
            f"could not find the FacetId union in {FACETS_TS}. This test is the "
            "lockstep guard between core's x-group and the console's facet list; "
            "if the union moved, re-point it rather than deleting it."
        )
        declared = set(re.findall(r'"([a-z_]+)"', match.group(1)))
        assert "models" in declared, (
            "the console no longer declares the 'models' facet, so the speech "
            "section now vanishes into the 'Other' fallback facet"
        )

    def test_the_section_is_reachable_from_the_top_level_schema(self):
        # AppConfig is extra="ignore", so the typed field is the only thing
        # that makes a speech block in app.json anything other than discarded.
        assert "speech" in AppConfig.model_json_schema()["properties"]

    def test_the_gateway_key_is_marked_secret_exactly_as_the_llm_key_is(self):
        # Not "has some marking": the SAME marking, because it is that flag
        # that makes ConfigSchemaView strip the value from GET /api/config and
        # route it through resolve_secret_writes on PATCH. A speech key without
        # it is a credential returned in an API response.
        defs = AppConfig.model_json_schema()["$defs"]
        speech_key = defs["SpeechConfig"]["properties"]["api_key"]
        llm_key = defs["LLMConfig"]["properties"]["api_key"]
        assert speech_key.get("x-secret") is True
        assert speech_key.get("x-secret") == llm_key.get("x-secret")

    def test_the_secret_value_never_survives_a_dump(self):
        # The property, not the annotation: a marking nothing enforces is a
        # comment. Driven through the API's own view, which is what serves
        # GET /api/config.
        from mewbo_api.config_view import ConfigSchemaView

        loaded = AppConfig.model_validate({"speech": {"api_key": "sk-should-not-appear"}})
        stripped = ConfigSchemaView.from_model().strip_values(loaded.model_dump())
        assert "sk-should-not-appear" not in repr(stripped)
        assert "api_key" not in stripped["speech"]


class TestTheConnectionFieldsMatchWhatThePackageReads:
    """``mewbo_speech`` reads these three keys, and they must not drift.

    ``SpeechGateway.from_config`` reads ``speech.api_base``/``speech.api_key``
    with an ``or llm.*`` fallback, and ``speech.timeout`` with NO fallback. The
    tests below import the package's own constants rather than restating them:
    core cannot import UP into the package, so the literals are genuinely
    duplicated, and a duplication nothing compares is a divergence waiting to
    happen. A test may import both sides; production code may not.
    """

    def test_the_timeout_default_equals_the_packages_own_constant(self):
        from mewbo_speech import DEFAULT_SPEECH_TIMEOUT

        # This is the one that bites silently. Once the typed field exists, the
        # accessor returns ITS default and the package's `default=` argument
        # never runs again, so the two drifting apart changes the deployed
        # timeout while both files still read as correct.
        assert SpeechConfig.model_validate({}).timeout == DEFAULT_SPEECH_TIMEOUT

    def test_the_voice_field_is_open_not_an_enum(self):
        """No closed voice set anywhere — the gateway owns that fact.

        Kept as a guard rather than deleted with the enum it used to compare:
        re-introducing a `Literal[...]` here is the exact change that made a
        self-hosted backend's own voice style unconfigurable, and it would look
        like a tightening improvement to whoever wrote it.
        """
        assert SpeechTtsConfig.model_fields["voice"].annotation is str
        assert get_args(SpeechTtsConfig.model_fields["voice"].annotation) == ()

    def test_the_format_enum_holds_exactly_the_packages_formats(self):
        from mewbo_speech import SYNTHESIS_FORMATS

        declared = get_args(SpeechTtsConfig.model_fields["response_format"].annotation)
        assert declared == tuple(fmt.value for fmt in SYNTHESIS_FORMATS)

    def test_the_defaults_are_the_model_ids_the_package_defaults_to(self):
        from mewbo_speech.models import DEFAULT_STT_MODEL, DEFAULT_TTS_MODEL

        speech = SpeechConfig.model_validate({})
        assert speech.tts.model == DEFAULT_TTS_MODEL
        assert speech.stt.model == DEFAULT_STT_MODEL

    def test_blank_connection_fields_leave_the_fallback_to_llm_intact(self):
        # The package's fallback is `speech.api_base or llm.api_base`, so the
        # default must be FALSY. A non-empty placeholder default here would
        # silently capture every deployment that never configured speech.
        speech = SpeechConfig.model_validate({})
        assert speech.api_base == ""
        assert speech.api_key == ""

    def test_a_non_positive_timeout_is_refused(self):
        for bad in (0, -1.0):
            with pytest.raises(ValidationError):
                SpeechConfig.model_validate({"timeout": bad})
