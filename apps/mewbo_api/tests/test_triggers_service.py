"""Unit tests for ``TriggerService`` tick/fire semantics (WP3).

Drives the service synchronously — an injected clock + a fake ``deliver`` + a
fake forge client — so every scheduling decision is exercised with no threads
and no sleeps. Stubs only the I/O boundaries the service depends on.
"""

# mypy: ignore-errors

from datetime import datetime, timedelta, timezone

import pytest
from mewbo_api.triggers.service import TriggerFireContext, TriggerService
from mewbo_core.config import TriggersConfig
from mewbo_core.session_runtime import SessionRuntime
from mewbo_core.session_store import SessionStore
from mewbo_core.triggers.policy import TriggerPolicy
from mewbo_core.triggers.spec import (
    CiWorkflowTrigger,
    CronTrigger,
    ForgePrTrigger,
    TimeAtTrigger,
    WebhookTrigger,
)
from mewbo_core.triggers.store import JsonTriggerStore

BASE = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)


class Clock:
    """A mutable UTC clock injected as the service's time source."""

    def __init__(self, now: datetime = BASE) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class Deliver:
    """Records each ``deliver(TriggerFireContext)`` call; returns a fixed verdict.

    The fire pipeline hands a structured ``TriggerFireContext`` (replacing
    the old 4-positional signature); ``calls`` records the fields as a
    tuple so the ordering/attribution assertions stay compact, while
    ``contexts`` keeps the objects for shape checks.
    """

    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.calls: list[tuple[str, str, str, str]] = []
        self.contexts: list[TriggerFireContext] = []

    def __call__(self, ctx: TriggerFireContext) -> bool:
        self.contexts.append(ctx)
        self.calls.append((ctx.session_id, ctx.wake, ctx.action, ctx.trigger_id))
        return self.result


class FakeForge:
    """A fake :class:`ForgeClient` returning canned normalized payloads."""

    def __init__(self, *, ci=None, pr=None, raises: bool = False) -> None:
        self._ci = ci or []
        self._pr = pr or []
        self.raises = raises
        self.ci_calls = 0
        self.pr_calls = 0

    def poll_ci_runs(self, repo, *, ref=None):
        self.ci_calls += 1
        if self.raises:
            raise RuntimeError("forge unreachable")
        return list(self._ci)

    def poll_pr(self, repo, number):
        self.pr_calls += 1
        if self.raises:
            raise RuntimeError("forge unreachable")
        return list(self._pr)


@pytest.fixture()
def env(tmp_path):
    """A real runtime + store + a fresh session, with a clock/deliver harness."""
    session_store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=session_store)
    session_id = runtime.resolve_session()
    store = JsonTriggerStore(data_file=str(tmp_path / "triggers.json"))
    clock = Clock()
    deliver = Deliver()

    def make(*, config=None, forge=None):
        return TriggerService(
            runtime=runtime,
            store=store,
            policy=TriggerPolicy(),
            config=config or TriggersConfig(poll_interval_seconds=5, max_consecutive_failures=2),
            forge_client_factory=(forge if forge is not None else (lambda repo: None)),
            deliver=deliver,
            clock=clock,
        )

    return {
        "runtime": runtime,
        "store": store,
        "session_id": session_id,
        "clock": clock,
        "deliver": deliver,
        "make": make,
    }


def _events(runtime, session_id, event_type):
    return [
        e for e in runtime.session_store.load_transcript(session_id) if e.get("type") == event_type
    ]


# ---------------------------------------------------------------------------
# Time-driven fires
# ---------------------------------------------------------------------------


def test_time_at_fires_once_then_completes(env):
    svc = env["make"]()
    spec = TimeAtTrigger(
        session_id=env["session_id"],
        wake_prompt="wake up",
        created_by="user",
        at=BASE - timedelta(seconds=1),
    )
    env["store"].create(spec)

    svc.tick()
    assert env["deliver"].calls == [(env["session_id"], "wake up", "message", spec.id)]
    reloaded = env["store"].get(spec.id)
    assert reloaded.status == "completed"
    assert reloaded.fires == 1
    assert len(_events(env["runtime"], env["session_id"], "trigger_fired")) == 1

    # A completed trigger is no longer armed → a second tick is a no-op.
    svc.tick()
    assert len(env["deliver"].calls) == 1


