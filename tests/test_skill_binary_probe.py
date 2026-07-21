"""A skill is only advertised when the executables it drives are installed.

The failure this closes: a skill was advertised to a run whose backing binary
was absent. The model selected it, followed instructions that shell out, and
spent steps on a not-found error it had no way to resolve.
"""

from __future__ import annotations

import pytest
from mewbo_core.skills import SkillBinaryProbe, SkillRegistry, _parse_skill_file


@pytest.fixture(autouse=True)
def _clean_probe_cache():
    SkillBinaryProbe.reset_cache()
    yield
    SkillBinaryProbe.reset_cache()


@pytest.fixture()
def installed(monkeypatch):
    """Control PATH resolution; records every lookup so caching is observable."""
    present: set[str] = set()
    lookups: list[str] = []

    def _which(binary: str):
        lookups.append(binary)
        return f"/usr/bin/{binary}" if binary in present else None

    monkeypatch.setattr("mewbo_core.skills.shutil.which", _which)
    return present, lookups


def _write_skill(root, name: str, frontmatter: str = "") -> None:
    directory = root / ".claude" / "skills" / name
    directory.mkdir(parents=True)
    header = f"---\nname: {name}\ndescription: does {name} things\n{frontmatter}---\n"
    (directory / "SKILL.md").write_text(f"{header}\nBody for {name}.\n", encoding="utf-8")


def _registry(tmp_path, monkeypatch) -> SkillRegistry:
    # Keep the personal-skills scan off the developer's real home.
    monkeypatch.setattr("mewbo_core.skills.Path.home", classmethod(lambda cls: tmp_path / "home"))
    registry = SkillRegistry()
    registry.load(cwd=str(tmp_path))
    return registry


def test_skill_with_a_missing_binary_is_not_advertised(tmp_path, monkeypatch, installed):
    present, _ = installed
    _write_skill(tmp_path, "tea-cli", "requires-binary: tea\n")
    _write_skill(tmp_path, "plain")
    registry = _registry(tmp_path, monkeypatch)

    advertised = {s.name for s in registry.visible_for(())}

    assert advertised == {"plain"}
    assert "tea-cli" not in registry.render_catalog(())
    assert {s.name for s in registry.list_user_invocable()} == {"plain"}
    assert present == set()


def test_skill_is_advertised_once_its_binary_is_present(tmp_path, monkeypatch, installed):
    present, _ = installed
    present.add("tea")
    _write_skill(tmp_path, "tea-cli", "requires-binary: tea\n")
    registry = _registry(tmp_path, monkeypatch)

    assert {s.name for s in registry.visible_for(())} == {"tea-cli"}
    assert "tea-cli" in registry.render_catalog(())


def test_every_declared_binary_must_be_present(tmp_path, monkeypatch, installed):
    present, _ = installed
    present.add("git")
    _write_skill(tmp_path, "release", "requires-binaries:\n  - git\n  - gh\n")
    registry = _registry(tmp_path, monkeypatch)

    assert registry.visible_for(()) == []


def test_an_explicit_lookup_still_returns_the_skill(tmp_path, monkeypatch, installed):
    """Naming a skill outright is a decision already made — a real error beats
    a skill that silently does not exist."""
    _write_skill(tmp_path, "tea-cli", "requires-binary: tea\n")
    registry = _registry(tmp_path, monkeypatch)

    assert registry.get("tea-cli") is not None


def test_probe_result_is_cached_within_a_turn(tmp_path, monkeypatch, installed):
    """The catalog renders on every LLM step; a PATH scan per skill per step is
    waste. The many renders inside one turn must hit the cache."""
    _, lookups = installed
    _write_skill(tmp_path, "tea-cli", "requires-binary: tea\n")
    registry = _registry(tmp_path, monkeypatch)

    for _ in range(5):
        registry.visible_for(())

    assert lookups == ["tea"]


def test_a_binary_installed_mid_process_is_picked_up_on_the_next_turn(
    tmp_path, monkeypatch, installed
):
    """The cache lifetime is ONE turn, not the process: a binary that appears in
    a long-running server must not stay cached-absent until restart."""
    present, _ = installed
    _write_skill(tmp_path, "tea-cli", "requires-binary: tea\n")
    registry = _registry(tmp_path, monkeypatch)

    assert registry.visible_for(()) == []  # absent this turn

    # The operator installs the binary into the running process, then a new turn
    # begins — maybe_reload is the once-per-run seam that re-probes.
    present.add("tea")
    registry.maybe_reload()

    assert {s.name for s in registry.visible_for(())} == {"tea-cli"}


def test_a_still_absent_binary_is_not_re_probed_within_a_turn(tmp_path, monkeypatch, installed):
    """refresh() only runs at the turn boundary, so within a turn the negative
    result is still cached across the per-step renders."""
    present, lookups = installed
    _write_skill(tmp_path, "tea-cli", "requires-binary: tea\n")
    registry = _registry(tmp_path, monkeypatch)

    registry.maybe_reload()
    for _ in range(5):
        registry.visible_for(())

    assert lookups == ["tea"]  # one probe for the whole turn, despite five renders


def test_absent_binary_reported_once_across_turns(tmp_path, monkeypatch, installed):
    """The per-turn re-probe must not re-report a binary that is simply not there.

    The withheld-log is deduped by ``_REPORTED``, which survives ``refresh()``
    for exactly this reason — asserting on that set (rather than the loguru sink)
    tests the property, not the log plumbing. Each turn STILL re-probes PATH, so
    a growing lookup count with a single ``_REPORTED`` entry is the proof that
    the dedup — not a stale cache — is what suppresses the repeat report.
    """
    _, lookups = installed
    _write_skill(tmp_path, "tea-cli", "requires-binary: tea\n")
    registry = _registry(tmp_path, monkeypatch)

    for _ in range(3):
        registry.maybe_reload()
        registry.visible_for(())

    assert SkillBinaryProbe._REPORTED == {("tea-cli", "tea")}
    assert lookups == ["tea", "tea", "tea"]  # re-probed each turn, reported once


def test_a_skill_declaring_nothing_is_never_probed(tmp_path, monkeypatch, installed):
    _, lookups = installed
    _write_skill(tmp_path, "plain")
    registry = _registry(tmp_path, monkeypatch)

    assert {s.name for s in registry.visible_for(())} == {"plain"}
    assert lookups == []


def test_frontmatter_parses_both_the_list_and_scalar_forms(tmp_path):
    _write_skill(tmp_path, "scalar", "requires-binary: tea\n")
    _write_skill(tmp_path, "listed", "requires-binaries:\n  - gh\n  - git\n")
    base = tmp_path / ".claude" / "skills"

    scalar = _parse_skill_file(base / "scalar" / "SKILL.md", source="project")
    listed = _parse_skill_file(base / "listed" / "SKILL.md", source="project")

    assert scalar is not None and scalar.requires_binaries == ("tea",)
    assert listed is not None and listed.requires_binaries == ("gh", "git")


def test_malformed_declaration_degrades_to_no_requirement(tmp_path):
    _write_skill(tmp_path, "broken", "requires-binaries: 42\n")

    spec = _parse_skill_file(tmp_path / ".claude" / "skills" / "broken" / "SKILL.md", "project")

    assert spec is not None and spec.requires_binaries == ()
