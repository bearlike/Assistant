"""Golden byte-equality tests for the migrated ``structured.*`` prompts.

Phase 1 of the central prompt registry (Gitea #89) is a VERBATIM extraction: the
hardcoded prompt constants/f-strings in ``structured_response.py`` and
``structured_synthesis.py`` move into ``prompts/registry/structured.yaml`` with
ZERO behaviour change. Each ``EXPECTED`` below is the ORIGINAL literal copied
verbatim; the test asserts ``render(...)`` reproduces it byte-for-byte for
representative inputs. If a render drifts by even one character (a stripped
newline, a re-flowed line), the golden fails — which is the whole point.
"""

from __future__ import annotations

from mewbo_core.prompt_registry import get_prompt_registry

# ---------------------------------------------------------------------------
# Verbatim copies of the ORIGINAL literals (the migration's source of truth).
# ---------------------------------------------------------------------------

# structured_response.py:FORCE_EMIT_DIRECTIVE (paren-concat, no trailing newline)
EXPECTED_FORCE_EMIT = (
    "You are operating in STRUCTURED OUTPUT mode. Your ONLY way to finish this "
    "task is to call the `emit_result` tool exactly once with arguments that "
    "validate against its schema. Do NOT answer in prose, do NOT write a final "
    "text message, and do NOT stop until you have called `emit_result`. Use any "
    "grounding tools you need first, then call `emit_result` to deliver the "
    "structured answer. A reply that does not call `emit_result` is a failure."
)

# structured_response.py:_REDRIVE_DIRECTIVE (FORCE_EMIT_DIRECTIVE + suffix)
EXPECTED_REDRIVE_DIRECTIVE = (
    EXPECTED_FORCE_EMIT
    + " You did not call `emit_result` last time. Do not gather any more "
    "context — call `emit_result` NOW with your best structured answer."
)

# structured_response.py:_REDRIVE_QUERY
EXPECTED_REDRIVE_QUERY = (
    "You must finish by calling the emit_result tool. "
    "Call emit_result now with the structured answer."
)

# structured_response.py:_on_validation_error — give-up branch
def _expected_reask_giveup(attempts: int, detail: str) -> str:
    return (
        f"Schema validation failed {attempts}x — giving up. "
        f"Last error: {detail}"
    )


# structured_response.py:_on_validation_error — reask branch
def _expected_reask_fix_fields(detail: str) -> str:
    return (
        f"Your output did not match the schema. {detail}. Fix these fields "
        "and call emit_result again."
    )


# structured_synthesis.py:_GROUNDED_HEADER
EXPECTED_GROUNDED_HEADER = (
    "## Grounded context\n\n"
    "The following snippets were retrieved from the workspace and MUST be "
    "used to populate the structured answer.  Cite only what is present here.\n\n"
)


# structured_synthesis.py:_format_citations per-line
def _expected_citation(i: int, kind: str, score: float, snippet: str) -> str:
    return f"[{i}] ({kind}) score={score:.3f} — {snippet.strip()}\n"


# structured_synthesis.py:synthesize reask_content
EXPECTED_SYNTHESIS_REASK = (
    "Your previous response did not validate against the schema. "
    "Correct the field errors and call emit_result again."
)


# ---------------------------------------------------------------------------
# Static prompts — verbatim, no variables.
# ---------------------------------------------------------------------------


def test_force_emit_directive_verbatim():
    reg = get_prompt_registry()
    assert reg.render("structured.force_emit_directive") == EXPECTED_FORCE_EMIT


def test_redrive_directive_inlines_force_emit_verbatim():
    reg = get_prompt_registry()
    rendered = reg.render("structured.redrive_directive")
    assert rendered == EXPECTED_REDRIVE_DIRECTIVE
    # The base directive must survive intact (assertions key off it across drives).
    assert EXPECTED_FORCE_EMIT in rendered


def test_redrive_query_verbatim():
    reg = get_prompt_registry()
    assert reg.render("structured.redrive_query") == EXPECTED_REDRIVE_QUERY


def test_grounded_header_verbatim_keeps_double_spaces_and_newlines():
    reg = get_prompt_registry()
    assert reg.render("structured.grounded_header") == EXPECTED_GROUNDED_HEADER


def test_synthesis_reask_verbatim():
    reg = get_prompt_registry()
    assert reg.render("structured.synthesis_reask") == EXPECTED_SYNTHESIS_REASK


# ---------------------------------------------------------------------------
# Templated prompts — byte-equality across representative inputs.
# ---------------------------------------------------------------------------


def test_reask_giveup_matches_fstring():
    reg = get_prompt_registry()
    for attempts, detail in [
        (3, "Field 'name': 'name' is a required property"),
        (5, "Field '<root>': boom"),
    ]:
        assert reg.render(
            "structured.reask_giveup", attempts=attempts, detail=detail
        ) == _expected_reask_giveup(attempts, detail)


def test_reask_fix_fields_matches_fstring():
    reg = get_prompt_registry()
    for detail in [
        "Field 'age': -1 is less than the minimum of 0",
        "Field '<root>': not valid",
    ]:
        assert reg.render(
            "structured.reask_fix_fields", detail=detail
        ) == _expected_reask_fix_fields(detail)


def test_grounded_citation_matches_fstring_including_3f_and_trailing_newline():
    reg = get_prompt_registry()
    cases = [
        (1, "code", 0.5, "  def foo():  "),
        (2, "doc", 0.12345, "snippet with trailing  "),
        (10, "graph", 1.0, "exact"),
    ]
    for i, kind, score, snippet in cases:
        assert reg.render(
            "structured.grounded_citation",
            i=i,
            kind=kind,
            score=score,
            snippet=snippet,
        ) == _expected_citation(i, kind, score, snippet)


# ---------------------------------------------------------------------------
# The whole structured.yaml is well-formed (declared vars == template vars).
# ---------------------------------------------------------------------------


def test_structured_entries_validate():
    reg = get_prompt_registry()
    reg.validate_all()
    for pid in [
        "structured.force_emit_directive",
        "structured.redrive_directive",
        "structured.redrive_query",
        "structured.reask_giveup",
        "structured.reask_fix_fields",
        "structured.grounded_header",
        "structured.grounded_citation",
        "structured.synthesis_reask",
    ]:
        assert reg.has(pid)
