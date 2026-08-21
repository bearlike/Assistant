"""Tests for :class:`mewbo_core.contracts.run_error.RunError`.

The load-bearing invariant under test: classification and the rendered
``title`` derive from the exception TYPE and the LiteLLM error-class NAME,
never from the response body. A provider that embeds an HTML error page in an
exception message must contribute nothing to ``title`` — such a page's own
``<title>`` names the operator's internal infrastructure, and this repository
public-mirrors, so the fixtures below use ``git.example.com`` throughout.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from mewbo_core.classes import OrchestrationState, TaskQueue
from mewbo_core.contracts.run_error import RunError
from mewbo_core.hooks import HookManager
from mewbo_core.loop.orchestrator import Orchestrator
from mewbo_core.loop.tool_use_loop import ToolUseLoop
from mewbo_core.session.session_store import SessionStore

# The shape LiteLLM produced on a real 502: an entire HTML document spliced
# into the exception message. Hostname is fictional on purpose.
_HTML_BODY = (
    '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
    "<title>502 Bad Gateway — proxy.git.example.com</title>\n"
    "</head>\n<body>\n<h1>502 Bad Gateway</h1>\n"
    "<p>The upstream server at internal-llm.git.example.com is unreachable.</p>\n"
    "</body>\n</html>\n" + ("<!-- padding -->\n" * 400)
)

_BAD_GATEWAY = (
    "LLM call failed on all models (claude-fable-5): "
    "litellm.BadGatewayError: BadGatewayError: OpenAIException - " + _HTML_BODY
)

_RATE_LIMITED = (
    "LLM call failed on all models (openai/claude-sonnet-4-6): "
    "litellm.RateLimitError: RateLimitError: OpenAIException - "
    "No deployments available for selected model,"
)


class _FakeExhausted(RuntimeError):
    """Stands in for ``LlmResilienceExhausted`` — same structural contract.

    Subclasses ``RuntimeError``, renders the same message, and carries the
    real provider exception on ``last_error`` plus the attempted models on
    ``models_tried``.
    """

    def __init__(self, message: str, last_error: BaseException, models: list[str]) -> None:
        super().__init__(message)
        self.last_error = last_error
        self.last_error_type = type(last_error).__name__
        self.models_tried = models


def _litellm_exc(class_name: str, message: str) -> BaseException:
    """Build an exception whose TYPE NAME matches a LiteLLM error class."""
    return type(class_name, (Exception,), {})(message)


class TestKindClassification:
    """Every kind must be reachable from a realistic message."""

    @pytest.mark.parametrize(
        ("class_name", "message", "expected"),
        [
            ("BadGatewayError", _BAD_GATEWAY, "upstream_bad_gateway"),
            ("RateLimitError", _RATE_LIMITED, "rate_limited"),
            ("Timeout", "litellm.Timeout: Request timed out after 600.0s", "timeout"),
            (
                "AuthenticationError",
                "litellm.AuthenticationError: invalid api key",
                "auth",
            ),
            (
                "ContextWindowExceededError",
                "litellm.ContextWindowExceededError: 210000 > 200000 tokens",
                "context_overflow",
            ),
            (
                "ServiceUnavailableError",
                "litellm.ServiceUnavailableError: upstream is down",
                "provider_unavailable",
            ),
            (
                "ToolInputError",
                "ERROR: file_edit_tool rejected the patch",
                "tool_failure",
            ),
        ],
    )
    def test_kind_from_exception_type(self, class_name: str, message: str, expected: str) -> None:
        """The exception's own type name drives the kind."""
        err = RunError.from_exception(_litellm_exc(class_name, message))
        assert err.kind == expected

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            (_BAD_GATEWAY, "upstream_bad_gateway"),
            (_RATE_LIMITED, "rate_limited"),
            ("litellm.Timeout: Request timed out after 600.0s", "timeout"),
            ("litellm.AuthenticationError: invalid api key", "auth"),
            ("litellm.ContextWindowExceededError: too many tokens", "context_overflow"),
            ("litellm.APIConnectionError: connection reset", "provider_unavailable"),
            ("ERROR: Tool 'shell_tool' timed out after 120.0s", "tool_failure"),
        ],
    )
    def test_kind_from_bare_message(self, message: str, expected: str) -> None:
        """A sticky ``last_error`` string classifies off the rendered class name."""
        assert RunError.from_message(message).kind == expected

    def test_wrapped_exception_classifies_from_the_inner_error(self) -> None:
        """``LlmResilienceExhausted`` is a RuntimeError — the inner type wins."""
        inner = _litellm_exc("BadGatewayError", "BadGatewayError: OpenAIException")
        exc = _FakeExhausted(_BAD_GATEWAY, inner, ["claude-fable-5"])
        assert RunError.from_exception(exc).kind == "upstream_bad_gateway"

    def test_cause_chain_is_walked(self) -> None:
        """A ``raise ... from`` chain still reaches the classifiable type."""
        inner = _litellm_exc("RateLimitError", "RateLimitError: slow down")
        outer = RuntimeError("wrapper")
        outer.__cause__ = inner
        assert RunError.from_exception(outer).kind == "rate_limited"

    def test_unknown_exception_degrades_without_raising(self) -> None:
        """An unrecognised failure is ``unknown``, never an exception."""
        err = RunError.from_exception(ValueError("something entirely novel"))
        assert err.kind == "unknown"
        assert err.title == "something entirely novel"

    def test_tool_failure_marker_loses_to_a_named_class(self) -> None:
        """The ``ERROR:`` marker is only a fallback — a class name outranks it."""
        message = "ERROR: litellm.RateLimitError: RateLimitError: slow down"
        assert RunError.from_message(message).kind == "rate_limited"


