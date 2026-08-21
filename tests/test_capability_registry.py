"""Tripwire: the capability registry vs its three hand-mirrors and the manifests.

A capability id is a bare string that a client, a server and a plugin manifest
must all spell identically. Nothing in a compiler or a type checker connects a
Kotlin ``const val``, a TypeScript ``const`` and a python ``Literal``, so the
only thing that can catch a rename, a typo or a half-landed addition is a test
that reads all three FROM SOURCE and compares them.

This is the capability equivalent of
``apps/mewbo_console/src/utils/__tests__/agentStatusAlignment.test.ts``, which
pins ``agentStatus.ts`` against ``hypervisor.py``'s ``AgentStatus``. Same
deliberate choice: a hand-mirror behind a tripwire, NOT a codegen pipeline or a
shared JSON artifact, because the mirrors are six-line constant blocks and a
build step would cost more than it protects.

**Direction of the assertions, which is the part worth understanding.** Every id
a client advertises, or a first-party manifest requires, must EXIST in the
registry — that is what catches a typo and a drifted rename. The converse is NOT
asserted: a client is not required to advertise every capability (the console has
no screen to drive, Aura renders no wiki), so demanding parity in that direction
would fail for correct code.

**The registry is not a wire validator.** An id outside it is still accepted at
the header (see ``parse_capability_header``), because a third-party plugin may
ship its own. What the registry closes is the FIRST-PARTY set.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from mewbo_core.capabilities import (
    ALL_CAPABILITIES,
    SPEECH_CAPTURE_CAPABILITY,
    SPEECH_PLAYBACK_CAPABILITY,
    parse_capability_header,
    serialize_capabilities,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

CONSOLE_CAPABILITIES_TS = (
    REPO_ROOT / "apps/mewbo_console/src/api/capabilities.ts"
)
AURA_DATA_MODULE_KT = (
    REPO_ROOT
    / "apps/mewbo_aura/app/src/main/java/com/mewbo/aura/di/DataModule.kt"
)

# Where a human has to go when one of these fails. A tripwire whose message only
# says "sets differ" costs the next person the whole investigation this test was
# written to skip.
_EDIT_SITES = f"""
  registry (the source of truth):
    packages/mewbo_core/src/mewbo_core/capabilities.py
  console mirror:
    {CONSOLE_CAPABILITIES_TS.relative_to(REPO_ROOT)}
  Aura mirror (the `companion object` inside AuthInterceptor):
    {AURA_DATA_MODULE_KT.relative_to(REPO_ROOT)}
