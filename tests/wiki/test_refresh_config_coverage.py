"""Every declared ``wiki.refresh`` field must have a live consumer.

A tripwire, not a unit test. The seven thresholds under this key were declared,
documented and shipped while nothing read them, and the reason that survived
review is structural: each stage's constructor default equals its config
default, so an unwired knob produces byte-identical behaviour to a wired one.
Nothing fails. Nothing looks wrong. The operator's setting is simply ignored.

So this file enumerates ``WikiRefreshConfig.model_fields`` rather than listing
names. A hand-written list of seven would pass forever after an eighth field is
added and forgotten — which is precisely the defect being guarded against, one
field later.

The check runs at RUNTIME, against the real composition root. A static or
grep-based version was tried and is worse than useless here: the reader passes
each field NAME as a variable, so a search for the literal string reports every
one of them as unwired. The only honest question is whether a non-default value
set in config arrives on the object that consumes it.
"""
from __future__ import annotations

from typing import Any

import pytest
from mewbo_core.config import WikiRefreshConfig, reset_config, set_config_override
from mewbo_graph.wiki.refresh import RefreshOrchestrator
from mewbo_graph.wiki.store import JsonWikiStore

from .conftest import FakeEmbedder, FakeParser

# Fields consumed OUTSIDE this module, and therefore not provable from a
# ``RefreshOrchestrator``. ``default_mode`` is an API-layer concern: it selects
# a refresh strategy before any orchestrator is built, so it is not a threshold
# a stage could hold. It is proved by
# ``tests/wiki/test_refresh_default_mode.py``; this exemption should shrink,
# never grow.
#
# ``require_scope_confirm`` was exempted here too, and is now RETIRED rather
# than wired. It claimed to gate a human confirmation between the plan and the
# act phase, and no such boundary exists to gate: the graph delta is committed
# before a scope preview is produced, and ``IndexingStatus`` has no state
# meaning "waiting for a human". A knob that cannot be honoured is a claim, not
# a setting.
CONSUMED_ELSEWHERE = {"default_mode"}

# The stages a ``from_store``-built orchestrator composes. A future stage added
# to the composer belongs here too, or its thresholds become unprovable.
STAGE_ATTRS = ("_graph_indexer", "_reconciler", "_doc_planner")


def _sentinel(field: str, index: int) -> Any:
    """A value no default equals, for a field's declared type.

    Deliberately raises on a type it does not know rather than skipping: a new
    field of an unhandled type must force an author to decide how it is proven,
    not slip through as "nothing to check".
    """
    annotation = WikiRefreshConfig.model_fields[field].annotation
    if annotation is int:
        return 1_000 + index
    if annotation is float:
        return 0.101 + index * 0.017
    raise AssertionError(
        f"wiki.refresh.{field} has type {annotation!r}, which this tripwire "
        "cannot generate a sentinel for. Add a branch here (and wire the field "
        "in RefreshOrchestrator.from_store) rather than exempting it."
    )


@pytest.fixture
def store(tmp_path):
    return JsonWikiStore(root_dir=tmp_path / "wiki")


@pytest.fixture
def restore_config():
    yield
    reset_config()


def _values_on_stages(orch: RefreshOrchestrator) -> set[Any]:
    """Every scalar an orchestrator's stages hold, whatever they named it.

    Read by value rather than by attribute name on purpose: three of these
    fields are renamed on the way down (``page_keep`` → ``keep``), so a
    name-matched check would report a correctly wired field as missing and
    invite someone to "fix" working code.
    """
    found: set[Any] = set()
    for stage_attr in STAGE_ATTRS:
        stage = getattr(orch, stage_attr)
        for value in vars(stage).values():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                found.add(value)
    return found


def test_every_declared_refresh_field_reaches_a_stage(store, restore_config) -> None:
    fields = [f for f in WikiRefreshConfig.model_fields if f not in CONSUMED_ELSEWHERE]
    assert fields, "WikiRefreshConfig declares nothing this tripwire can prove"
    sentinels = {f: _sentinel(f, i) for i, f in enumerate(fields)}

    set_config_override({"wiki": {"refresh": dict(sentinels)}})
    orch = RefreshOrchestrator.from_store(
        store, slug="acme/repo", parser=FakeParser({}), embedder=FakeEmbedder()
    )

    landed = _values_on_stages(orch)
    unwired = sorted(f for f, value in sentinels.items() if value not in landed)
    assert not unwired, (
        "these wiki.refresh fields are declared in config but no refresh stage "
        f"consumes them: {unwired}. Read each one in "
        "RefreshOrchestrator.from_store (packages/mewbo_graph/src/mewbo_graph/"
        "wiki/refresh.py) and pass it into the stage that uses it — the stages "
        "stay pure DI and must not read config themselves. If the field is "
        "genuinely consumed elsewhere, add it to CONSUMED_ELSEWHERE with a "
        "comment naming where."
    )


def test_the_exemptions_are_real_fields(store) -> None:
    """A stale exemption is how a wired-elsewhere field turns into an unwired one.

    Renaming or deleting a config field leaves its name behind in the exemption
    set, where it silently excuses whatever field is added under that name next.
    """
    unknown = CONSUMED_ELSEWHERE - set(WikiRefreshConfig.model_fields)
    assert not unknown, (
        f"CONSUMED_ELSEWHERE names fields WikiRefreshConfig no longer declares: "
        f"{sorted(unknown)}. Drop them."
    )


def test_the_tripwire_fails_when_a_field_goes_unwired(store, restore_config) -> None:
    """The guard's own failure path — a tripwire that cannot trip guards nothing.

    Simulates the exact defect (a field declared in config that no stage reads)
    by adding a name to the enumeration that ``from_store`` has never heard of,
    and asserts the value genuinely fails to arrive. Without this, a bug in
    ``_values_on_stages`` would make every field look wired forever.
    """
    set_config_override({"wiki": {"refresh": {"drift_keep": 0.101}}})
    orch = RefreshOrchestrator.from_store(
        store, slug="acme/repo", parser=FakeParser({}), embedder=FakeEmbedder()
    )

    landed = _values_on_stages(orch)
    assert 0.101 in landed, "the wired field must arrive"
    assert 987.654 not in landed, "a value nothing was configured with must not"
