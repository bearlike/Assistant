"""Golden byte-equality tests for the migrated compaction prompts (Gitea #89).

Phase 1 is a VERBATIM extraction: the registry must reproduce the exact bytes
that ``compact.py`` previously hardcoded. Each ``EXPECTED_*`` literal below is a
frozen copy of the original constant/f-string; if a future edit retunes a
prompt, the byte-equality assertion fails loudly. The originals live in the
registry now (``compact.yaml``); this test is the contract that the migration
changed nothing.
"""

from __future__ import annotations

from mewbo_core.prompt_registry import get_prompt_registry

# --- Frozen originals (copied verbatim from compact.py before migration) -----

EXPECTED_FULL = """\
You are summarizing a conversation to fit within a context window.
Do NOT use any tools. Do NOT generate code. This is a summarization task only.

Produce your response in two parts:

<analysis>
Reason about what information is critical to preserve vs what can be safely discarded.
Consider: active tasks, recent errors, file context, user preferences expressed.
This section will be removed from the final summary.
</analysis>

<summary>
## Primary Request
What the user originally asked for and the overall goal.

## Key Technical Concepts
Important technical details, architecture decisions, constraints discovered.

## Files and Code
Key files read or modified, with brief relevant context.

## Errors and Fixes
Any errors encountered and how they were resolved.

## Current State
Where the conversation left off, what is in progress.

## Pending Tasks
Anything the user asked for that has not been completed yet.
</summary>
"""


EXPECTED_CAVEMAN = """\
You are summarizing a conversation to fit within a context window.
Do NOT use any tools. Do NOT generate code. This is a summarization task only.

=== TERSE MODE: ACTIVE ===
Write all summary prose like smart caveman. Technical substance stays exact. Only fluff dies.

Drop rules:
- Articles: a, an, the.
- Filler adverbs: just, really, basically, actually, simply, essentially, generally.
- Pleasantries: sure, certainly, of course, happy to, let me, I'd recommend.
- Hedging: perhaps, maybe, might be worth, it would be good to, I think.
- Redundant phrasing: "in order to" -> "to"; "make sure to" -> "ensure";
  "the reason is because" -> "because"; "utilize" -> "use".
- Connective fluff: however, furthermore, additionally, moreover, in addition.
- Pronoun subjects when obvious: prefer imperative ("Fix auth bug.")
  over "you should fix the auth bug".

Style rules:
- Fragments OK. Short synonyms. Imperative voice preferred.
- Pattern: [thing] [action] [reason]. [next step].
- One word when one word is enough.

Preserve EXACTLY (never compress these):
- Code blocks (fenced ``` or indented) and inline backticks.
- URLs, file paths, CLI commands.
- Library, API, class, and function names.
- Error strings (quote verbatim).
- Numeric values, dates, versions, env vars.
- Markdown headings (##) below — do not rename or reorder.
- Bullet hierarchy and list ordering.

Auto-Clarity escape: for security warnings, irreversible destructive actions,
or user confusion, revert to normal prose in that section. Resume terse next.

Persistence: every section stays terse. No drift. Still terse if unsure.
No filler creep after many bullets.

Example:
- Not: "The user originally asked me to help with debugging an authentication
       middleware issue that was causing token validation to fail intermittently."
- Yes: "User: debug auth middleware. Token validation fails intermittently."

Produce your response in two parts:

<analysis>
Reason about what information is critical to preserve vs what can be safely discarded.
Consider: active tasks, recent errors, file context, user preferences expressed.
This section will be removed from the final summary.
</analysis>

<summary>
## Primary Request
What user originally asked. Goal in one line.

## Key Technical Concepts
Architecture decisions, constraints, technical details. Fragments OK.

## Files and Code
Files read or modified. Path + one-line why.

## Errors and Fixes
Errors encountered (quote verbatim). Fix applied.

## Current State
Where conversation left off. What is in progress.

## Pending Tasks
What user asked for that is not yet done.
</summary>
"""


def _expected_focus_suffix(focus_prompt: str) -> str:
    """The exact string compact.py appended for ``/compact <focus>`` (verbatim)."""
    return (
        "\n\n## User Focus\n"
        "The user invoked compaction with a focus directive. Bias the "
        "summary toward content matching this directive without dropping "
        "critical state (active tasks, recent errors, file context):\n"
        f"{focus_prompt}"
    )


# --- Golden assertions -------------------------------------------------------


def test_compact_system_base_is_verbatim():
    assert get_prompt_registry().render("compact.system") == EXPECTED_FULL


def test_compact_system_caveman_scenario_is_verbatim():
    assert (
        get_prompt_registry().render("compact.system", scenario="caveman")
        == EXPECTED_CAVEMAN
    )


def test_compact_focus_suffix_is_verbatim():
    # Call sites pass the already-stripped focus value as ``focus_prompt``.
    focus = "auth middleware token validation"
    assert get_prompt_registry().render(
        "compact.focus_suffix", focus_prompt=focus
    ) == _expected_focus_suffix(focus)


def test_compact_registry_entries_validate():
    # Definition-time gate: templates parse, declared vars match, no drift.
    get_prompt_registry().validate_all()
