"""Tests for the demo database seeder (mewbo_demo_seeder).

Drives the real code paths: the Pydantic bundle contract, offset rebasing
against an INJECTED fixed T0 (no clock patching — the seeder takes it as a
field), and a full ``seed()`` into mongomock-backed stores, asserting the
persisted documents match what the store contract echoes back and that the
console-facing ``summarize_session`` reads the sessions as completed.

Mongo is stubbed via the existing ``tests/test_session_store_mongo.py`` /
``tests/test_triggers_store_mongo.py`` pattern: patch ``MongoClient`` at the
store's import site before construction.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import mongomock
import pytest
from mewbo_core.key_store_mongo import MongoKeyStore
from mewbo_core.session_runtime import SessionRuntime
from mewbo_core.session_store_mongo import MongoSessionStore
from mewbo_core.triggers.spec import CronTrigger, WebhookTrigger
from mewbo_core.triggers.store_mongo import MongoTriggerStore
from mewbo_demo_seeder.models import (
    FileEditToolEvent,
    FileReadToolEvent,
    SeedBundle,
    ShellToolEvent,
)
from mewbo_demo_seeder.seeder import DemoSeeder
from pydantic import ValidationError

T0 = datetime(2026, 7, 14, 9, 30, 0, tzinfo=timezone.utc)

_BUNDLE_PATH = (
    Path(__file__).resolve().parents[2] / "demo" / "seeder" / "bundles" / "console-poc.json"
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session_store(tmp_path):
    """A MongoSessionStore backed by mongomock."""
    with patch("mewbo_core.session_store_mongo.MongoClient", mongomock.MongoClient):
        return MongoSessionStore(
            root_dir=str(tmp_path), uri="mongodb://localhost:27017", database="test_demo"
        )


@pytest.fixture
def trigger_store():
    """A MongoTriggerStore backed by mongomock."""
    with patch("mewbo_core.triggers.store_mongo.MongoClient", mongomock.MongoClient):
        return MongoTriggerStore(uri="mongodb://localhost:27017", database="test_demo")


@pytest.fixture
def key_store():
    """A MongoKeyStore backed by mongomock (canonical bundle declares api_keys)."""
    with patch("mewbo_core.key_store_mongo.MongoClient", mongomock.MongoClient):
        return MongoKeyStore(uri="mongodb://localhost:27017", database="test_demo")


def _minimal_bundle() -> dict:
    """A tiny but complete two-session + one-trigger bundle as raw JSON data."""
    return {
        "sessions": [
            {
                "id": "sess-shell",
                "title": "Shell session",
                "model": "claude-sonnet-5",
                "offset_seconds": -600,
                "events": [
                    {"kind": "context", "at_seconds": 0},
                    {"kind": "user", "at_seconds": 2, "text": "run the tests"},
                    {"kind": "agent_message", "at_seconds": 5, "text": "Running the suite now."},
                    {
                        "kind": "shell",
                        "at_seconds": 9,
                        "command": "npm test",
                        "cwd": "/workspace",
                        "exit_code": 0,
                        "stdout": "4 passed",
                        "duration_ms": 1200,
                    },
                    {
                        "kind": "file_read",
                        "at_seconds": 14,
                        "path": "src/auth.ts",
                        "text": "1\tconst x = 1;",
                        "total_lines": 1,
                    },
                    {
                        "kind": "file_edit",
                        "at_seconds": 17,
                        "path": "src/auth.ts",
                        "diff": (
                            "--- src/auth.ts\n+++ src/auth.ts\n"
                            "@@ -1 +1 @@\n-const x = 1;\n+const x = 2;"
                        ),
                    },
                    {"kind": "assistant", "at_seconds": 20, "text": "All green."},
                    {"kind": "completion", "at_seconds": 22, "task_result": "All green."},
                ],
            },
            {
                "id": "sess-todos",
                "title": "Todos session",
                "model": "claude-sonnet-5",
                "offset_seconds": -1200,
                "events": [
                    {"kind": "context", "at_seconds": 0},
                    {"kind": "user", "at_seconds": 2, "text": "make a report"},
                    {
                        "kind": "todos",
                        "at_seconds": 6,
                        "source": "agent",
                        "items": [
                            {"label": "gather", "status": "completed"},
                            {"label": "write", "status": "completed"},
                        ],
                    },
                    {"kind": "assistant", "at_seconds": 12, "text": "Report ready."},
                    {"kind": "completion", "at_seconds": 14, "task_result": "Report ready."},
                ],
            },
        ],
        "triggers": [
            {
                "id": "trig-cron",
                "session": "sess-shell",
                "kind": "time.cron",
                "wake_prompt": "Nightly digest",
                "created_by": "agent",
                "created_at_offset": -300,
                "fields": {"cron": "0 6 * * *"},
            }
        ],
    }


# ---------------------------------------------------------------------------
# bundle contract (validation at definition)
# ---------------------------------------------------------------------------


def test_minimal_bundle_round_trips():
    """A well-formed bundle validates and preserves its structure."""
    bundle = SeedBundle.model_validate(_minimal_bundle())
    assert [s.id for s in bundle.sessions] == ["sess-shell", "sess-todos"]
    assert bundle.triggers[0].kind == "time.cron"


def test_bundle_rejects_unknown_field():
    """An unknown key anywhere in the bundle is a clean ValidationError (extra=forbid)."""
    raw = _minimal_bundle()
    raw["sessions"][0]["events"][1]["token"] = "smuggled"
    with pytest.raises(ValidationError):
        SeedBundle.model_validate(raw)


def test_bundle_rejects_session_without_completion():
    """A session with no completion event cannot render as a finished run."""
    raw = _minimal_bundle()
    raw["sessions"][0]["events"] = [
        {"kind": "context", "at_seconds": 0},
        {"kind": "user", "at_seconds": 2, "text": "hi"},
        {"kind": "assistant", "at_seconds": 4, "text": "hello"},
    ]
    with pytest.raises(ValidationError):
        SeedBundle.model_validate(raw)


def test_bundle_rejects_non_increasing_offsets():
    """Two events at the same offset would collide onto one instant."""
    raw = _minimal_bundle()
    raw["sessions"][0]["events"][2]["at_seconds"] = 2  # equal to the user event
    with pytest.raises(ValidationError):
        SeedBundle.model_validate(raw)


def test_bundle_rejects_bad_trigger_field():
    """A malformed trigger spec fails at bundle-load, not at seed time."""
    raw = _minimal_bundle()
    raw["triggers"][0]["fields"] = {"cron": "not a cron expr"}
    with pytest.raises(ValidationError):
        SeedBundle.model_validate(raw)


def test_bundle_rejects_unknown_trigger_field():
    """A field the concrete TriggerSpec kind doesn't declare is rejected (extra=forbid)."""
    raw = _minimal_bundle()
    raw["triggers"][0]["fields"] = {"cron": "0 6 * * *", "nope": 1}
    with pytest.raises(ValidationError):
        SeedBundle.model_validate(raw)


