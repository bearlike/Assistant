"""ChangeDetector — content-hash diff of a working tree vs the file manifest."""
from __future__ import annotations

from pathlib import Path

import pytest
from mewbo_graph.wiki.memory_types import FileManifest
from mewbo_graph.wiki.refresh import ChangeDetector
from mewbo_graph.wiki.store import JsonWikiStore

SLUG = "org/repo"


@pytest.fixture
def store(tmp_path):
    return JsonWikiStore(root_dir=tmp_path / "wiki")


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "clone"
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


def _paths(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


def test_all_added_when_manifest_empty(store, tmp_path) -> None:
    root = _repo(tmp_path, {"a.py": "x", "b.py": "y"})
    cs = ChangeDetector(store).detect(SLUG, root, _paths(root))
    assert set(cs.added) == {"a.py", "b.py"}
    assert cs.modified == []
    assert cs.deleted == []
    assert not cs.is_empty


def test_modified_and_deleted_detected(store, tmp_path) -> None:
    root = _repo(tmp_path, {"a.py": "x", "b.py": "y"})
    det = ChangeDetector(store)
    cs0 = det.detect(SLUG, root, _paths(root))
    # persist the manifest as if we indexed
    store.upsert_file_manifest(
        SLUG,
        [
            FileManifest(slug=SLUG, path=p, content_hash=h)
            for p, h in cs0.current_hashes.items()
        ],
    )
    # mutate a.py, delete b.py, add c.py
    (root / "a.py").write_text("x2", encoding="utf-8")
    (root / "b.py").unlink()
    (root / "c.py").write_text("z", encoding="utf-8")
    cs = det.detect(SLUG, root, _paths(root))
    assert cs.modified == ["a.py"]
    assert cs.added == ["c.py"]
    assert cs.deleted == ["b.py"]
    assert set(cs.dirty) == {"a.py", "c.py"}


def test_no_change_is_empty(store, tmp_path) -> None:
    root = _repo(tmp_path, {"a.py": "x"})
    det = ChangeDetector(store)
    cs0 = det.detect(SLUG, root, _paths(root))
    store.upsert_file_manifest(
        SLUG, [FileManifest(slug=SLUG, path="a.py", content_hash=cs0.current_hashes["a.py"])]
    )
    cs = det.detect(SLUG, root, _paths(root))
    assert cs.is_empty
