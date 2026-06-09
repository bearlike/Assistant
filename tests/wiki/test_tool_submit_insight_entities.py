"""wiki_submit_insight carries optional entity_recommendations → persisted priors.

The recommendations are resolution PRIORS the next ``EntityResolver`` pass
consults — they never hard-mutate the graph. Exercises the real tool ``handle``
path over a real ``JsonWikiStore``; only the runtime/ctx + log seams are stubbed.
The existing insight behavior must stay unchanged when the field is absent.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from mewbo_core.classes import ActionStep
from mewbo_graph.plugins.wiki import submit_insight as mod
from mewbo_graph.wiki.store import JsonWikiStore

SLUG = "org/repo"


def _patch(monkeypatch, store):
    ctx = SimpleNamespace(
        slug=SLUG, store=store, session_id="s1", job_id="j1", clone_dir=None
    )
    monkeypatch.setattr(mod, "_resolve_runtime", lambda: SimpleNamespace(wiki_store=store))
    monkeypatch.setattr(mod, "resolve_job_ctx", lambda sid, rt: ctx)
    monkeypatch.setattr(mod, "resolve_qa_ctx", lambda sid, rt: None)
    monkeypatch.setattr(mod, "emit_log", lambda *a, **k: None)
    return ctx


def _run(store, tool_input):
    tool = mod.WikiSubmitInsightTool("s1")
    step = ActionStep(tool_id="wiki_submit_insight", operation="call", tool_input=tool_input)
    return asyncio.run(tool.handle(step))


def test_submit_insight_persists_entity_recommendations(tmp_path, monkeypatch):
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    _patch(monkeypatch, store)
    res = _run(
        store,
        {
            "content": "AuthService is owned by the Platform team",
            "entity_recommendations": [
                {
                    "action": "merge",
                    "subjects": ["e1", "e2"],
                    "type": None,
                    "rationale": "same team",
                }
            ],
        },
    )
    assert json.loads(res.content)["ok"] in {True, False}
    recs = store.get_entity_recommendations(SLUG)
    assert len(recs) == 1
    assert recs[0].action == "merge" and recs[0].subjects == ["e1", "e2"]


def test_submit_insight_without_recs_persists_none(tmp_path, monkeypatch):
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    _patch(monkeypatch, store)
    res = _run(store, {"content": "AuthService verifies tokens"})
    # Existing behavior unchanged: a normal insight is ingested...
    assert json.loads(res.content)["ok"] is True
    assert len(store.query_memory(SLUG)) == 1
    # ...and no recommendation is written when the field is absent.
    assert store.get_entity_recommendations(SLUG) == []


def test_submit_insight_skips_malformed_recommendation(tmp_path, monkeypatch):
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    _patch(monkeypatch, store)
    res = _run(
        store,
        {
            "content": "AuthService verifies tokens",
            "entity_recommendations": [
                {"action": "merge", "subjects": ["a", "b"]},  # valid
                {"action": "bogus", "subjects": ["c"]},  # invalid action → skipped
            ],
        },
    )
    assert json.loads(res.content)["ok"] is True
    recs = store.get_entity_recommendations(SLUG)
    assert len(recs) == 1 and recs[0].action == "merge"  # malformed one dropped, non-fatal