def test_bundle_rejects_trigger_referencing_unknown_session():
    """A trigger must point at a session the bundle actually declares."""
    raw = _minimal_bundle()
    raw["triggers"][0]["session"] = "ghost"
    with pytest.raises(ValidationError):
        SeedBundle.model_validate(raw)


# ---------------------------------------------------------------------------
# offset rebasing (injected fixed T0)
# ---------------------------------------------------------------------------


def test_offsets_rebase_against_injected_t0(session_store, trigger_store):
    """Each event's ts is T0 + session offset + event offset, monotonically."""
    bundle = SeedBundle.model_validate(_minimal_bundle())
    DemoSeeder(
        session_store=session_store, trigger_store=trigger_store, bundle=bundle, t0=T0
    ).seed()

    events = session_store.load_transcript("sess-shell")
    first_ts = (T0 + timedelta(seconds=-600 + 0)).isoformat()
    assert events[0]["ts"] == first_ts
    # user event is session offset -600 + at_seconds 2.
    assert events[1]["ts"] == (T0 + timedelta(seconds=-600 + 2)).isoformat()
    timestamps = [e["ts"] for e in events]
    assert timestamps == sorted(timestamps)
    assert len(set(timestamps)) == len(timestamps)  # every ts distinct


def test_seed_documents_match_store_contract(session_store, trigger_store):
    """The persisted event docs equal what each union member's to_event produces."""
    bundle = SeedBundle.model_validate(_minimal_bundle())
    session = bundle.sessions[0]
    DemoSeeder(
        session_store=session_store, trigger_store=trigger_store, bundle=bundle, t0=T0
    ).seed()

    agent_id = DemoSeeder._root_agent_id(session.id)
    start = T0 + timedelta(seconds=session.offset_seconds)
    expected = [
        ev.to_event(
            start + timedelta(seconds=ev.at_seconds),
            session_model=session.model,
            agent_id=agent_id,
            session_id=session.id,
        )
        for ev in session.events
    ]
    assert session_store.load_transcript(session.id) == expected


