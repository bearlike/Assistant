#!/usr/bin/env python3
"""Trigger admission policy — the ONE place arming limits live.

``TriggerPolicy`` is a config-tunable Pydantic model, never a constant
hardcoded into the tool or a future scheduler service. Every numeric knob a
caller might want to retune per deployment lives here as a field with a sane
default; Wave-2 constructs an instance from ``AppConfig`` and hands it to
``ScheduleTriggerTool`` via DI — this module owns only the model + its
``admit()`` gate, no config I/O and no store access.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from croniter import croniter  # type: ignore[import-untyped]  # no stubs published
from pydantic import BaseModel, ConfigDict, Field

from mewbo_core.triggers.spec import CronTrigger, TriggerSpec


class TriggerPolicy(BaseModel):
    """Admission limits for arming a trigger — sane defaults, deployment-tunable.

    ``admit()`` is the single gate a spec must pass before it is persisted:
    it validates *and* stamps defaults (currently just ``expires_at``) in one
    step, so a caller never has to remember to apply policy defaults
    separately from checking policy limits.
    """

    model_config = ConfigDict(extra="forbid")

    max_armed_per_session: int = Field(
        default=20,
        ge=1,
        description="Ceiling on currently-armed triggers a single session may hold.",
    )
    max_fires_cap: int = Field(
        default=100,
        ge=1,
        description="Hard ceiling on a trigger's own `max_fires` at arm time.",
    )
    default_expiry: timedelta = Field(
        default=timedelta(days=7),
        description="Stamped onto `expires_at` when an armed spec doesn't set one.",
    )
    cron_min_interval_seconds: int = Field(
        default=60,
        ge=1,
        description="Minimum allowed gap between two consecutive cron fires.",
    )
    webhook_payload_max_bytes: int = Field(
        default=200_000,
        ge=1,
        description=(
            "Ceiling on an inbound webhook body a firing service should accept. "
            "Not consumed by `admit()` (no payload exists at arm time) — read by "
            "the webhook-receiving service that verifies/fires the trigger."
        ),
    )

    def admit(self, spec: TriggerSpec, armed_count: int) -> TriggerSpec:
        """Validate *spec* against this policy and return it with defaults applied.

        Raises ``ValueError`` with a human-readable reason on rejection.
        Never mutates *spec* in place — stamping a default expiry returns a
        NEW instance (``model_copy``) so a caller holding the original spec
        object is unaffected.
        """
        if armed_count >= self.max_armed_per_session:
            raise ValueError(
                f"session already has {armed_count} armed trigger(s) "
                f"(policy max_armed_per_session={self.max_armed_per_session})"
            )
        if spec.max_fires is not None and spec.max_fires > self.max_fires_cap:
            raise ValueError(
                f"max_fires={spec.max_fires} exceeds policy "
                f"max_fires_cap={self.max_fires_cap}"
            )
        if isinstance(spec, CronTrigger):
            self._check_cron_interval(spec)
        if spec.expires_at is None:
            spec = spec.model_copy(
                update={"expires_at": spec.created_at + self.default_expiry}
            )
        return spec

    def _check_cron_interval(self, spec: CronTrigger) -> None:
        """Reject a cron schedule whose consecutive fires are tighter than allowed.

        Samples the gap between the first two occurrences after
        ``created_at`` — cheap and sufficient: a fixed-interval expression
        (``*/N ...``) has a constant gap, and an irregular one is still
        rejected on its first observed (tightest-at-arm-time) pair rather
        than an unbounded scan.
        """
        it = croniter(spec.cron, spec.created_at)
        first: datetime = it.get_next(datetime)
        second: datetime = it.get_next(datetime)
        gap_seconds = (second - first).total_seconds()
        if gap_seconds < self.cron_min_interval_seconds:
            raise ValueError(
                f"cron {spec.cron!r} fires every {gap_seconds:.0f}s, below policy "
                f"cron_min_interval_seconds={self.cron_min_interval_seconds}"
            )


__all__ = ["TriggerPolicy"]
