"""HTTP contract tests for the trigger REST surface (WP3).

Exercises the FROZEN v1 routes the console is built against: arm/list/pause/
resume/cancel, the unauthenticated webhook capability URL, and the terminate
cascade. Swaps the module stores to tmp-backed instances (mirrors
``test_backend_terminate._reset_backend``) and wires the trigger service with a
FAKE deliver so a webhook fire never starts a real orchestration run.
"""

# mypy: ignore-errors

from mewbo_api import backend
from mewbo_api.triggers import routes as trigger_routes
from mewbo_api.triggers.service import TriggerFireContext, TriggerService
from mewbo_core.config import TriggersConfig
from mewbo_core.session_store import SessionStore
from mewbo_core.triggers.policy import TriggerPolicy
from mewbo_core.triggers.store import JsonTriggerStore


class RecordingDeliver:
    """Fake ``deliver`` — records each ``TriggerFireContext`` call, reports delivered."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []

    def __call__(self, ctx: TriggerFireContext) -> bool:
        self.calls.append((ctx.session_id, ctx.wake, ctx.action, ctx.trigger_id))
        return True


def _setup(tmp_path, *, policy: TriggerPolicy | None = None):
    """Point the runtime + trigger routes/service at fresh tmp-backed stores.

    The routes are DI'd through the ONE ``TriggerRoutesController`` the import-
    time ``init_trigger_routes`` registered (no module-level collaborator
    globals anymore) — so a test points the single
    registered controller at fresh stores by reassigning its fields.
    """
    backend.session_store = SessionStore(root_dir=str(tmp_path))
    backend.runtime = backend.SessionRuntime(session_store=backend.session_store)
    store = JsonTriggerStore(data_file=str(tmp_path / "triggers.json"))
    pol = policy or TriggerPolicy()
    deliver = RecordingDeliver()
    service = TriggerService(
        runtime=backend.runtime,
        store=store,
        policy=pol,
        config=TriggersConfig(),
        forge_client_factory=lambda repo: None,
        deliver=deliver,
    )
    controller = trigger_routes._controller
    assert controller is not None  # set by init_trigger_routes at backend import
    controller.service = service
    controller.store = store
    controller.policy = pol
    controller.runtime = backend.runtime
    # Cascade so the /terminate endpoint cancels this session's triggers.
    backend.runtime.register_on_terminate(lambda sid: store.cancel_for_session(sid))
    return store, pol, deliver


def _session() -> str:
    return backend.runtime.resolve_session()


def _arm(client, auth_headers, sid, body):
    return client.post(f"/api/sessions/{sid}/triggers", headers=auth_headers, json=body)


def _arm_id(client, auth_headers, sid, body):
    return _arm(client, auth_headers, sid, body).get_json()["id"]


WEBHOOK = {"kind": "webhook", "wake_prompt": "a"}
CRON = {"kind": "time.cron", "wake_prompt": "b", "cron": "0 9 * * *"}


# ---------------------------------------------------------------------------
# Arm
# ---------------------------------------------------------------------------


class TestArm:
    def test_arm_returns_201_dto(self, client, auth_headers, tmp_path):
        _setup(tmp_path)
        sid = _session()
        resp = _arm(
            client, auth_headers, sid,
            {"kind": "time.cron", "wake_prompt": "nightly", "cron": "0 9 * * *"},
        )
        assert resp.status_code == 201
        dto = resp.get_json()
        assert dto["session_id"] == sid
        assert dto["kind"] == "time.cron"
        assert dto["status"] == "armed"
        assert dto["created_by"] == "user"
        assert dto["wake_prompt"] == "nightly"
        assert dto["args"]["cron"] == "0 9 * * *"
        assert dto["next_fire_at"]  # computed on the way out
        assert "cron" not in dto  # kind config is nested under args, never top-level

    def test_arm_webhook_includes_secret_on_create_but_not_in_list(
        self, client, auth_headers, tmp_path
    ):
        _setup(tmp_path)
        sid = _session()
        dto = _arm(client, auth_headers, sid, {"kind": "webhook", "wake_prompt": "hook"}).get_json()
        assert dto["args"]["secret"]  # capability secret returned once, on create
        rows = client.get(
            f"/api/sessions/{sid}/triggers", headers=auth_headers
        ).get_json()["triggers"]
        row = next(t for t in rows if t["id"] == dto["id"])
        assert "secret" not in row["args"]  # stripped from list views

    def test_arm_unknown_session_404(self, client, auth_headers, tmp_path):
        _setup(tmp_path)
        resp = _arm(client, auth_headers, "nope", {"kind": "webhook", "wake_prompt": "x"})
        assert resp.status_code == 404

    def test_arm_terminated_session_410(self, client, auth_headers, tmp_path):
        _setup(tmp_path)
        sid = _session()
        backend.runtime.terminate_session(sid)
        resp = _arm(client, auth_headers, sid, {"kind": "webhook", "wake_prompt": "x"})
        assert resp.status_code == 410
        assert resp.get_json()["error"]["code"] == "session_terminated"

    def test_arm_policy_rejection_400(self, client, auth_headers, tmp_path):
        _setup(tmp_path, policy=TriggerPolicy(max_armed_per_session=1))
        sid = _session()
        assert _arm(client, auth_headers, sid, WEBHOOK).status_code == 201
        resp = _arm(client, auth_headers, sid, {"kind": "webhook", "wake_prompt": "b"})
        assert resp.status_code == 400
        assert resp.get_json()["error"]["code"] == "policy"

    def test_arm_validation_400_on_bad_cron(self, client, auth_headers, tmp_path):
        _setup(tmp_path)
        sid = _session()
        resp = _arm(
            client, auth_headers, sid,
            {"kind": "time.cron", "wake_prompt": "x", "cron": "not a cron"},
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"]["code"] == "validation"

    def test_arm_requires_auth(self, client, tmp_path):
        _setup(tmp_path)
        sid = _session()
        no_auth = client.post(f"/api/sessions/{sid}/triggers", json=WEBHOOK)
        assert no_auth.status_code == 401


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


class TestList:
    def test_session_and_collection_list(self, client, auth_headers, tmp_path):
        _setup(tmp_path)
        sid = _session()
        _arm(client, auth_headers, sid, {"kind": "webhook", "wake_prompt": "a"})
        _arm(client, auth_headers, sid, CRON)
        session_rows = client.get(
            f"/api/sessions/{sid}/triggers", headers=auth_headers
        ).get_json()["triggers"]
        assert len(session_rows) == 2
        cron_rows = client.get(
            "/api/triggers?kind=time.cron", headers=auth_headers
        ).get_json()["triggers"]
        assert [t["kind"] for t in cron_rows] == ["time.cron"]

    def test_collection_status_filter(self, client, auth_headers, tmp_path):
        _setup(tmp_path)
        sid = _session()
        _arm(client, auth_headers, sid, {"kind": "webhook", "wake_prompt": "a"})
        armed = client.get(
            "/api/triggers?status=armed", headers=auth_headers
        ).get_json()["triggers"]
        assert len(armed) == 1
        # A nonsense status filters to nothing rather than erroring.
        junk = client.get("/api/triggers?status=bogus", headers=auth_headers)
        assert junk.status_code == 200


# ---------------------------------------------------------------------------
# Pause / resume / cancel
# ---------------------------------------------------------------------------


class TestMutations:
    def test_pause_then_resume(self, client, auth_headers, tmp_path):
        _setup(tmp_path)
        sid = _session()
        tid = _arm_id(client, auth_headers, sid, WEBHOOK)
        paused = client.patch(
            f"/api/triggers/{tid}", headers=auth_headers, json={"status": "paused"}
        )
        assert paused.status_code == 200
        assert paused.get_json()["status"] == "paused"
        resumed = client.patch(
            f"/api/triggers/{tid}", headers=auth_headers, json={"status": "armed"}
        )
        assert resumed.get_json()["status"] == "armed"

    def test_patch_terminal_409(self, client, auth_headers, tmp_path):
        _setup(tmp_path)
        sid = _session()
        tid = _arm_id(client, auth_headers, sid, WEBHOOK)
        client.delete(f"/api/triggers/{tid}", headers=auth_headers)  # → cancelled (terminal)
        resp = client.patch(f"/api/triggers/{tid}", headers=auth_headers, json={"status": "armed"})
        assert resp.status_code == 409
        assert resp.get_json()["error"]["code"] == "conflict"

    def test_patch_bad_status_400(self, client, auth_headers, tmp_path):
        _setup(tmp_path)
        sid = _session()
        tid = _arm_id(client, auth_headers, sid, WEBHOOK)
        resp = client.patch(
            f"/api/triggers/{tid}", headers=auth_headers, json={"status": "completed"}
        )
        assert resp.status_code == 400

    def test_patch_unknown_404(self, client, auth_headers, tmp_path):
        _setup(tmp_path)
        resp = client.patch("/api/triggers/nope", headers=auth_headers, json={"status": "armed"})
        assert resp.status_code == 404

    def test_cancel_idempotent(self, client, auth_headers, tmp_path):
        _setup(tmp_path)
        sid = _session()
        tid = _arm_id(client, auth_headers, sid, WEBHOOK)
        first = client.delete(f"/api/triggers/{tid}", headers=auth_headers)
        assert first.status_code == 200
        assert first.get_json() == {"id": tid, "status": "cancelled"}
        second = client.delete(f"/api/triggers/{tid}", headers=auth_headers)
        assert second.status_code == 200
        assert second.get_json()["status"] == "cancelled"

    def test_cancel_unknown_404(self, client, auth_headers, tmp_path):
        _setup(tmp_path)
        assert client.delete("/api/triggers/nope", headers=auth_headers).status_code == 404


# ---------------------------------------------------------------------------
# Webhook capability URL (unauthenticated)
# ---------------------------------------------------------------------------


class TestWebhookFire:
    def test_webhook_fire_happy_path(self, client, auth_headers, tmp_path):
        _, _, deliver = _setup(tmp_path)
        sid = _session()
        dto = _arm(client, auth_headers, sid, {"kind": "webhook", "wake_prompt": "wake"}).get_json()
        tid, secret = dto["id"], dto["args"]["secret"]
        # No auth header — the capability URL IS the credential.
        resp = client.post(
            f"/api/triggers/hook/{tid}/{secret}", data=b"{}", content_type="application/json"
        )
        assert resp.status_code == 202
        assert resp.get_json() == {"accepted": True}
        assert len(deliver.calls) == 1
        rows = client.get(f"/api/sessions/{sid}/triggers", headers=auth_headers).get_json()
        row = rows["triggers"][0]
        assert row["fires"] == 1
        fired = [
            e for e in backend.runtime.session_store.load_transcript(sid)
            if e.get("type") == "trigger_fired"
        ]
        assert len(fired) == 1

    def test_webhook_bad_secret_404(self, client, auth_headers, tmp_path):
        _setup(tmp_path)
        sid = _session()
        tid = _arm_id(client, auth_headers, sid, {"kind": "webhook", "wake_prompt": "x"})
        resp = client.post(f"/api/triggers/hook/{tid}/wrong-secret", data=b"{}")
        assert resp.status_code == 404

    def test_webhook_unknown_id_404(self, client, auth_headers, tmp_path):
        _setup(tmp_path)
        assert client.post("/api/triggers/hook/nope/whatever", data=b"{}").status_code == 404

    def test_webhook_payload_truncation(self, client, auth_headers, tmp_path):
        _, _, deliver = _setup(tmp_path, policy=TriggerPolicy(webhook_payload_max_bytes=10))
        sid = _session()
        dto = _arm(client, auth_headers, sid, {"kind": "webhook", "wake_prompt": "hook"}).get_json()
        tid, secret = dto["id"], dto["args"]["secret"]
        resp = client.post(
            f"/api/triggers/hook/{tid}/{secret}", data=b"X" * 100, content_type="application/json"
        )
        assert resp.status_code == 202
        _, wake, _, _ = deliver.calls[0]
        assert '"truncated": true' in wake
        assert wake.count("X") == 10  # body truncated to the 10-byte policy cap


# ---------------------------------------------------------------------------
# Terminate cascade (WP2 ↔ WP3 seam)
# ---------------------------------------------------------------------------


class TestTerminateCascade:
    def test_terminate_cancels_and_counts_triggers(self, client, auth_headers, tmp_path):
        _setup(tmp_path)
        sid = _session()
        _arm(client, auth_headers, sid, {"kind": "webhook", "wake_prompt": "a"})
        _arm(client, auth_headers, sid, CRON)
        resp = client.post(f"/api/sessions/{sid}/terminate", headers=auth_headers)
        assert resp.status_code == 200
        # WP2 contract: cancelled_triggers is an int COUNT summed from the
        # on_terminate cascade callbacks (see report — console types it string[]).
        assert resp.get_json()["cancelled_triggers"] == 2
        listed = client.get(f"/api/triggers?session_id={sid}", headers=auth_headers).get_json()
        rows = listed["triggers"]
        assert all(t["status"] == "cancelled" for t in rows)

    def test_terminate_without_triggers_counts_zero(self, client, auth_headers, tmp_path):
        _setup(tmp_path)
        sid = _session()
        resp = client.post(f"/api/sessions/{sid}/terminate", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["cancelled_triggers"] == 0