class TestHtmlNeverReachesTitle:
    """THE load-bearing rule: an HTML body contributes nothing to ``title``."""

    def test_html_body_yields_a_synthesized_title(self) -> None:
        err = RunError.from_message(_BAD_GATEWAY)
        assert err.title == "Upstream returned an HTML error page (502)"

    def test_no_fragment_of_the_page_survives_into_title(self) -> None:
        """Nothing from the document — least of all its host — reaches the title."""
        err = RunError.from_message(_BAD_GATEWAY)
        for leaked in ("git.example.com", "proxy", "internal-llm", "Bad Gateway</", "<"):
            assert leaked not in err.title
        assert "502 Bad Gateway" not in err.title

    def test_html_detail_is_retained_verbatim_up_to_the_cap(self) -> None:
        """The page stays available as a diagnostic; only the title is guarded."""
        err = RunError.from_message(_BAD_GATEWAY)
        assert "<!DOCTYPE html>" in err.detail

    def test_markup_bearing_title_is_dropped_not_sanitized(self) -> None:
        """The INNER TEXT must not survive — asserting on brackets is vacuous.

        Stripping ``<...>`` and keeping what was between them passes a
        "no angle brackets" check while preserving the whole payload, so this
        asserts the HOST SUBSTRING is gone. A title is a label; a markup-bearing
        one degrades to the fallback rather than being sanitized into something
        plausible-looking.
        """
        err = RunError(
            kind="unknown",
            title="<title>502 Bad Gateway - llm-proxy.git.example.com</title>",
            provider=None,
            detail="boom",
        )
        assert "git.example.com" not in err.title
        assert "llm-proxy" not in err.title
        assert "502 Bad Gateway" not in err.title
        assert err.title == "Run failed"

    def test_markup_bearing_title_is_dropped_on_the_model_validate_path(self) -> None:
        """Same guarantee when re-validating a STORED payload, not just on init.

        This is the reachable path: ``error_detail`` is persisted to Mongo and
        the console renders ``detail.title`` as the card headline.
        """
        err = RunError.model_validate(
            {
                "kind": "unknown",
                "title": "<title>502 Bad Gateway - llm-proxy.git.example.com</title>",
                "detail": "x",
            }
        )
        assert "git.example.com" not in err.title
        assert "llm-proxy" not in err.title
        assert err.title == "Run failed"

    def test_bare_angle_bracket_is_arithmetic_not_markup(self) -> None:
        """A ``<`` with no closing ``>`` must not trigger the drop."""
        err = RunError(kind="unknown", title="max_tokens < min_tokens", detail="boom")
        assert err.title == "max_tokens < min_tokens"

    def test_title_clamped_to_120_chars(self) -> None:
        err = RunError(kind="unknown", title="z" * 500, provider=None, detail="boom")
        assert len(err.title) == 120
        assert err.title.endswith("…")

    def test_title_never_empties_out(self) -> None:
        """Markup-only input falls back rather than yielding an empty label."""
        err = RunError(kind="unknown", title="<html><head></head></html>", detail="boom")
        assert err.title == "Run failed"