def test_shell_and_file_tool_result_shapes(session_store, trigger_store):
    """Tool steps persist the exact structured result the console parses."""
    bundle = SeedBundle.model_validate(_minimal_bundle())
    DemoSeeder(
        session_store=session_store, trigger_store=trigger_store, bundle=bundle, t0=T0
    ).seed()

    events = session_store.load_transcript("sess-shell")
    tool_results = [e for e in events if e["type"] == "tool_result"]

    shell = json.loads(tool_results[0]["payload"]["result"])
    assert shell == {
        "kind": "shell",
        "command": "npm test",
        "cwd": "/workspace",
        "exit_code": 0,
        "stdout": "4 passed",
        "stderr": "",
        "duration_ms": 1200,
    }
    assert tool_results[0]["payload"]["success"] is True

    file_doc = json.loads(tool_results[1]["payload"]["result"])
    assert file_doc == {
        "kind": "file",
        "path": "src/auth.ts",
        "text": "1\tconst x = 1;",
        "total_lines": 1,
    }

    # The file-edit step renders as the console's DiffCard (kind: "diff").
    edit_doc = json.loads(tool_results[2]["payload"]["result"])
    assert edit_doc["kind"] == "diff"
    assert edit_doc["files"] == ["src/auth.ts"]
    assert edit_doc["text"].startswith("--- src/auth.ts")
    assert tool_results[2]["payload"]["tool_id"] == "search_replace_block"


def test_todos_event_shape(session_store, trigger_store):
    """The todos event carries {items, source, agent_id} the console renders."""
    bundle = SeedBundle.model_validate(_minimal_bundle())
    DemoSeeder(
        session_store=session_store, trigger_store=trigger_store, bundle=bundle, t0=T0
    ).seed()

    events = session_store.load_transcript("sess-todos")
    todos = next(e for e in events if e["type"] == "todos")
    assert todos["payload"]["source"] == "agent"
    assert todos["payload"]["agent_id"] == DemoSeeder._root_agent_id("sess-todos")
    assert [i["label"] for i in todos["payload"]["items"]] == ["gather", "write"]
    assert all(i["status"] == "completed" for i in todos["payload"]["items"])


# ---------------------------------------------------------------------------
# console-facing contract: summarize_session
# ---------------------------------------------------------------------------


def test_seeded_session_summarizes_as_completed(session_store, trigger_store):
    """summarize_session reads a seeded session as a completed run with its title."""
    bundle = SeedBundle.model_validate(_minimal_bundle())
    DemoSeeder(
        session_store=session_store, trigger_store=trigger_store, bundle=bundle, t0=T0
    ).seed()

    runtime = SessionRuntime(session_store=session_store)
    summary = runtime.summarize_session("sess-shell")
    assert summary["status"] == "completed"
    assert summary["title"] == "Shell session"
    assert summary["origin"] == "user"
    assert summary["created_at"] == (T0 + timedelta(seconds=-600)).isoformat()