def test_deliver_receives_structured_fire_context(env):
    """The fire pipeline hands ``deliver`` ONE frozen ``TriggerFireContext``."""
    svc = env["make"]()
    spec = TimeAtTrigger(
        session_id=env["session_id"],
        wake_prompt="wake up",
        created_by="user",
        at=BASE - timedelta(seconds=1),
    )
    env["store"].create(spec)
    svc.tick()

    ctxs = env["deliver"].contexts
    assert len(ctxs) == 1
    ctx = ctxs[0]
    assert isinstance(ctx, TriggerFireContext)
    assert (ctx.session_id, ctx.wake, ctx.action, ctx.trigger_id) == (
        env["session_id"],
        "wake up",
        "message",
        spec.id,
    )
    with pytest.raises(Exception):  # frozen — in-process runtime record
        ctx.session_id = "other"  # type: ignore[misc]


def test_time_at_not_due_does_not_fire(env):
    svc = env["make"]()
    spec = TimeAtTrigger(
        session_id=env["session_id"],
        wake_prompt="later",
        created_by="user",
        at=BASE + timedelta(hours=1),
    )
    env["store"].create(spec)
    svc.tick()
    assert env["deliver"].calls == []
    assert env["store"].get(spec.id).status == "armed"


def test_cron_rearms_across_ticks(env):
    svc = env["make"]()
    spec = CronTrigger(
        session_id=env["session_id"],
        wake_prompt="every minute",
        created_by="user",
        cron="* * * * *",
        created_at=BASE,
    )
    env["store"].create(spec)

    env["clock"].now = BASE + timedelta(seconds=61)  # past the first minute boundary
    svc.tick()
    assert env["store"].get(spec.id).fires == 1
    assert env["store"].get(spec.id).status == "armed"  # recurring → stays armed

    env["clock"].now = BASE + timedelta(seconds=181)
    svc.tick()
    assert env["store"].get(spec.id).fires == 2
    assert env["store"].get(spec.id).status == "armed"


# ---------------------------------------------------------------------------
# Busy session / re-arm
# ---------------------------------------------------------------------------


def test_busy_refusal_rearms_then_fires(env):
    """A refused delivery (busy session) re-arms; no record_fire, retry next tick."""
    deliver = env["deliver"]
    deliver.result = False
    svc = env["make"]()
    spec = TimeAtTrigger(
        session_id=env["session_id"],
        wake_prompt="wake",
        created_by="user",
        at=BASE - timedelta(seconds=1),
    )
    env["store"].create(spec)

    svc.tick()  # refused
    assert len(deliver.calls) == 1
    reloaded = env["store"].get(spec.id)
    assert reloaded.status == "armed"  # re-armed, not fired
    assert reloaded.fires == 0
    assert _events(env["runtime"], env["session_id"], "trigger_fired") == []

    deliver.result = True
    svc.tick()  # now delivers
    assert env["store"].get(spec.id).status == "completed"
    assert env["store"].get(spec.id).fires == 1


# ---------------------------------------------------------------------------
# Terminated / expiry
# ---------------------------------------------------------------------------


def test_terminated_session_cancels_not_fires(env):
    env["runtime"].terminate_session(env["session_id"])
    svc = env["make"]()
    spec = TimeAtTrigger(
        session_id=env["session_id"],
        wake_prompt="wake",
        created_by="user",
        at=BASE - timedelta(seconds=1),
    )
    env["store"].create(spec)
    svc.tick()
    assert env["deliver"].calls == []
    assert env["store"].get(spec.id).status == "cancelled"


def test_expiry_transitions_expired(env):
    svc = env["make"]()
    spec = TimeAtTrigger(
        session_id=env["session_id"],
        wake_prompt="wake",
        created_by="user",
        at=BASE + timedelta(hours=1),  # not due
        expires_at=BASE - timedelta(seconds=1),  # already expired
    )
    env["store"].create(spec)
    svc.tick()
    assert env["deliver"].calls == []
    assert env["store"].get(spec.id).status == "expired"


