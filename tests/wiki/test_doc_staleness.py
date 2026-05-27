"""DocStalenessPlanner — docs-as-nodes staleness propagation + new-page hints."""
from __future__ import annotations

import pytest
from mewbo_graph.wiki.refresh import DocStalenessPlanner, GraphDelta
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import Frontmatter, GraphNode, SourceRef, WikiPage

SLUG = "org/repo"


@pytest.fixture
def store(tmp_path):
    return JsonWikiStore(root_dir=tmp_path / "wiki")


def _page(page_id, sources):
    return WikiPage(
        id=page_id,
        title=page_id.title(),
        frontmatter=Frontmatter(
            title=page_id.title(),
            slug=page_id,
            relevantSources=[SourceRef(path=p) for p in sources],
        ),
        body=f"# {page_id}\n\nbody",
        toc=[],
        nav=[],
    )


def _code(nid, name, f):
    return GraphNode(slug=SLUG, node_id=nid, type="Function", name=name, file=f, range=(0, 9))


def _delta(*, added=(), modified=(), removed=(), affected=None):
    keys = set(added) | set(modified) | set(removed)
    return GraphDelta(
        added_keys=frozenset(added),
        modified_keys=frozenset(modified),
        removed_keys=frozenset(removed),
        affected=frozenset(affected if affected is not None else keys),
        early_cutoff_files=(),
    )


# ── migration ───────────────────────────────────────────────────────────────


def test_migrate_builds_doc_notes_from_frontmatter(store) -> None:
    store.save_page(SLUG, _page("auth", ["auth.py", "session.py"]))
    store.save_page(SLUG, _page("storage", ["store.py"]))
    n = DocStalenessPlanner(store=store).migrate(SLUG)
    assert n == 2
    note = store.get_doc_note(SLUG, "auth")
    assert set(note.anchor_keys) == {"auth.py", "session.py"}
    # idempotent: re-migrate does not duplicate
    DocStalenessPlanner(store=store).migrate(SLUG)
    assert len(store.list_doc_notes(SLUG)) == 2


# ── staleness propagation ───────────────────────────────────────────────────


def test_clean_page_kept(store) -> None:
    store.save_page(SLUG, _page("auth", ["auth.py"]))
    planner = DocStalenessPlanner(store=store)
    planner.migrate(SLUG)
    plan = planner.plan(SLUG, _delta(modified=["other.py"]))
    entry = next(p for p in plan.pages if p.page_id == "auth")
    assert entry.policy == "keep"
    assert store.get_doc_note(SLUG, "auth").generation_policy == "keep"


def test_all_anchors_changed_regenerates(store) -> None:
    store.save_page(SLUG, _page("auth", ["auth.py"]))
    planner = DocStalenessPlanner(store=store)
    planner.migrate(SLUG)
    plan = planner.plan(SLUG, _delta(modified=["auth.py"]))
    entry = next(p for p in plan.pages if p.page_id == "auth")
    # direct=1.0 → staleness 0.5 → regenerate band
    assert entry.policy == "regenerate"


def test_partial_change_edits(store) -> None:
    store.save_page(SLUG, _page("auth", ["auth.py", "session.py", "token.py", "crypto.py"]))
    planner = DocStalenessPlanner(store=store)
    planner.migrate(SLUG)
    plan = planner.plan(SLUG, _delta(modified=["auth.py"]))  # 1/4 anchors → direct .25 → .125
    entry = next(p for p in plan.pages if p.page_id == "auth")
    assert entry.policy == "edit"


def test_deleted_anchors_flag_review(store) -> None:
    store.save_page(SLUG, _page("auth", ["auth.py", "session.py"]))
    planner = DocStalenessPlanner(store=store)
    planner.migrate(SLUG)
    # both anchors deleted → deleted_fraction 1.0 > 0.5 → review
    plan = planner.plan(SLUG, _delta(removed=["auth.py", "session.py"]))
    entry = next(p for p in plan.pages if p.page_id == "auth")
    assert entry.policy == "regenerate"
    assert entry.needs_review is True


def test_transitive_affected_counts(store) -> None:
    # auth page anchored to a caller; the callee changed and the closure put the
    # caller into `affected` → the page is impacted transitively.
    store.save_page(SLUG, _page("auth", ["caller.py"]))
    planner = DocStalenessPlanner(store=store)
    planner.migrate(SLUG)
    plan = planner.plan(
        SLUG, _delta(modified=["callee.py"], affected={"callee.py", "caller.py"})
    )
    entry = next(p for p in plan.pages if p.page_id == "auth")
    assert entry.policy != "keep"


# ── new-page detection ──────────────────────────────────────────────────────


def test_new_page_proposed_for_uncovered_public_entities(store) -> None:
    store.save_page(SLUG, _page("auth", ["auth.py"]))  # documents auth.py only
    store.upsert_nodes(
        SLUG,
        [_code(f"n{i}", f"fn{i}", "newmod.py") for i in range(5)],
    )
    planner = DocStalenessPlanner(store=store, new_page_min=3)
    planner.migrate(SLUG)
    added = [f"newmod.py#fn{i}" for i in range(5)]
    plan = planner.plan(SLUG, _delta(added=added))
    assert "newmod.py" in plan.new_pages


def test_no_new_page_below_threshold(store) -> None:
    store.save_page(SLUG, _page("auth", ["auth.py"]))
    store.upsert_nodes(SLUG, [_code("n0", "fn0", "tiny.py")])
    planner = DocStalenessPlanner(store=store, new_page_min=3)
    planner.migrate(SLUG)
    plan = planner.plan(SLUG, _delta(added=["tiny.py#fn0"]))
    assert plan.new_pages == ()