class TestTitleDerivation:
    """A non-HTML message produces a readable one-line label."""

    def test_wrapper_prefix_and_duplicate_segments_are_dropped(self) -> None:
        err = RunError.from_message(_RATE_LIMITED)
        assert err.title == (
            "RateLimitError: OpenAIException - No deployments available for selected model"
        )

    def test_plain_message_is_its_own_title(self) -> None:
        err = RunError.from_message("litellm.Timeout: Request timed out after 600.0s")
        assert err.title == "Timeout: Request timed out after 600.0s"


class TestDetailCapping:
    """``detail_chars`` is the PRE-cap length; ``truncated`` flips at the boundary."""

    def test_detail_chars_reflects_the_original_length(self) -> None:
        """A realistic upstream-HTML failure sits UNDER the 10k cap.

        The real session's message was 5,887 characters — the cap is a
        runaway guard, not what bounds the wire. ``brief()`` is what keeps the
        flat payload keys small, which is why this case is untruncated and
        still fixed the bug.
        """
        err = RunError.from_message(_BAD_GATEWAY)
        assert err.detail_chars == len(_BAD_GATEWAY)
        assert err.truncated is False
        # What actually bounds the wire is the flat projection, not the cap.
        assert len(err.brief()) <= 500

    @pytest.mark.parametrize(
        ("length", "truncated", "kept"),
        [
            (9_999, False, 9_999),
            (10_000, False, 10_000),
            (10_001, True, 10_000),
        ],
    )
    def test_truncation_boundary(self, length: int, truncated: bool, kept: int) -> None:
        err = RunError.from_message("y" * length)
        assert err.truncated is truncated
        assert err.detail_chars == length
        assert len(err.detail) == kept

    def test_round_trip_preserves_the_original_length(self) -> None:
        """Re-validating a stored payload must not re-derive a shorter count."""
        original = RunError.from_message("y" * 25_000)
        restored = RunError.model_validate(original.model_dump(mode="json"))
        assert restored.detail_chars == 25_000
        assert restored.truncated is True
        assert restored == original

    def test_inconsistent_supplied_counts_are_re_derived(self) -> None:
        """Presence is not consistency — a payload cannot describe itself wrong.

        The console clamps a too-small ``detail_chars`` but takes
        ``truncated`` at face value, so an inconsistent pair silently removed
        its "showing N of M" affordance.
        """
        err = RunError(
            kind="unknown",
            title="t",
            detail="A" * 25_000,
            detail_chars=0,
            truncated=False,
        )
        assert len(err.detail) == 10_000
        assert err.detail_chars == 25_000
        assert err.truncated is True

    def test_supplied_count_below_the_cap_must_match_exactly(self) -> None:
        """Uncapped text cannot honestly claim a longer pre-cap length."""
        err = RunError(kind="unknown", title="t", detail="abc", detail_chars=100)
        assert err.detail_chars == 3
        assert err.truncated is False

    def test_validation_does_not_mutate_the_callers_mapping(self) -> None:
        """``model_validate(mongo_doc)`` must read the doc, not rewrite it."""
        doc = {"kind": "unknown", "title": "t", "detail": "A" * 25_000}
        snapshot = dict(doc)
        RunError.model_validate(doc)
        assert doc == snapshot
        assert len(doc["detail"]) == 25_000
        assert "detail_chars" not in doc
        assert "truncated" not in doc

    def test_extra_keys_are_forbidden(self) -> None:
        with pytest.raises(ValueError):
            RunError.model_validate(
                {
                    "kind": "unknown",
                    "title": "x",
                    "provider": None,
                    "detail": "y",
                    "detail_chars": 1,
                    "truncated": False,
                    "traceback": "nope",
                }
            )


class TestProviderExtraction:
    """The failing model is recovered from the exception or our own message."""

    def test_plain_model_name(self) -> None:
        assert RunError.from_message(_BAD_GATEWAY).provider == "claude-fable-5"

    def test_prefixed_model_name(self) -> None:
        assert RunError.from_message(_RATE_LIMITED).provider == "openai/claude-sonnet-4-6"

    def test_models_tried_attribute_wins_over_the_message(self) -> None:
        inner = _litellm_exc("BadGatewayError", "BadGatewayError")
        exc = _FakeExhausted(_BAD_GATEWAY, inner, ["claude-fable-5", "openai/gpt-5"])
        assert RunError.from_exception(exc).provider == "claude-fable-5, openai/gpt-5"

    def test_model_argument_is_the_fallback(self) -> None:
        err = RunError.from_exception(ValueError("boom"), model="claude-opus-4-8")
        assert err.provider == "claude-opus-4-8"

    def test_provider_is_none_when_nothing_names_a_model(self) -> None:
        assert RunError.from_message("boom").provider is None

    def test_markup_bearing_provider_degrades_to_none(self) -> None:
        """``provider`` renders in a badge — same markup rule as ``title``.

        A markup-bearing value is not a model name, so it drops rather than
        being sanitized into a string that still carries its payload.
        """
        err = RunError.from_exception(
            ValueError("x"), model="<b>secret-deploy.git.example.com</b>"
        )
        assert err.provider is None

    def test_provider_is_length_bounded(self) -> None:
        """``_models_tried`` joins a fallback ladder with no cap of its own."""
        exc = _FakeExhausted("boom", ValueError("inner"), [f"model-{i}" for i in range(2_000)])
        err = RunError.from_exception(exc)
        assert err.provider is not None
        assert len(err.provider) <= 200