def test_list_sessions_orders_newest_first(session_store, trigger_store):
    """Sessions list descending by created_at (the newest session offset first)."""
    bundle = SeedBundle.model_validate(_minimal_bundle())
    DemoSeeder(
        session_store=session_store, trigger_store=trigger_store, bundle=bundle, t0=T0
    ).seed()

    runtime = SessionRuntime(session_store=session_store)
    ids = [s["session_id"] for s in runtime.list_sessions()]
    # sess-shell starts at -600 (more recent) than sess-todos at -1200.
    assert ids == ["sess-shell", "sess-todos"]


# ---------------------------------------------------------------------------
# triggers
# ---------------------------------------------------------------------------


def test_triggers_persist_as_concrete_kinds(session_store, trigger_store):
    """A seeded trigger round-trips through the store as its concrete kind."""
    bundle = SeedBundle.model_validate(_minimal_bundle())
    DemoSeeder(
        session_store=session_store, trigger_store=trigger_store, bundle=bundle, t0=T0
    ).seed()

    trig = trigger_store.get("trig-cron")
    assert isinstance(trig, CronTrigger)
    assert trig.cron == "0 6 * * *"
    assert trig.wake_prompt == "Nightly digest"
    assert trig.session_id == "sess-shell"
    assert trig.created_at == T0 + timedelta(seconds=-300)


def test_webhook_trigger_from_canonical_bundle(session_store, trigger_store, key_store):
    """The canonical bundle's webhook trigger seeds with its pinned secret."""
    bundle = SeedBundle.model_validate(json.loads(_BUNDLE_PATH.read_text()))
    DemoSeeder(
        session_store=session_store,
        trigger_store=trigger_store,
        bundle=bundle,
        t0=T0,
        key_store=key_store,
    ).seed()

    trig = trigger_store.get("demo-trigger-ci-failure-watcher")
    assert isinstance(trig, WebhookTrigger)
    assert trig.secret == "demo-ci-failure-watcher-secret"
    assert trig.wake_prompt == "CI failure watcher"


# ---------------------------------------------------------------------------
# idempotency (byte-identical re-seed)
# ---------------------------------------------------------------------------


def test_reseed_is_byte_identical(session_store, trigger_store):
    """Seeding twice leaves an identical transcript (no duplicate events)."""
    bundle = SeedBundle.model_validate(_minimal_bundle())
    seeder = DemoSeeder(
        session_store=session_store, trigger_store=trigger_store, bundle=bundle, t0=T0
    )
    seeder.seed()
    first = session_store.load_transcript("sess-shell")
    seeder.seed()
    second = session_store.load_transcript("sess-shell")
    assert first == second


def test_reseed_updates_triggers_not_duplicates(session_store, trigger_store):
    """A re-seed replaces the trigger by id rather than colliding on the unique index."""
    bundle = SeedBundle.model_validate(_minimal_bundle())
    seeder = DemoSeeder(
        session_store=session_store, trigger_store=trigger_store, bundle=bundle, t0=T0
    )
    seeder.seed()
    seeder.seed()
    assert len(trigger_store.list()) == 1


# ---------------------------------------------------------------------------
# canonical fixture bundle
# ---------------------------------------------------------------------------


def test_canonical_bundle_validates_load_bearing_content():
    """console-poc.json loads fully-seated AND keeps every capture-flow anchor.

    The bundle grew to a max-usage instance (many sessions + triggers),
    but the strings the Playwright flows locate by (``shots.ts`` SEED) and the two
    original triggers must survive verbatim, or a capture silently anchors on the
    wrong row.
    """
    bundle = SeedBundle.model_validate(json.loads(_BUNDLE_PATH.read_text()))
    titles = [s.title for s in bundle.sessions]
    # Fully seated: many sessions, not the original three.
    assert len(bundle.sessions) >= 12
    for anchor in (
        "Refactor the auth middleware",
        "Weekly infra health report",
        "Plan a birthday dinner menu",
    ):
        assert anchor in titles
    # The two original triggers survive the expansion verbatim.
    wake_prompts = [t.wake_prompt for t in bundle.triggers]
    assert "Nightly repo digest" in wake_prompts
    assert "CI failure watcher" in wake_prompts
    assert len(bundle.triggers) >= 2


