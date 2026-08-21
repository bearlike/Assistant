"""Tests for the Mewbo Apps domain contracts (mewbo_api.apps.models).

TDD: written before the implementation exists. Covers extra-field rejection
at every model, the ``AppSpec`` status-transition graph (legal moves +
illegal moves + the ``archived`` absorbing terminal + the same-status
no-op), ``AppFrontend``'s path-traversal/entrypoint validators, the
``PipelineRun`` ledger (open/record_write/close + the freshness
classmethod), and ``AppReadToken`` expiry — all with the clock passed as an
explicit method arg (``now=``), never patched.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jsonschema
import pytest
from mewbo_api.apps.models import (
    AppDataDoc,
    AppFrontend,
    AppPolicies,
    AppReadToken,
    AppReadyEvent,
    AppSpec,
    AppUpdatedEvent,
    AppVersion,
    CollectionSpec,
    CsvResult,
    FailureBudget,
    JsonResult,
    PipelineIssue,
    PipelineRun,
    PipelineSpec,
    TextResult,
    WorkspaceRef,
    XmlResult,
)
from pydantic import ValidationError

NOW = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)


def _frontend(**overrides: object) -> AppFrontend:
    defaults: dict[str, object] = dict(
        entrypoint="app.py",
        files={"app.py": "import streamlit as st\n"},
    )
    defaults.update(overrides)
    return AppFrontend(**defaults)  # type: ignore[arg-type]


def _app_spec(**overrides: object) -> AppSpec:
    defaults: dict[str, object] = dict(
        app_id="app-1",
        title="Inbox Triage",
        summary="Groups incoming email into tasks.",
        owner_session_id="s1",
        workspace_ref=WorkspaceRef(kind="own", key="s1"),
        frontend=_frontend(),
        created_at=NOW,
        updated_at=NOW,
    )
    defaults.update(overrides)
    return AppSpec(**defaults)  # type: ignore[arg-type]


def _open_run(**overrides: object) -> PipelineRun:
    defaults: dict[str, object] = dict(
        run_key="r1", app_id="app-1", pipeline_name="ingest", now=NOW
    )
    defaults.update(overrides)
    return PipelineRun.open(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# extra="forbid" at every model
# ---------------------------------------------------------------------------


def test_app_spec_extra_field_forbidden():
    with pytest.raises(ValidationError):
        _app_spec(bogus="nope")


def test_workspace_ref_extra_field_forbidden():
    with pytest.raises(ValidationError):
        WorkspaceRef(kind="own", key="s1", bogus="nope")


def test_collection_spec_extra_field_forbidden():
    with pytest.raises(ValidationError):
        CollectionSpec(name="emails", json_schema={"type": "object"}, bogus="nope")


def test_pipeline_spec_extra_field_forbidden():
    with pytest.raises(ValidationError):
        PipelineSpec(name="ingest", wake_prompt="go", bogus="nope")


def test_app_policies_extra_field_forbidden():
    with pytest.raises(ValidationError):
        AppPolicies(bogus="nope")


def test_app_frontend_extra_field_forbidden():
    with pytest.raises(ValidationError):
        AppFrontend(entrypoint="app.py", files={"app.py": "x"}, bogus="nope")


def test_pipeline_run_extra_field_forbidden():
    with pytest.raises(ValidationError):
        PipelineRun(
            run_key="r1",
            app_id="app-1",
            pipeline_name="ingest",
            started_at=NOW,
            bogus="nope",
        )


def test_app_data_doc_extra_field_forbidden():
    with pytest.raises(ValidationError):
        AppDataDoc(
            app_id="app-1",
            collection="emails",
            key="msg-1",
            doc={},
            updated_at=NOW,
            bogus="nope",
        )


def test_app_read_token_extra_field_forbidden():
    with pytest.raises(ValidationError):
        AppReadToken(token_id="t1", app_id="app-1", expires_at=NOW, bogus="nope")


def test_app_version_extra_field_forbidden():
    with pytest.raises(ValidationError):
        AppVersion(
            app_id="app-1",
            version=1,
            spec=_app_spec(),
            author="builder",
            bogus="nope",
        )


def test_app_ready_event_extra_field_forbidden():
    with pytest.raises(ValidationError):
        AppReadyEvent(
            app_id="app-1", title="t", summary="s", version=1, bogus="nope"
        )


def test_app_updated_event_extra_field_forbidden():
    with pytest.raises(ValidationError):
        AppUpdatedEvent(app_id="app-1", version=2, author="repair", bogus="nope")


# ---------------------------------------------------------------------------
# Slug validation (CollectionSpec.name / PipelineSpec.name)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["Emails", "e mail", "", "-emails", "emails-", "em@il"])
def test_collection_spec_rejects_invalid_slug(name):
    with pytest.raises(ValidationError):
        CollectionSpec(name=name, json_schema={"type": "object"})


def test_collection_spec_accepts_valid_slug():
    cs = CollectionSpec(name="emails_v2", json_schema={"type": "object"})
    assert cs.name == "emails_v2"


@pytest.mark.parametrize("name", ["Ingest", "in gest", ""])
def test_pipeline_spec_rejects_invalid_slug(name):
    with pytest.raises(ValidationError):
        PipelineSpec(name=name, wake_prompt="go")


def test_pipeline_spec_defaults():
    # A pipeline must declare HOW it runs — on_demand here.
    ps = PipelineSpec(name="ingest", wake_prompt="go check inbox", on_demand=True)
    assert ps.trigger_ref is None
    assert ps.schedule is None
    assert ps.on_demand is True
    assert ps.tools_allowlist == []
    assert ps.cursor == {}
    assert ps.writes == ()


def test_app_spec_rejects_pipeline_write_to_undeclared_collection():
    with pytest.raises(ValidationError, match="unknown collection"):
        _app_spec(
            pipelines=[
                PipelineSpec(name="ingest", wake_prompt="go", on_demand=True, writes=("records",))
            ]
        )


def test_app_spec_accepts_pipeline_write_to_declared_collection():
    spec = _app_spec(
        collections=[CollectionSpec(name="records", json_schema={"type": "object"})],
        pipelines=[
            PipelineSpec(name="ingest", wake_prompt="go", on_demand=True, writes=("records",))
        ],
    )
    assert spec.pipelines[0].writes == ("records",)


# ---------------------------------------------------------------------------
# AppFrontend — path traversal + entrypoint-present validators
# ---------------------------------------------------------------------------


def test_app_frontend_rejects_absolute_path():
    with pytest.raises(ValidationError):
        AppFrontend(entrypoint="app.py", files={"/etc/passwd": "x", "app.py": "y"})


def test_app_frontend_rejects_parent_traversal():
    with pytest.raises(ValidationError):
        AppFrontend(entrypoint="app.py", files={"../secrets.py": "x", "app.py": "y"})


def test_app_frontend_rejects_embedded_traversal_segment():
    with pytest.raises(ValidationError):
        AppFrontend(
            entrypoint="app.py",
            files={"pages/../../escape.py": "x", "app.py": "y"},
        )


def test_app_frontend_rejects_windows_drive_path():
    with pytest.raises(ValidationError):
        AppFrontend(entrypoint="app.py", files={"C:/evil.py": "x", "app.py": "y"})


def test_app_frontend_rejects_empty_files():
    with pytest.raises(ValidationError):
        AppFrontend(entrypoint="app.py", files={})


def test_app_frontend_requires_entrypoint_present():
    with pytest.raises(ValidationError):
        AppFrontend(entrypoint="app.py", files={"other.py": "x"})


def test_app_frontend_accepts_nested_relative_paths():
    fe = AppFrontend(entrypoint="app.py", files={"app.py": "x", "pages/sub.py": "y"})
    assert "pages/sub.py" in fe.files


def test_app_frontend_defaults():
    fe = _frontend()
    assert fe.entrypoint == "app.py"
    assert fe.requirements == []


# ---------------------------------------------------------------------------
# WorkspaceRef
# ---------------------------------------------------------------------------


def test_workspace_ref_valid_kinds():
    ref = WorkspaceRef(kind="shared", key="proj-42")
    assert ref.kind == "shared"


def test_workspace_ref_rejects_invalid_kind():
    with pytest.raises(ValidationError):
        WorkspaceRef(kind="bogus", key="proj-42")


# ---------------------------------------------------------------------------
# AppPolicies
# ---------------------------------------------------------------------------


def test_app_policies_defaults():
    p = AppPolicies()
    assert p.on_pipeline_failure == "notify"
    assert p.retention_days is None
    assert p.max_docs_per_collection == 50_000


def test_app_policies_rejects_non_positive_retention_days():
    with pytest.raises(ValidationError):
        AppPolicies(retention_days=0)


def test_app_policies_rejects_non_positive_max_docs():
    with pytest.raises(ValidationError):
        AppPolicies(max_docs_per_collection=0)


# ---------------------------------------------------------------------------
# AppSpec — construction, defaults, tz-aware timestamps
# ---------------------------------------------------------------------------


def test_app_spec_defaults():
    app = _app_spec()
    assert app.status == "draft"
    assert app.version == 1
    assert app.maintainer_session_id is None
    assert isinstance(app.policies, AppPolicies)
    assert app.collections == []
    assert app.pipelines == []


def test_app_spec_rejects_colon_in_app_id():
    # A colon breaks render-token parsing (`<app_id>:<exp>:<nonce>:<sig>`) — a
    # self-DoS the tokens docstring asserts against; enforce it at the model.
    with pytest.raises(ValidationError, match="must not contain ':'"):
        _app_spec(app_id="app:evil")


def test_app_spec_accepts_normal_hyphenated_app_id():
    assert _app_spec(app_id="app-1a2b3c4d5e6f").app_id == "app-1a2b3c4d5e6f"


def test_app_spec_rejects_naive_created_at():
    with pytest.raises(ValidationError):
        _app_spec(created_at=datetime(2026, 7, 17, 12, 0, 0))


def test_app_spec_rejects_naive_updated_at():
    with pytest.raises(ValidationError):
        _app_spec(updated_at=datetime(2026, 7, 17, 12, 0, 0))


# ---------------------------------------------------------------------------
# AppSpec.transition — the status graph
# ---------------------------------------------------------------------------


def test_app_spec_legal_transition_draft_to_building():
    app = _app_spec()
    app.transition("building", now=NOW + timedelta(seconds=1))
    assert app.status == "building"
    assert app.updated_at == NOW + timedelta(seconds=1)


def test_app_spec_legal_transition_chain_to_live():
    app = _app_spec()
    app.transition("building", now=NOW)
    app.transition("live", now=NOW)
    assert app.status == "live"


def test_app_spec_illegal_transition_draft_to_live_direct():
    app = _app_spec()
    with pytest.raises(ValueError):
        app.transition("live", now=NOW)


def test_app_spec_illegal_transition_out_of_archived():
    app = _app_spec(status="archived")
    with pytest.raises(ValueError):
        app.transition("live", now=NOW)


def test_app_spec_archived_self_transition_is_still_a_noop():
    """Same-status is a no-op even from the absorbing terminal."""
    app = _app_spec(status="archived")
    app.transition("archived", now=NOW)
    assert app.status == "archived"


def test_app_spec_noop_transition_to_same_status_leaves_updated_at():
    app = _app_spec(status="live")
    before = app.updated_at
    app.transition("live", now=NOW + timedelta(hours=1))
    assert app.status == "live"
    assert app.updated_at == before


def test_app_spec_broken_can_recover_to_live_via_repair():
    app = _app_spec(status="broken")
    app.transition("live", now=NOW)
    assert app.status == "live"


def test_app_spec_live_can_pause_and_resume():
    app = _app_spec(status="live")
    app.transition("paused", now=NOW)
    assert app.status == "paused"
    app.transition("live", now=NOW)
    assert app.status == "live"


def test_app_spec_paused_cannot_go_broken_directly():
    app = _app_spec(status="paused")
    with pytest.raises(ValueError):
        app.transition("broken", now=NOW)


def test_app_spec_any_non_archived_status_can_archive():
    for status in ("draft", "building", "live", "paused", "broken"):
        app = _app_spec(status=status)
        app.transition("archived", now=NOW)
        assert app.status == "archived"


# ---------------------------------------------------------------------------
# PipelineRun — open / record_write / close ledger + freshness
# ---------------------------------------------------------------------------


def test_pipeline_run_open_starts_running():
    run = _open_run()
    assert run.status == "running"
    assert run.started_at == NOW
    assert run.ended_at is None
    assert run.docs_written == {}


def test_pipeline_run_record_write_accumulates_per_collection():
    run = _open_run()
    run.record_write("emails", 3)
    run.record_write("emails", 2)
    run.record_write("tasks", 1)
    assert run.docs_written == {"emails": 5, "tasks": 1}


def test_pipeline_run_record_write_default_count_is_one():
    run = _open_run()
    run.record_write("emails")
    assert run.docs_written == {"emails": 1}


def test_pipeline_run_record_write_rejects_non_positive_count():
    run = _open_run()
    with pytest.raises(ValueError):
        run.record_write("emails", 0)


def test_pipeline_run_record_write_after_close_rejected():
    run = _open_run()
    run.close(now=NOW, status="succeeded")
    with pytest.raises(ValueError):
        run.record_write("emails", 1)


def test_pipeline_run_close_succeeded():
    run = _open_run()
    later = NOW + timedelta(minutes=5)
    run.close(now=later, status="succeeded", cursor_after={"offset": 42})
    assert run.status == "succeeded"
    assert run.ended_at == later
    assert run.cursor_after == {"offset": 42}
    assert run.error is None


def test_pipeline_run_close_failed_requires_error():
    run = _open_run()
    with pytest.raises(ValueError):
        run.close(now=NOW, status="failed")


def test_pipeline_run_close_failed_with_error():
    run = _open_run()
    run.close(now=NOW, status="failed", error="connector timed out")
    assert run.status == "failed"
    assert run.error == "connector timed out"


def test_pipeline_run_close_already_closed_rejected():
    run = _open_run()
    run.close(now=NOW, status="succeeded")
    with pytest.raises(ValueError):
        run.close(now=NOW, status="succeeded")


def test_pipeline_run_freshness_none_when_never_succeeded():
    runs = [_open_run()]
    assert PipelineRun.freshness(runs, now=NOW + timedelta(hours=1)) is None


def test_pipeline_run_freshness_empty_list_is_none():
    assert PipelineRun.freshness([], now=NOW) is None


def test_pipeline_run_freshness_returns_age_of_latest_success():
    r1 = _open_run(run_key="r1")
    r1.close(now=NOW + timedelta(minutes=1), status="succeeded")
    r2 = _open_run(run_key="r2", now=NOW + timedelta(hours=2))
    r2.close(now=NOW + timedelta(hours=2, minutes=1), status="succeeded")
    check_at = NOW + timedelta(hours=3)
    age = PipelineRun.freshness([r1, r2], now=check_at)
    assert age == check_at - (NOW + timedelta(hours=2, minutes=1))


def test_pipeline_run_freshness_ignores_failed_runs():
    run = _open_run()
    run.close(now=NOW + timedelta(minutes=1), status="failed", error="boom")
    assert PipelineRun.freshness([run], now=NOW + timedelta(hours=1)) is None


# ---------------------------------------------------------------------------
# PipelineRun.cooldown_remaining — the manual-agentic-fire anti-spam guard
# ---------------------------------------------------------------------------


def test_cooldown_remaining_none_when_no_runs():
    assert PipelineRun.cooldown_remaining([], now=NOW, cooldown_seconds=300) is None


def test_cooldown_remaining_none_when_cooldown_disabled():
    run = _open_run(now=NOW)  # started right now, but a 0 cooldown never cools down
    assert PipelineRun.cooldown_remaining([run], now=NOW, cooldown_seconds=0) is None


def test_cooldown_remaining_within_window_returns_seconds_left():
    # Started 100s ago; a 300s cooldown leaves 200s (any kind/status counts).
    run = _open_run(now=NOW - timedelta(seconds=100))
    run.close(now=NOW - timedelta(seconds=90), status="succeeded")
    assert PipelineRun.cooldown_remaining([run], now=NOW, cooldown_seconds=300) == 200


def test_cooldown_remaining_rounds_up_partial_seconds():
    # 100.4s elapsed -> 199.6s left -> rounds UP to 200 so retry_after never under-promises.
    run = _open_run(now=NOW - timedelta(seconds=100, milliseconds=400))
    assert PipelineRun.cooldown_remaining([run], now=NOW, cooldown_seconds=300) == 200


def test_cooldown_remaining_none_past_the_window():
    run = _open_run(now=NOW - timedelta(seconds=400))
    assert PipelineRun.cooldown_remaining([run], now=NOW, cooldown_seconds=300) is None


def test_cooldown_remaining_keys_on_the_most_recent_run_by_started_at():
    old = _open_run(run_key="old", now=NOW - timedelta(seconds=1000))
    recent = _open_run(run_key="recent", now=NOW - timedelta(seconds=50))
    # The recent run (any status — still running here) drives the cooldown.
    assert PipelineRun.cooldown_remaining([old, recent], now=NOW, cooldown_seconds=300) == 250


# ---------------------------------------------------------------------------
# PipelineRun.wrote_nothing — succeeded-but-wrote-nothing honesty
# ---------------------------------------------------------------------------


def test_pipeline_run_wrote_nothing_true_when_succeeded_with_no_writes():
    run = _open_run()
    run.close(now=NOW, status="succeeded")
    assert run.wrote_nothing is True


def test_pipeline_run_wrote_nothing_false_when_succeeded_with_writes():
    run = _open_run()
    run.record_write("emails", 2)
    run.close(now=NOW, status="succeeded")
    assert run.wrote_nothing is False


def test_pipeline_run_wrote_nothing_false_when_failed():
    run = _open_run()
    run.close(now=NOW, status="failed", error="boom")
    assert run.wrote_nothing is False  # not the honesty gap this flag targets


def test_pipeline_run_wrote_nothing_false_while_still_running():
    run = _open_run()
    assert run.wrote_nothing is False


# ---------------------------------------------------------------------------
# PipelineRun.unwritten_collections — the OTHER honesty gap wrote_nothing misses
# ---------------------------------------------------------------------------


def test_unwritten_collections_reports_a_declared_collection_never_written():
    # The exact incident shape: the run wrote real docs to "today_digest" but
    # never touched the declared "records" collection — wrote_nothing alone
    # can't see this (docs_written isn't empty).
    run = _open_run()
    run.record_write("today_digest", 1)
    run.close(now=NOW, status="succeeded")
    assert run.wrote_nothing is False
    assert run.unwritten_collections(["records", "today_digest"]) == ["records"]


def test_unwritten_collections_empty_when_every_declared_collection_was_written():
    run = _open_run()
    run.record_write("records", 3)
    run.close(now=NOW, status="succeeded")
    assert run.unwritten_collections(["records"]) == []


def test_unwritten_collections_all_declared_when_nothing_written():
    run = _open_run()
    run.close(now=NOW, status="succeeded")  # no writes recorded (wrote_nothing case)
    assert run.unwritten_collections(["records", "today_digest"]) == ["records", "today_digest"]


def test_unwritten_collections_empty_when_the_app_declares_no_collections():
    run = _open_run()
    run.close(now=NOW, status="succeeded")
    assert run.unwritten_collections([]) == []


def test_unwritten_collections_false_when_failed():
    # Not the honesty gap this signal targets — a failed run's own status
    # already says enough; stale/error surface it, not this field.
    run = _open_run()
    run.close(now=NOW, status="failed", error="boom")
    assert run.unwritten_collections(["records"]) == []


def test_unwritten_collections_false_while_still_running():
    run = _open_run()
    assert run.unwritten_collections(["records"]) == []


# ---------------------------------------------------------------------------
# Result specs + render tier
# ---------------------------------------------------------------------------


class TestResultSpecs:
    def test_json_validates_and_renders_json(self):
        spec = JsonResult(json_schema={"type": "object", "required": ["title"]})
        output = {"title": "Digest"}
        spec.validate_output(output)
        assert spec.render(output) == ('{"title": "Digest"}', "application/json")
        with pytest.raises(ValueError, match="JSON result"):
            spec.validate_output({})

    def test_csv_validates_declared_columns_and_renders_them_in_order(self):
        spec = CsvResult(columns=["title", "count"])
        output = [{"count": 2, "title": "Digest", "ignored": "x"}]
        spec.validate_output(output)
        assert spec.render(output) == ("title,count\r\nDigest,2\r\n", "text/csv")
        with pytest.raises(ValueError, match="missing column 'count'"):
            spec.validate_output([{"title": "Digest"}])

    def test_xml_validates_mapping_and_list_and_renders_each_shape(self):
        spec = XmlResult(root="items", item="item")
        mapping = {"title": "Digest"}
        rows = [{"title": "First"}, {"title": "Second"}]
        spec.validate_output(mapping)
        spec.validate_output(rows)
        assert spec.render(mapping) == ("<items><title>Digest</title></items>", "application/xml")
        assert spec.render(rows) == (
            "<items><item><title>First</title></item><item><title>Second</title></item></items>",
            "application/xml",
        )
        with pytest.raises(ValueError, match="mapping or a list"):
            spec.validate_output("not XML data")

    def test_text_validates_and_renders_plain_text(self):
        spec = TextResult()
        spec.validate_output("ready")
        assert spec.render("ready") == ("ready", "text/plain")
        with pytest.raises(ValueError, match="must be a string"):
            spec.validate_output({"body": "ready"})


def test_render_tier_requires_code_and_a_result_and_does_not_expect_writes():
    with pytest.raises(ValidationError, match="not mode='code'"):
        PipelineSpec(
            name="p", wake_prompt="w", on_demand=True, tier="render", result={"media": "text"}
        )
    with pytest.raises(ValidationError, match="declares no `result`"):
        PipelineSpec(
            name="p", wake_prompt="w", on_demand=True, mode="code", entrypoint="pipelines/p.py",
            tier="render",
        )
    pipeline = PipelineSpec(
        name="p", wake_prompt="w", on_demand=True, mode="code", entrypoint="pipelines/p.py",
        tier="render", result={"media": "text"},
    )
    assert pipeline.expects_writes() is False


# ---------------------------------------------------------------------------
# Pipeline failure budget
# ---------------------------------------------------------------------------


def _failed_run(key: str, *, started_at: datetime) -> PipelineRun:
    run = PipelineRun.open(run_key=key, app_id="app-1", pipeline_name="ingest", now=started_at)
    run.close(now=started_at, status="failed", error="boom")
    return run


def test_failure_budget_dispatches_exactly_at_the_threshold_and_not_afterward():
    budget = FailureBudget(consecutive_failures=2, window_seconds=300)
    newest = _failed_run("new", started_at=NOW)
    older = _failed_run("old", started_at=NOW - timedelta(seconds=1))
    oldest = _failed_run("older", started_at=NOW - timedelta(seconds=2))
    assert PipelineRun.should_dispatch_failure([newest], budget, now=NOW) is False
    assert PipelineRun.should_dispatch_failure([newest, older], budget, now=NOW) is True
    assert PipelineRun.should_dispatch_failure([newest, older, oldest], budget, now=NOW) is False


def test_failure_budget_resets_on_success_and_ignores_failures_outside_its_window():
    budget = FailureBudget(consecutive_failures=2, window_seconds=60)
    failed = _failed_run("failed", started_at=NOW - timedelta(seconds=1))
    success = _open_run(run_key="success", now=NOW)
    success.close(now=NOW, status="succeeded")
    stale = _failed_run("stale", started_at=NOW - timedelta(seconds=61))
    assert PipelineRun.should_dispatch_failure([success, failed], budget, now=NOW) is False
    assert PipelineRun.should_dispatch_failure([failed, stale], budget, now=NOW) is False


# ---------------------------------------------------------------------------
# AppReadToken — expiry with injected NOW
# ---------------------------------------------------------------------------


def test_app_read_token_valid_before_expiry():
    token = AppReadToken(token_id="t1", app_id="app-1", expires_at=NOW + timedelta(minutes=5))
    assert token.is_valid(NOW) is True


def test_app_read_token_invalid_after_expiry():
    token = AppReadToken(token_id="t1", app_id="app-1", expires_at=NOW + timedelta(minutes=5))
    assert token.is_valid(NOW + timedelta(minutes=6)) is False


def test_app_read_token_invalid_at_exact_expiry():
    expires = NOW + timedelta(minutes=5)
    token = AppReadToken(token_id="t1", app_id="app-1", expires_at=expires)
    assert token.is_valid(expires) is False


def test_app_read_token_rejects_naive_expiry():
    with pytest.raises(ValidationError):
        AppReadToken(token_id="t1", app_id="app-1", expires_at=datetime(2026, 7, 17, 12, 0, 0))


def test_app_read_token_default_scope_is_read():
    token = AppReadToken(token_id="t1", app_id="app-1", expires_at=NOW + timedelta(minutes=5))
    assert token.scope == "read"


# ---------------------------------------------------------------------------
# AppVersion + wire events + AppDataDoc
# ---------------------------------------------------------------------------


def test_app_version_snapshot_round_trip():
    app = _app_spec()
    version = AppVersion(
        app_id=app.app_id, version=1, spec=app, author="builder", note="initial build"
    )
    assert version.spec.title == "Inbox Triage"
    assert version.author == "builder"


def test_app_version_rejects_non_positive_version():
    with pytest.raises(ValidationError):
        AppVersion(app_id="app-1", version=0, spec=_app_spec(), author="builder")


def test_app_ready_event_shape():
    event = AppReadyEvent(app_id="app-1", title="Inbox Triage", summary="...", version=1)
    assert event.version == 1


def test_app_updated_event_shape():
    event = AppUpdatedEvent(app_id="app-1", version=2, author="repair")
    assert event.author == "repair"


def test_app_data_doc_round_trip():
    doc = AppDataDoc(
        app_id="app-1", collection="emails", key="msg-1", doc={"subject": "hi"}, updated_at=NOW
    )
    assert doc.doc["subject"] == "hi"


# ---------------------------------------------------------------------------
# CollectionSpec.validate_doc — schema-owned validation
# ---------------------------------------------------------------------------


def test_collection_spec_validate_doc_accepts_matching_schema():
    cs = CollectionSpec(
        name="emails",
        json_schema={
            "type": "object",
            "required": ["subject"],
            "properties": {"subject": {"type": "string"}},
        },
    )
    cs.validate_doc({"subject": "hi"})  # must not raise


def test_collection_spec_validate_doc_rejects_mismatch():
    cs = CollectionSpec(
        name="emails",
        json_schema={"type": "object", "required": ["subject"]},
    )
    with pytest.raises(jsonschema.ValidationError):
        cs.validate_doc({})


# ---------------------------------------------------------------------------
# The integrity signal that drives auto-repair: which collections a SUCCEEDED
# run regressed, edge-triggered so one break dispatches once.
# ---------------------------------------------------------------------------

_BASE = datetime(2026, 5, 4, 8, 0, 0, tzinfo=timezone.utc)


def _ledger_run(key, writes, *, minute, status="succeeded", cache=None):
    """A closed ledger row for pipeline ``p`` with *writes* recorded."""
    at = _BASE + timedelta(minutes=minute)
    run = PipelineRun.open(run_key=key, app_id="app-x", pipeline_name="p", now=at)
    for name, count in writes.items():
        run.record_write(name, count)
    if status != "running":
        run.close(
            now=at,
            status=status,
            error="boom" if status == "failed" else None,
            cache=cache,
        )
    return run


class TestNewIntegrityViolations:
    """Regressed-vs-never-populated, and the edge trigger that bounds the reaction."""

    def test_first_ever_run_reports_nothing(self):
        # No baseline to regress FROM — an app whose first run writes nothing may
        # simply have an empty upstream, which must never trigger a repair.
        run = _ledger_run("r1", {}, minute=0)
        assert run.new_integrity_violations(["records"], prior_runs=[]) == []

    def test_never_populated_collection_is_not_a_regression(self):
        # 'records' has never been written by any run of this pipeline, so a run
        # that again writes nothing to it is the status quo, not a break.
        prior = _ledger_run("r1", {"digest": 3}, minute=0)
        run = _ledger_run("r2", {"digest": 3}, minute=10)
        assert run.new_integrity_violations(["records", "digest"], prior_runs=[prior]) == []

    def test_declared_output_is_reported_on_the_first_empty_run(self):
        # The bootstrap hole: no previous run could establish the historical
        # baseline, but this pipeline expressly promises to materialize 'records'.
        run = _ledger_run("r1", {}, minute=0)
        assert run.new_integrity_violations(
            ["records"], prior_runs=[], expected_writes=["records"]
        ) == ["records"]

    def test_declared_empty_output_is_edge_triggered(self):
        r1 = _ledger_run("r1", {}, minute=0)
        r2 = _ledger_run("r2", {}, minute=10)
        assert r1.new_integrity_violations(
            ["records"], prior_runs=[], expected_writes=["records"]
        ) == ["records"]
        assert r2.new_integrity_violations(
            ["records"], prior_runs=[r1], expected_writes=["records"]
        ) == []

    def test_declared_output_stays_silent_for_a_cache_hit(self):
        run = _ledger_run("r1", {}, minute=0, cache="hit")
        assert run.new_integrity_violations(
            ["records"], prior_runs=[], expected_writes=["records"]
        ) == []

    def test_regressed_collection_is_reported(self):
        prior = _ledger_run("r1", {"records": 5}, minute=0)
        run = _ledger_run("r2", {}, minute=10)
        assert run.new_integrity_violations(["records"], prior_runs=[prior]) == ["records"]

    def test_partial_miss_is_reported_while_the_written_sibling_is_not(self):
        # The exact live shape: one collection keeps filling, the one every page
        # reads goes empty, and the run still closes green.
        prior = _ledger_run("r1", {"records": 5, "digest": 1}, minute=0)
        run = _ledger_run("r2", {"digest": 1}, minute=10)
        got = run.new_integrity_violations(["records", "digest"], prior_runs=[prior])
        assert got == ["records"]

    def test_an_ongoing_violation_is_reported_only_once(self):
        # THE anti-spam bound: run 2 already reported 'records'; run 3 must stay
        # silent rather than dispatch a repair on every fire, forever.
        r1 = _ledger_run("r1", {"records": 5}, minute=0)
        r2 = _ledger_run("r2", {}, minute=10)
        r3 = _ledger_run("r3", {}, minute=20)
        assert r2.new_integrity_violations(["records"], prior_runs=[r1]) == ["records"]
        assert r3.new_integrity_violations(["records"], prior_runs=[r1, r2]) == []

    def test_a_new_regression_alongside_an_ongoing_one_is_reported(self):
        # 'records' is already known-broken and stays silent; 'digest' just broke
        # and must still get through — suppression is per-collection, not per-run.
        r1 = _ledger_run("r1", {"records": 5, "digest": 1}, minute=0)
        r2 = _ledger_run("r2", {"digest": 1}, minute=10)
        r3 = _ledger_run("r3", {}, minute=20)
        got = r3.new_integrity_violations(["records", "digest"], prior_runs=[r1, r2])
        assert got == ["digest"]

    def test_the_edge_re_arms_after_a_recovery(self):
        # Broke, was repaired, broke again — the second break is a NEW episode and
        # must dispatch again, or a fixed-then-refroken pipeline goes unreported.
        r1 = _ledger_run("r1", {"records": 5}, minute=0)
        r2 = _ledger_run("r2", {}, minute=10)  # broke (reported)
        r3 = _ledger_run("r3", {"records": 5}, minute=20)  # recovered
        r4 = _ledger_run("r4", {}, minute=30)  # broke again
        assert r4.new_integrity_violations(["records"], prior_runs=[r1, r2, r3]) == ["records"]

    def test_a_cache_hit_run_reports_nothing(self):
        # A hit writes nothing BY DESIGN; counting it would report every watched
        # collection as regressed on every cached invoke.
        prior = _ledger_run("r1", {"records": 5}, minute=0)
        run = _ledger_run("r2", {}, minute=10, cache="hit")
        assert run.new_integrity_violations(["records"], prior_runs=[prior]) == []

    def test_a_cache_hit_is_not_used_as_the_comparison_point(self):
        # A hit sitting between two real runs must not MASK a genuine regression
        # by looking like a run that already reported it.
        r1 = _ledger_run("r1", {"records": 5}, minute=0)
        r2 = _ledger_run("r2", {}, minute=10, cache="hit")
        r3 = _ledger_run("r3", {}, minute=20, cache="miss")
        assert r3.new_integrity_violations(["records"], prior_runs=[r1, r2]) == ["records"]

    def test_a_failed_run_reports_nothing(self):
        # Its own status already says enough; the failure policy handles it.
        prior = _ledger_run("r1", {"records": 5}, minute=0)
        run = _ledger_run("r2", {}, minute=10, status="failed")
        assert run.new_integrity_violations(["records"], prior_runs=[prior]) == []

    def test_an_undeclared_collection_is_never_reported(self):
        prior = _ledger_run("r1", {"scratch": 5}, minute=0)
        run = _ledger_run("r2", {}, minute=10)
        assert run.new_integrity_violations(["records"], prior_runs=[prior]) == []

    def test_self_is_excluded_when_the_caller_passes_the_whole_ledger(self):
        # The store's list_runs includes the just-saved run; it must not become
        # its own baseline (which would make every run look self-consistent).
        prior = _ledger_run("r1", {"records": 5}, minute=0)
        run = _ledger_run("r2", {}, minute=10)
        assert run.new_integrity_violations(["records"], prior_runs=[run, prior]) == ["records"]


class TestPipelineIssue:
    def test_a_failure_issue_carries_the_error_verbatim(self):
        issue = PipelineIssue.run_failed("p", "collection 'tasks' rejected the doc")
        assert issue.describe() == "collection 'tasks' rejected the doc"
        assert "FAILED" in issue.repair_brief()

    def test_a_failure_issue_without_an_error_still_reads_honestly(self):
        assert PipelineIssue.run_failed("p", None).describe()

    def test_an_integrity_issue_never_claims_the_run_failed(self):
        # The repair agent is told the run SUCCEEDED, so it does not go hunting
        # for an exception that never happened.
        brief = PipelineIssue.unwritten("p", ["records"]).repair_brief()
        assert "SUCCEEDED" in brief
        assert "raised no exception" in brief
        assert "records" in brief

    def test_only_an_integrity_issue_needs_its_own_event(self):
        # A failure is already visible as a failed ledger row; an integrity
        # violation's run row is green, so the event is its only push signal.
        assert PipelineIssue.unwritten("p", ["records"]).needs_own_event is True
        assert PipelineIssue.run_failed("p", "boom").needs_own_event is False