class TestBrief:
    """``brief()`` is the bounded, markup-free blurb the flat keys carry.

    It is subject to the SAME guards as ``title``: it is what Aura's error
    card and the CLI render, and it is persisted on the completion event, so
    a raw ``detail`` slice would republish an upstream page's markup forever.
    """

    def test_html_body_never_reaches_brief(self) -> None:
        """The defect this class exists to pin: no markup in the flat key."""
        brief = RunError.from_message(_BAD_GATEWAY).brief()
        assert "<" not in brief
        assert ">" not in brief

    def test_upstream_page_title_never_reaches_brief(self) -> None:
        """The page's own <title> names internal infrastructure — never project it."""
        brief = RunError.from_message(_BAD_GATEWAY).brief()
        for leaked in ("git.example.com", "proxy", "internal-llm", "502 Bad Gateway"):
            assert leaked not in brief

    def test_html_body_degrades_to_the_title(self) -> None:
        err = RunError.from_message(_BAD_GATEWAY)
        assert err.brief() == "Upstream returned an HTML error page (502)"

    def test_detail_still_holds_the_full_raw_text(self) -> None:
        """Guard against anyone "fixing" the leak by sanitizing ``detail``.

        ``detail`` is the expandable diagnostic and must stay verbatim; only
        the flat projection is guarded.
        """
        err = RunError.from_message(_BAD_GATEWAY)
        assert err.detail == _BAD_GATEWAY
        assert "<!DOCTYPE html>" in err.detail
        assert "<title>" in err.detail

    def test_normal_message_keeps_the_useful_diagnostic(self) -> None:
        """A non-HTML failure must NOT degrade to a generic label."""
        brief = RunError.from_message(_RATE_LIMITED).brief()
        assert "No deployments available for selected model" in brief
        assert "RateLimitError" in brief
        assert brief != "Run failed"

    @pytest.mark.parametrize(
        "message",
        [
            _BAD_GATEWAY,
            _RATE_LIMITED,
            "litellm.InternalServerError: " + ("detail " * 2_000),
            "y" * 25_000,
            "boom",
        ],
    )
    def test_brief_never_exceeds_500_chars(self, message: str) -> None:
        """The ceiling is inclusive of the ellipsis, on every path."""
        assert len(RunError.from_message(message).brief()) <= 500

    def test_long_non_html_message_is_truncated_with_an_ellipsis(self) -> None:
        brief = RunError.from_message("litellm.InternalServerError: " + "x" * 5_000).brief()
        assert len(brief) == 500
        assert brief.endswith("…")

    def test_short_errors_pass_through_untouched(self) -> None:
        assert RunError.from_message("boom").brief() == "boom"

    @pytest.mark.parametrize(
        "message",
        [
            "ValidationError: max_tokens < min_tokens (8 < 16), refusing call",
            "TypeError: expected <int>, got str",
        ],
    )
    def test_arithmetic_angle_bracket_does_not_truncate_the_diagnostic(
        self, message: str
    ) -> None:
        """A ``<`` in a NON-HTML message is arithmetic or a type name.

        Cutting there destroyed the diagnostic that Aura, the CLI and the
        ``on_session_end`` hook (→ forge PR comment) receive — the cut is only
        warranted when there is a body to guard against.
        """
        assert RunError.from_message(message).brief() == message

    def test_html_still_degrades_even_though_the_cut_is_gated(self) -> None:
        """The HTML path must not be weakened to fix the arithmetic case."""
        assert RunError.from_message(_BAD_GATEWAY).brief() == (
            "Upstream returned an HTML error page (502)"
        )

    def test_tag_regex_is_linear_not_quadratic(self) -> None:
        """``<[^>]*>`` backtracked quadratically on a run of unclosed ``<``."""
        start = time.perf_counter()
        RunError(kind="unknown", title="<" * 200_000, detail="boom")
        assert time.perf_counter() - start < 1.0

    def test_brief_falls_back_to_title_when_detail_is_blank(self) -> None:
        err = RunError(kind="unknown", title="Some label", detail="   ")
        assert err.brief() == "Some label"