def test_canonical_session_a_edit_diff_is_expanded():
    """Session A's file-edit diff must exceed 8 changed lines (expanded DiffCard).

    The console only gives a diff the expanded treatment above 8 changed lines and
    the capture flow is calibrated for that, so guard the threshold as a contract.
    """
    bundle = SeedBundle.model_validate(json.loads(_BUNDLE_PATH.read_text()))
    session_a = next(
        s for s in bundle.sessions if s.id == "demo-refactor-auth-middleware"
    )
    edit = next(
        e
        for e in session_a.events
        if isinstance(e, FileEditToolEvent)
    )
    changed = [
        line
        for line in edit.diff.splitlines()
        if (line.startswith("+") and not line.startswith("+++"))
        or (line.startswith("-") and not line.startswith("---"))
    ]
    assert len(changed) > 8


def test_canonical_bundle_full_seed(session_store, trigger_store, key_store):
    """The whole canonical bundle seeds a fully-seated set of sessions + triggers."""
    bundle = SeedBundle.model_validate(json.loads(_BUNDLE_PATH.read_text()))
    report = DemoSeeder(
        session_store=session_store,
        trigger_store=trigger_store,
        bundle=bundle,
        t0=T0,
        key_store=key_store,
    ).seed()
    assert len(report.sessions) == len(bundle.sessions) >= 12
    assert len(report.triggers) == len(bundle.triggers) >= 2

    runtime = SessionRuntime(session_store=session_store)
    summaries = runtime.list_sessions()
    by_id = {s["session_id"]: s for s in summaries}
    # The flow-anchored sessions summarise as completed runs.
    for sid in (
        "demo-refactor-auth-middleware",
        "demo-weekly-infra-health-report",
        "demo-birthday-dinner-menu",
    ):
        assert by_id[sid]["status"] == "completed"
    # newest-first ordering: auth-refactor stays near the top (a couple newer
    # sessions ahead) so the front-page capture finds it without scrolling, and it
    # still precedes the infra report and dinner sessions it originally led.
    ids = [s["session_id"] for s in summaries]
    assert "demo-refactor-auth-middleware" in ids[:5]
    assert ids.index("demo-refactor-auth-middleware") < ids.index(
        "demo-weekly-infra-health-report"
    )
    assert ids.index("demo-weekly-infra-health-report") < ids.index(
        "demo-birthday-dinner-menu"
    )


def test_unit_shell_event_to_event_is_pure():
    """A union member's to_event is a pure (ts, model, agent_id, session_id) -> doc transform."""
    ev = ShellToolEvent(at_seconds=3, command="ls", exit_code=0, stdout="ok")
    ts = T0
    doc = ev.to_event(ts, session_model="m", agent_id="abc123", session_id="sess-abc123")
    assert doc["type"] == "tool_result"
    assert doc["ts"] == ts.isoformat()
    assert doc["payload"]["model"] == "m"
    assert doc["payload"]["agent_id"] == "abc123"
    assert json.loads(doc["payload"]["result"])["kind"] == "shell"


def test_unit_file_event_infers_total_lines():
    """file_read total_lines defaults to the line count of the numbered text."""
    ev = FileReadToolEvent(at_seconds=1, path="a.py", text="1\ta\n2\tb")
    doc = ev.to_event(T0, session_model="m", agent_id="x", session_id="sess-x")
    assert json.loads(doc["payload"]["result"])["total_lines"] == 2


def test_unit_file_edit_event_emits_diff():
    """file_edit produces a kind:"diff" result the console renders as a DiffCard."""
    ev = FileEditToolEvent(
        at_seconds=1, path="a.py", diff="--- a.py\n+++ a.py\n@@ -1 +1 @@\n-a\n+b"
    )
    doc = ev.to_event(T0, session_model="m", agent_id="x", session_id="sess-x")
    result = json.loads(doc["payload"]["result"])
    assert result == {
        "kind": "diff",
        "title": "a.py",
        "text": "--- a.py\n+++ a.py\n@@ -1 +1 @@\n-a\n+b",
        "files": ["a.py"],
    }
    assert doc["payload"]["success"] is True
