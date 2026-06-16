"""Tests for :class:`RelatedQuestionsRunner` — the parallel follow-up generator.

The runner reuses the core ``StructuredSynthesizer`` (mocked here at the
synthesize seam — NEVER a real LLM, per the SCG no-real-LLM rule). The contract:
one structured round-trip → a deduped, capped list of follow-up strings, and a
best-effort guarantee that ANY failure degrades to ``[]`` rather than raising.
"""

from mewbo_api.agentic_search.scg.related_questions import RelatedQuestionsRunner


class _FakeSynth:
    """A ``StructuredSynthesizer`` stand-in whose ``synthesize`` is canned."""

    def __init__(self, payload=None, *, raises=None):
        self._payload = payload
        self._raises = raises
        self.calls: list[tuple[str, dict]] = []

    async def synthesize(self, query, schema, *, workspace=None, k=8):
        self.calls.append((query, schema))
        if self._raises is not None:
            raise self._raises
        return self._payload, []


def test_projects_dedupes_and_caps():
    """Strings only, blank/duplicate dropped (case-insensitive), capped at max."""
    synth = _FakeSynth(
        {"related_questions": ["How is auth wired?", "how is auth wired?", "Rate limits?", "", 7]}
    )
    out = RelatedQuestionsRunner(max_questions=5, synthesizer=synth).run("q", "an answer")
    assert out == ["How is auth wired?", "Rate limits?"]


def test_caps_at_max_questions():
    synth = _FakeSynth({"related_questions": ["a?", "b?", "c?", "d?"]})
    out = RelatedQuestionsRunner(max_questions=2, synthesizer=synth).run("q", "ans")
    assert out == ["a?", "b?"]


def test_empty_inputs_skip_the_call():
    """No query OR no answer → ``[]`` without ever invoking the synthesizer."""
    synth = _FakeSynth({"related_questions": ["x?"]})
    assert RelatedQuestionsRunner(synthesizer=synth).run("", "ans") == []
    assert RelatedQuestionsRunner(synthesizer=synth).run("q", "   ") == []
    assert synth.calls == []


def test_synthesizer_failure_degrades_to_empty():
    """A raising synthesize never propagates — a follow-up list is best-effort."""
    synth = _FakeSynth(raises=RuntimeError("no LLM"))
    assert RelatedQuestionsRunner(synthesizer=synth).run("q", "ans") == []


def test_malformed_payload_is_empty():
    """A payload missing/!list ``related_questions`` yields ``[]`` (not a crash)."""
    assert RelatedQuestionsRunner(synthesizer=_FakeSynth({})).run("q", "a") == []
    assert (
        RelatedQuestionsRunner(synthesizer=_FakeSynth({"related_questions": "nope"})).run(
            "q", "a"
        )
        == []
    )


def test_answer_is_capped_in_the_prompt():
    """A long answer is truncated before it reaches the (cheap) follow-up call."""
    synth = _FakeSynth({"related_questions": ["ok?"]})
    long_answer = "x" * 5000
    RelatedQuestionsRunner(synthesizer=synth).run("the query", long_answer)
    (prompt, _schema) = synth.calls[0]
    assert "the query" in prompt
    # The answer body is capped — 2000 consecutive x's land, never the full 5000.
    assert "x" * 2000 in prompt
    assert "x" * 2001 not in prompt
