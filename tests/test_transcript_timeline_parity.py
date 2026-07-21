"""Cross-language parity enforcement for the transcript turn assembler.

Two implementations reconstruct turns from the same event log — the canonical
``TranscriptTimeline`` and the console's ``buildTimeline``. Their top-of-file
comments have long asserted the two MUST stay in sync, but nothing checked it:
each side tested only against itself, so a rule added to one and missed by the
other shipped silently. That is exactly how the Python port came to render no
plan, todos, widget, run_failure or question rows while claiming parity.

``tests/fixtures/transcript_timeline_corpus.json`` is the contract. It holds
event logs plus the rows both implementations must produce, written from the
documented rules rather than dumped from either implementation — an expectation
generated from the code under test proves only that the code equals itself.

WHAT THIS FILE ENFORCES TODAY: the Python side matches the corpus, and every
role the Python side can emit has a case in the corpus (so adding a role without
adding a case fails). WHAT IT DOES NOT: the TS side is not executed here. Until
a vitest test replays this same file, TS drift is caught by review, not by a
gate. The corpus is deliberately language-neutral so that test is a small
addition rather than a second convention.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, get_args

import pytest
from mewbo_core.transcript_timeline import TimelineEntry, TimelineRole, TranscriptTimeline

CORPUS_PATH = Path(__file__).parent / "fixtures" / "transcript_timeline_corpus.json"


def _corpus() -> list[dict[str, Any]]:
    return json.loads(CORPUS_PATH.read_text())["cases"]


def _project(entry: TimelineEntry) -> dict[str, Any]:
    """Reduce an entry to the language-neutral shape the corpus records.

    The corpus uses the TS ``TimelineEntry`` spelling (``turnId``), so the
    mapping happens here rather than in the fixture — the fixture is the shared
    contract and must not lean toward either implementation's conventions.
    """
    return {
        "id": entry.id,
        "role": entry.role,
        "turnId": entry.turn_id,
        "content": entry.content,
        "ts": entry.ts,
    }


@pytest.mark.parametrize("case", _corpus(), ids=lambda c: c["name"])
def test_python_matches_the_shared_corpus(case: dict[str, Any]):
    """The canonical assembler reproduces the contract row for row."""
    produced = [_project(e) for e in TranscriptTimeline.assemble(case["events"])]
    assert produced == case["expected"], case["why"]


def test_every_role_is_represented_in_the_corpus():
    """A role with no case is a rule no cross-language test can ever compare.

    This is the guard that makes the corpus self-maintaining: widening
    ``TimelineRole`` without seeding a case fails here rather than quietly
    leaving the new role outside the contract.
    """
    declared = set(get_args(TimelineRole))
    covered = {row["role"] for case in _corpus() for row in case["expected"]}
    assert declared - covered == set(), (
        "roles missing a corpus case — add one to "
        "tests/fixtures/transcript_timeline_corpus.json"
    )


def test_corpus_expectations_are_not_self_generated():
    """Every case states WHY it holds, in prose a reviewer can check.

    A corpus whose expectations were dumped from the implementation would pass
    forever while encoding a bug; requiring a stated rationale per case keeps a
    regenerated fixture from silently becoming the new contract.
    """
    for case in _corpus():
        assert case.get("why", "").strip(), f"case {case['name']} has no stated rationale"
        assert case.get("events"), f"case {case['name']} has no events"
