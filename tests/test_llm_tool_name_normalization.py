"""Tool names a provider mangles are reconciled at the adapter seam.

Observed shapes: a namespace prefix leaking into the returned function name
(``default_api_shell``) and case drift. Both reach the loop as an unknown tool,
so the call is wasted. Per the cross-model normalization law the fix belongs
here — at the LiteLLM response seam, where the request's bound tool list and the
response are both in scope — never in the orchestration loop.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from mewbo_core import llm as llm_module


def _tools(*names: str) -> list[dict]:
    """The bound-tools shape a request carries (OpenAI function format)."""
    return [{"type": "function", "function": {"name": n, "parameters": {}}} for n in names]


def _response(*call_names: str):
    """A non-streaming litellm response carrying *call_names* as tool calls."""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    tool_calls=[
                        SimpleNamespace(
                            id=f"c{i}",
                            function=SimpleNamespace(name=name, arguments="{}"),
                        )
                        for i, name in enumerate(call_names)
                    ],
                    function_call=None,
                ),
                delta=None,
            )
        ],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )


class _FakeLiteLLM:
    """Stands in for the litellm module — the only I/O boundary stubbed here."""

    def __init__(self, response_factory) -> None:
        self._factory = response_factory
        self.seen_kwargs: dict = {}

    def completion(self, **kwargs):
        self.seen_kwargs = kwargs
        return self._factory()

    async def acompletion(self, **kwargs):
        self.seen_kwargs = kwargs
        return self._factory()


def _returned_names(response) -> list[str]:
    return [c.function.name for c in response.choices[0].message.tool_calls]


def _call(inner, **kwargs):
    return llm_module._UsageNormalizingLiteLLM(inner).completion(**kwargs)


@pytest.mark.parametrize(
    "returned",
    [
        "default_api_shell",
        "default_api:shell",
        "default_api.shell",
        "DEFAULT_API_SHELL",
        "Shell",
        "SHELL",
    ],
)
def test_mangled_name_resolves_to_the_bound_tool(returned):
    """Prefix leak, separator drift and case drift all fold onto the bound name."""
    inner = _FakeLiteLLM(lambda: _response(returned))

    out = _call(inner, tools=_tools("shell", "file_edit"), messages=[])

    assert _returned_names(out) == ["shell"]


def test_exact_name_is_untouched():
    inner = _FakeLiteLLM(lambda: _response("file_edit"))

    out = _call(inner, tools=_tools("shell", "file_edit"), messages=[])

    assert _returned_names(out) == ["file_edit"]


def test_unknown_name_passes_through_unchanged():
    """Never rename onto a tool that was not bound — that manufactures a call."""
    inner = _FakeLiteLLM(lambda: _response("wander_off"))

    out = _call(inner, tools=_tools("shell"), messages=[])

    assert _returned_names(out) == ["wander_off"]


def test_a_tool_genuinely_named_for_the_namespace_resolves_to_itself():
    """The prefix strip is a last resort, so an exact match always wins."""
    inner = _FakeLiteLLM(lambda: _response("default_api_shell"))

    out = _call(inner, tools=_tools("default_api_shell", "shell"), messages=[])

    assert _returned_names(out) == ["default_api_shell"]


def test_ambiguous_fold_resolves_to_nothing():
    """Two bound tools sharing a fold must never be guessed between."""
    inner = _FakeLiteLLM(lambda: _response("FILEEDIT"))

    out = _call(inner, tools=_tools("file_edit", "fileedit"), messages=[])

    assert _returned_names(out) == ["FILEEDIT"]


def test_unbound_request_is_a_no_op():
    """A call with no tools declared has no index to reconcile against."""
    inner = _FakeLiteLLM(lambda: _response("default_api_shell"))

    out = _call(inner, messages=[])

    assert _returned_names(out) == ["default_api_shell"]


def test_legacy_function_call_slot_is_normalized():
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    tool_calls=None,
                    function_call=SimpleNamespace(name="default_api_shell", arguments="{}"),
                ),
                delta=None,
            )
        ],
        usage=None,
    )
    inner = _FakeLiteLLM(lambda: response)

    out = _call(inner, tools=_tools("shell"), messages=[])

    assert out.choices[0].message.function_call.name == "shell"


def test_async_and_streaming_paths_are_covered():
    """Every exit of the shim normalizes — a fix on one path only is a half fix."""

    async def _drive() -> tuple[list[str], list[str]]:
        inner = _FakeLiteLLM(lambda: _response("default_api_shell"))
        shim = llm_module._UsageNormalizingLiteLLM(inner)
        non_stream = await shim.acompletion(tools=_tools("shell"), messages=[])

        async def _chunks():
            yield _response("DEFAULT_API_SHELL")

        streamed = []
        inner_stream = _FakeLiteLLM(_chunks)
        async for chunk in await llm_module._UsageNormalizingLiteLLM(
            inner_stream
        ).acompletion(tools=_tools("shell"), messages=[], stream=True):
            streamed.extend(_returned_names(chunk))
        return _returned_names(non_stream), streamed

    non_stream_names, streamed_names = asyncio.run(_drive())
    assert non_stream_names == ["shell"]
    assert streamed_names == ["shell"]


def test_partial_streaming_name_is_left_alone():
    """A delta can carry a fragment; guessing a tool from it would be invention."""
    chunk = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=None,
                delta=SimpleNamespace(
                    tool_calls=[
                        SimpleNamespace(function=SimpleNamespace(name="she", arguments=""))
                    ],
                    function_call=None,
                ),
            )
        ],
        usage=None,
    )
    normalizer = llm_module._ToolNameNormalizer(["shell"])

    normalizer.apply(chunk)

    assert chunk.choices[0].delta.tool_calls[0].function.name == "she"


def test_malformed_response_never_breaks_the_call():
    """The shim is on the hot path; a shape it does not recognise passes through."""
    inner = _FakeLiteLLM(lambda: SimpleNamespace(choices="not-a-list", usage=None))

    out = _call(inner, tools=_tools("shell"), messages=[])

    assert out.choices == "not-a-list"