class TestOrchestratorWiring:
    """The bug this fixes, asserted at the caller site the payload comes from."""

    def test_upstream_html_failure_never_reaches_a_payload_raw(self, tmp_path) -> None:
        """A 502 HTML page must not ride the transcript or the completion event."""
        store = SessionStore(root_dir=str(tmp_path))
        orch = Orchestrator(session_store=store)
        session_id = store.create_session()

        async def _raise_bad_gateway(*_args, **_kwargs):
            inner = _litellm_exc("BadGatewayError", "BadGatewayError: OpenAIException")
            raise _FakeExhausted(_BAD_GATEWAY, inner, ["claude-fable-5"])

        with patch.object(ToolUseLoop, "run", _raise_bad_gateway):
            orch.run(user_query="hi", session_id=session_id, max_iters=1)

        transcript = store.load_transcript(session_id)
        completion = next(e for e in transcript if e.get("type") == "completion")
        payload = completion["payload"]

        # The flat keys some clients read (Aura's error card, the CLI) stay
        # present, bounded AND markup-free — they are persisted here, so a raw
        # slice would republish the page on every history replay.
        for key in ("error", "last_error"):
            assert len(payload[key]) <= 500
            assert "<" not in payload[key]
            assert "git.example.com" not in payload[key]
            assert payload[key] == "Upstream returned an HTML error page (502)"

        # The structured record carries the classification and the diagnostic.
        detail = payload["error_detail"]
        assert detail["kind"] == "upstream_bad_gateway"
        assert detail["title"] == "Upstream returned an HTML error page (502)"
        assert detail["provider"] == "claude-fable-5"
        assert detail["detail_chars"] == len(_BAD_GATEWAY)

        # No markup in the synthetic assistant closure — it feeds the next
        # run's ``recent_events`` bullet list, which is where the page used to
        # land and burn context.
        closure = next(e for e in transcript if e.get("type") == "assistant")
        assert "<" not in closure["payload"]["text"]
        assert "Upstream returned an HTML error page (502)" in closure["payload"]["text"]

    def test_session_end_hook_receives_a_bounded_string(self, tmp_path) -> None:
        """The hook's string is posted OUTWARD — into forge PR comments and chat.

        ``channels/routes.py:extract_final_answer`` wraps it as "Session ended
        with an error: {error}", which the vcs-pickup completion hook posts as
        a PR/issue comment and the channel adapters send as a chat message. A
        raw ``str(exc)`` there republishes the provider's page — and the
        internal infrastructure its markup names — somewhere potentially public.
        """
        store = SessionStore(root_dir=str(tmp_path))
        hooks = HookManager()
        seen: list[str | None] = []
        hooks.run_on_session_end = lambda _sid, err: seen.append(err)  # type: ignore[method-assign]
        orch = Orchestrator(session_store=store, hook_manager=hooks)
        session_id = store.create_session()

        async def _raise_bad_gateway(*_args, **_kwargs):
            inner = _litellm_exc("BadGatewayError", "BadGatewayError: OpenAIException")
            raise _FakeExhausted(_BAD_GATEWAY, inner, ["claude-fable-5"])

        with patch.object(ToolUseLoop, "run", _raise_bad_gateway):
            orch.run(user_query="hi", session_id=session_id, max_iters=1)

        assert len(seen) == 1
        received = seen[0]
        assert received is not None
        assert "<" not in received
        assert "git.example.com" not in received
        assert len(received) <= 500
        assert received != _BAD_GATEWAY

    def test_session_end_hook_still_receives_none_on_success(self, tmp_path) -> None:
        """The ``str | None`` contract is unchanged — success stays None."""
        store = SessionStore(root_dir=str(tmp_path))
        hooks = HookManager()
        seen: list[str | None] = []
        hooks.run_on_session_end = lambda _sid, err: seen.append(err)  # type: ignore[method-assign]
        orch = Orchestrator(session_store=store, hook_manager=hooks)
        session_id = store.create_session()

        async def _succeed(*_args, **_kwargs):
            task_queue = TaskQueue(action_steps=[])
            task_queue.task_result = "All done."
            state = OrchestrationState(goal="go")
            state.done = True
            state.done_reason = "completed"
            return task_queue, state

        # Title generation is a live LLM call on this branch — stub that I/O
        # boundary so the test never reaches a provider.
        with (
            patch.object(ToolUseLoop, "run", _succeed),
            patch.object(Orchestrator, "_maybe_generate_title", lambda *_a, **_k: None),
        ):
            orch.run(user_query="hi", session_id=session_id, max_iters=1)

        assert seen == [None]

    def test_sticky_last_error_is_capped_on_the_non_exception_path(self, tmp_path) -> None:
        """``max_steps_reached`` & friends: the CLI and scg map-job read this raw.

        The payload copies were already bounded; the sticky attribute the
        in-process readers actually take was not.
        """
        store = SessionStore(root_dir=str(tmp_path))
        orch = Orchestrator(session_store=store)
        session_id = store.create_session()

        async def _finish_with_sticky_error(*_args, **_kwargs):
            task_queue = TaskQueue(action_steps=[])
            task_queue.last_error = _BAD_GATEWAY
            state = OrchestrationState(goal="go")
            state.done = True
            state.done_reason = "max_steps_reached"
            return task_queue, state

        with (
            patch.object(ToolUseLoop, "run", _finish_with_sticky_error),
            patch.object(Orchestrator, "_maybe_generate_title", lambda *_a, **_k: None),
        ):
            result = orch.run(user_query="go", session_id=session_id, max_iters=1)

        assert result.last_error is not None
        assert len(result.last_error) <= 500
        assert "<" not in result.last_error
        assert "git.example.com" not in result.last_error

    def test_sticky_last_error_is_capped_even_when_the_run_completes(
        self, tmp_path
    ) -> None:
        """The clamp applies to a clean run too, and so does the payload.

        A run that recovers from a mid-run tool failure and finishes clean still
        carries a sticky ``last_error``. The scg map-job reads that attribute
        with NO ``done_reason`` check and persists it onto the job record, so
        gating the CLAMP on "not completed" let a raw provider page escape
        through a SUCCESSFUL run.

        The RECORD is not gated on that condition either. Withholding every key
        would silence overwhelmingly the LAUNDERED runs — a halt or an unmet
        outcome presenting as success — and dropping the one field able to
        contradict the status is what makes a wrong status unfalsifiable. Only
        ``error`` is withheld from a completed run, because that key alone is
        what a client renders as an error card. Both the clamp and the carried
        record are asserted here; what must never regress is that either one
        lets an unbounded provider page through.
        """
        store = SessionStore(root_dir=str(tmp_path))
        orch = Orchestrator(session_store=store)
        session_id = store.create_session()

        async def _recovered_and_completed(*_args, **_kwargs):
            task_queue = TaskQueue(action_steps=[])
            task_queue.task_result = "Recovered and finished."
            task_queue.last_error = _BAD_GATEWAY
            state = OrchestrationState(goal="go")
            state.done = True
            state.done_reason = "completed"
            return task_queue, state

        with (
            patch.object(ToolUseLoop, "run", _recovered_and_completed),
            patch.object(Orchestrator, "_maybe_generate_title", lambda *_a, **_k: None),
        ):
            result = orch.run(user_query="go", session_id=session_id, max_iters=1)

        # (1) The attribute its readers take is clamped regardless of outcome.
        assert result.last_error is not None
        assert len(result.last_error) <= 500
        assert "<html" not in result.last_error
        assert "git.example.com" not in result.last_error

        # (2) The payload CARRIES the record even on a completed run, so the
        # status can be checked against it rather than merely believed — and
        # every carried key goes through the same bound as the attribute.
        completion = next(
            e for e in store.load_transcript(session_id) if e.get("type") == "completion"
        )
        payload = completion["payload"]
        assert payload["done_reason"] == "completed"
        # ``error`` is the client's error-card trigger and is withheld from a
        # completed run; the record rides ``last_error`` instead, under the SAME
        # bound — which is what this test exists to protect.
        assert "error" not in payload
        assert len(payload["last_error"]) <= 500
        assert "<html" not in payload["last_error"]
        assert "git.example.com" not in payload["last_error"]
        # The structured record keeps the full diagnostic behind its own cap and
        # classifies from the exception type, never from the HTML body.
        assert payload["error_detail"]["kind"] == "upstream_bad_gateway"
        assert "git.example.com" not in payload["error_detail"]["title"]