""".rstrip()


def _console_ids() -> set[str]:
    """Every capability id the console mirror declares, read from its source.

    Matches both spellings the file uses: a bare literal
    (``const ASK_USER_CAPABILITY_ID = "ask_user"``) and the build-time-overridable
    form (``... = (import.meta.env.X as string | undefined) || "stlite"``). The
    literal captured is the DEFAULT that ships, which is the one a deployment
    without the env var actually sends.
    """
    source = CONSOLE_CAPABILITIES_TS.read_text(encoding="utf-8")
    pattern = re.compile(
        r"^export const \w*CAPABILITY_ID\s*=\s*(?:.*\|\|\s*)?\"([a-z_]+)\"",
        re.MULTILINE,
    )
    return set(pattern.findall(source))


def _aura_ids() -> set[str]:
    """Every capability id the Aura mirror declares, read from its source."""
    source = AURA_DATA_MODULE_KT.read_text(encoding="utf-8")
    pattern = re.compile(r"const val \w*CAPABILITY_ID\s*=\s*\"([a-z_]+)\"")
    return set(pattern.findall(source))


def _aura_advertised_ids() -> set[str]:
    """The ids Aura actually ADDS to the header, not merely declares.

    A constant that is declared and never added is dead — the capability would be
    silently un-advertised while the mirror looks complete. So this parses the
    ``buildList { ... }`` body rather than the companion object.
    """
    source = AURA_DATA_MODULE_KT.read_text(encoding="utf-8")
    block = re.search(r"buildList \{(.*?)\n\s*\}\.sorted\(\)", source, re.DOTALL)
    assert block is not None, (
        "Could not find the `buildList { ... }.sorted()` capability block in "
        f"{AURA_DATA_MODULE_KT.relative_to(REPO_ROOT)}. If the header assembly "
        "was restructured, update this parser in the SAME change."
    )
    names = set(re.findall(r"add\((\w*CAPABILITY_ID)\)", block.group(1)))
    declared = dict(
        re.findall(
            r"const val (\w*CAPABILITY_ID)\s*=\s*\"([a-z_]+)\"",
            source,
        )
    )
    return {declared[name] for name in names if name in declared}


def _manifest_required_ids() -> dict[str, set[str]]:
    """``requires-capabilities`` from every first-party plugin manifest.

    Keyed by the manifest's repo-relative path so a failure names the file to fix.
    Only first-party manifests are in the tree, which is exactly the set the
    registry claims to close.
    """
    found: dict[str, set[str]] = {}
    for manifest in REPO_ROOT.glob(
        "packages/*/src/**/.claude-plugin/plugin.json"
    ):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):  # pragma: no cover - unreadable manifest
            continue
        required = data.get("requires-capabilities")
        if isinstance(required, str):
            required = [required]
        if isinstance(required, list) and required:
            found[str(manifest.relative_to(REPO_ROOT))] = {
                str(c) for c in required if isinstance(c, str)
            }
    return found


class TestConsoleMirror:
    def test_console_declares_at_least_one_id(self) -> None:
        """Guard the PARSER, not just the data.

        A regex that silently matches nothing would make every other assertion in
        this class vacuously true — the classic way a tripwire stops testing its
        subject while still reporting green.
        """
        assert _console_ids(), (
            "Parsed ZERO capability ids out of "
            f"{CONSOLE_CAPABILITIES_TS.relative_to(REPO_ROOT)}. The file's "
            "declaration style changed and this test's regex no longer matches, "
            "so it is asserting nothing. Fix the regex in _console_ids()."
        )

    def test_every_console_id_is_in_the_registry(self) -> None:
        unknown = _console_ids() - set(ALL_CAPABILITIES)
        assert not unknown, (
            f"The console advertises capability id(s) the server registry does "
            f"not know: {sorted(unknown)}.\n"
            f"A capability the server has never heard of gates NOTHING — the "
            f"feature behind it is silently dead, with no error anywhere.\n"
            f"Either fix the spelling in the console mirror, or add the id to "
            f"the registry (and to `Capability`, the `Literal`).\n{_EDIT_SITES}"
        )


class TestAuraMirror:
    def test_aura_declares_at_least_one_id(self) -> None:
        """Same parser guard as the console's — see that test's docstring."""
        assert _aura_ids(), (
            "Parsed ZERO capability ids out of "
            f"{AURA_DATA_MODULE_KT.relative_to(REPO_ROOT)}. The declaration "
            "style changed and this test's regex no longer matches. Fix the "
            "regex in _aura_ids()."
        )

    def test_every_aura_id_is_in_the_registry(self) -> None:
        unknown = _aura_ids() - set(ALL_CAPABILITIES)
        assert not unknown, (
            f"Aura advertises capability id(s) the server registry does not "
            f"know: {sorted(unknown)}.\n"
            f"A capability the server has never heard of gates NOTHING — the "
            f"feature behind it is silently dead, with no error anywhere.\n"
            f"Either fix the spelling in the Aura mirror, or add the id to the "
            f"registry (and to `Capability`, the `Literal`).\n{_EDIT_SITES}"
        )

    def test_every_declared_aura_id_is_actually_advertised(self) -> None:
        """A declared-but-never-added constant is a capability that never ships."""
        declared = _aura_ids()
        advertised = _aura_advertised_ids()
        orphaned = declared - advertised
        assert not orphaned, (
            f"Aura DECLARES capability id(s) it never adds to the header: "
            f"{sorted(orphaned)}.\n"
            f"The constant exists, so the mirror looks complete and this "
            f"capability is still never sent — the server never gates on it.\n"
            f"Add it inside the `buildList {{ ... }}` in "
            f"{AURA_DATA_MODULE_KT.relative_to(REPO_ROOT)}, or delete the "
            f"constant."
        )


class TestPluginManifests:
    def test_manifests_were_found(self) -> None:
        """Parser guard — an empty glob would make the next test vacuous."""
        assert _manifest_required_ids(), (
            "Found ZERO plugin manifests declaring `requires-capabilities`. The "
            "glob in _manifest_required_ids() no longer matches the tree layout, "
            "so the next assertion is vacuously true."
        )

    def test_every_manifest_capability_is_in_the_registry(self) -> None:
        """A manifest naming an id the registry lacks is a permanently dead gate."""
        offenders = {
            path: sorted(ids - set(ALL_CAPABILITIES))
            for path, ids in _manifest_required_ids().items()
            if ids - set(ALL_CAPABILITIES)
        }
        assert not offenders, (
            "Plugin manifest(s) require a capability the registry does not "
            "know:\n"
            + "\n".join(f"  {path}: {ids}" for path, ids in sorted(offenders.items()))
            + "\nNo client can advertise an id the registry never named, so the "
            "plugin behind it can never load.\n"
            f"{_EDIT_SITES}"
        )


class TestSpeechCapabilitiesReachedBothClients:
    """The speech pair is the newest addition, so pin that it landed everywhere.

    Generic set-comparison tests above pass when an id is added to the registry
    and NEITHER client — the half-landed case. These name the two ids explicitly,
    which is the only way to catch that.
    """

    @pytest.mark.parametrize(
        "capability",
        [SPEECH_PLAYBACK_CAPABILITY, SPEECH_CAPTURE_CAPABILITY],
    )
    def test_console_advertises_speech(self, capability: str) -> None:
        assert capability in _console_ids(), (
            f"The console does not advertise {capability!r}. Its speech control "
            f"is rendered but the server is never told the client can service "
            f"it.\nAdd it in {CONSOLE_CAPABILITIES_TS.relative_to(REPO_ROOT)} "
            f"and include it in CLIENT_CAPABILITIES."
        )

    @pytest.mark.parametrize(
        "capability",
        [SPEECH_PLAYBACK_CAPABILITY, SPEECH_CAPTURE_CAPABILITY],
    )
    def test_aura_advertises_speech(self, capability: str) -> None:
        assert capability in _aura_advertised_ids(), (
            f"Aura does not advertise {capability!r} on the header.\nAdd it to "
            f"the `buildList` in "
            f"{AURA_DATA_MODULE_KT.relative_to(REPO_ROOT)}."
        )


class TestWireSeam:
    """``parse_capability_header`` / ``serialize_capabilities`` are one contract."""

    def test_serialize_then_parse_round_trips(self) -> None:
        original = ["stlite", "apps", "ask_user"]
        assert parse_capability_header(serialize_capabilities(original)) == (
            "apps",
            "ask_user",
            "stlite",
        )

    def test_parse_normalises_spacing_order_and_duplicates(self) -> None:
        """A client's spacing and ordering must not change what the server sees."""
        assert parse_capability_header("  stlite ,apps,  stlite , ,apps ") == (
            "apps",
            "stlite",
        )

    def test_empty_header_is_no_capabilities(self) -> None:
        assert parse_capability_header("") == ()
        assert parse_capability_header("   ") == ()
        assert parse_capability_header(",,,") == ()

    def test_non_string_header_is_no_capabilities(self) -> None:
        assert parse_capability_header(None) == ()
        assert parse_capability_header(["stlite"]) == ()

    def test_unknown_ids_are_KEPT_not_dropped(self) -> None:
        """The load-bearing one: a third-party plugin's capability must survive.

        The operator-facing capability list is computed from INSTALLED MANIFESTS
        (``value_sources.py:_capabilities``), so an id this registry has never
        heard of is legitimate traffic. Filtering to the first-party registry
        here would silently disable every third-party gate, and a newer client
        talking to an older server would lose features with nothing logged as an
        error.
        """
        assert parse_capability_header("stlite,acme_custom_thing") == (
            "acme_custom_thing",
            "stlite",
        )

    def test_serialize_dedupes_and_sorts(self) -> None:
        assert serialize_capabilities(["stlite", "apps", "stlite"]) == "apps,stlite"

    def test_serialize_drops_blanks(self) -> None:
        assert serialize_capabilities(["stlite", "", "  "]) == "stlite"


class TestRegistryShape:
    def test_all_capabilities_matches_the_literal(self) -> None:
        """``ALL_CAPABILITIES`` and ``Capability`` must not drift apart.

        They are two hand-written spellings of one set, and adding an id to the
        ``Literal`` while forgetting the frozenset would leave the tripwire above
        blind to it.
        """
        from typing import get_args

        from mewbo_core.capabilities import Capability

        assert set(get_args(Capability)) == set(ALL_CAPABILITIES)

    def test_ids_are_lower_snake_case(self) -> None:
        """The wire format has no escaping, so a comma or space in an id is fatal."""
        for capability in ALL_CAPABILITIES:
            assert re.fullmatch(r"[a-z][a-z0-9_]*", capability), (
                f"{capability!r} is not lower_snake_case. The header is a bare "
                f"comma-separated list with no quoting, so a separator or space "
                f"inside an id silently splits it into two."
            )