# ---------------------------------------------------------------------------
# Forge polling
# ---------------------------------------------------------------------------


def test_ci_workflow_fires_once_on_match(env):
    forge = FakeForge(
        ci=[
            {
                "repo": "acme/widgets",
                "run_id": 7,
                "workflow": "ci.yml",
                "ref": None,
                "status": "completed",
                "conclusion": "success",
            }
        ]
    )
    svc = env["make"](forge=lambda repo: forge)
    spec = CiWorkflowTrigger(
        session_id=env["session_id"],
        wake_prompt="ci done",
        created_by="user",
        repo="acme/widgets",
        workflow="ci.yml",
        conclusion_filter=["success"],
    )
    env["store"].create(spec)

    svc.tick()
    assert len(env["deliver"].calls) == 1
    assert env["store"].get(spec.id).fires == 1

    # Same run on the next poll → signature dedup → no re-fire.
    env["clock"].advance(6)
    svc.tick()
    assert len(env["deliver"].calls) == 1


def test_forge_pr_merged_fires_once(env):
    forge = FakeForge(pr=[{"repo": "acme/widgets", "number": 42, "event": "merged"}])
    svc = env["make"](forge=lambda repo: forge)
    spec = ForgePrTrigger(
        session_id=env["session_id"],
        wake_prompt="pr merged",
        created_by="user",
        repo="acme/widgets",
        number=42,
        events=["merged"],
    )
    env["store"].create(spec)

    svc.tick()
    assert len(env["deliver"].calls) == 1

    env["clock"].advance(6)
    svc.tick()
    assert len(env["deliver"].calls) == 1  # deduped


def test_poll_cadence_gates_forge_calls(env):
    forge = FakeForge(ci=[])
    svc = env["make"](forge=lambda repo: forge)
    spec = CiWorkflowTrigger(
        session_id=env["session_id"],
        wake_prompt="ci",
        created_by="user",
        repo="acme/widgets",
        workflow="ci.yml",
    )
    env["store"].create(spec)

    svc.tick()  # polls (last_poll was None)
    assert forge.ci_calls == 1
    svc.tick()  # within poll_interval → no poll
    assert forge.ci_calls == 1
    env["clock"].advance(6)  # past poll_interval (5s)
    svc.tick()
    assert forge.ci_calls == 2


def test_consecutive_poll_failures_transition_failed(env):
    forge = FakeForge(raises=True)
    svc = env["make"](forge=lambda repo: forge)
    spec = CiWorkflowTrigger(
        session_id=env["session_id"],
        wake_prompt="ci",
        created_by="user",
        repo="acme/widgets",
        workflow="ci.yml",
    )
    env["store"].create(spec)

    svc.tick()  # failure 1
    assert env["store"].get(spec.id).status == "armed"
    env["clock"].advance(6)
    svc.tick()  # failure 2 == max_consecutive_failures
    failed = env["store"].get(spec.id)
    assert failed.status == "failed"
    assert failed.last_error


# ---------------------------------------------------------------------------
# Boot rescan (persisted state IS the schedule)
# ---------------------------------------------------------------------------


def test_boot_rescan_fires_trigger_persisted_before_service_existed(env):
    """A trigger armed in the store BEFORE the service is built still fires.

    Nothing in-memory carries the schedule — the first tick re-reads the store,
    which is exactly what the boot rescan relies on.
    """
    spec = TimeAtTrigger(
        session_id=env["session_id"],
        wake_prompt="persisted",
        created_by="user",
        at=BASE - timedelta(seconds=1),
    )
    env["store"].create(spec)  # persisted first
    svc = env["make"]()  # service built second — no in-memory arm
    svc.tick()
    assert len(env["deliver"].calls) == 1
    assert env["store"].get(spec.id).status == "completed"


def test_webhook_trigger_never_fires_from_the_tick(env):
    """A webhook trigger is event-driven; the clock tick never fires it."""
    svc = env["make"]()
    spec = WebhookTrigger(
        session_id=env["session_id"], wake_prompt="hook", created_by="user"
    )
    env["store"].create(spec)
    svc.tick()
    assert env["deliver"].calls == []
    assert env["store"].get(spec.id).status == "armed"
