"""Tripwire: first-party plugin manifests have stable machine and display identities.

Plugin ``name`` is a durable machine identifier: skills record it as
``plugin:<name>``. ``display_name`` is the separately editable label a client
shows to people. Scanning the source-tree manifests keeps a newly added
first-party suite from silently omitting either half.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _first_party_manifests() -> dict[str, dict[str, object]]:
    """Read every plugin manifest shipped from a first-party source package."""
    manifests = [
        *REPO_ROOT.glob("packages/*/src/**/.claude-plugin/plugin.json"),
        *REPO_ROOT.glob("apps/*/src/**/.claude-plugin/plugin.json"),
    ]
    found: dict[str, dict[str, object]] = {}
    for manifest in manifests:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):  # pragma: no cover - unreadable manifest
            continue
        if isinstance(data, dict):
            found[str(manifest.relative_to(REPO_ROOT))] = data
    return found


class TestFirstPartyPluginManifestIdentity:
    def test_manifests_were_found(self) -> None:
        """Guard the scan: an empty glob would make identity checks vacuous."""
        assert _first_party_manifests(), (
            "Found zero first-party plugin manifests. The source-tree layout or "
            "_first_party_manifests() glob changed, so the identity checks below "
            "assert nothing. Update the discovery glob."
        )

    def test_every_manifest_sets_a_display_name(self) -> None:
        missing = []
        for path, manifest in _first_party_manifests().items():
            display_name = manifest.get("display_name")
            if not isinstance(display_name, str) or not display_name.strip():
                missing.append(path)
        assert not missing, (
            "First-party plugin manifests need a non-empty `display_name` for "
            "client-facing lists:\n" + "\n".join(f"  {path}" for path in sorted(missing))
        )

    def test_every_manifest_name_is_lower_kebab_case(self) -> None:
        invalid: dict[str, object] = {}
        for path, manifest in _first_party_manifests().items():
            name = manifest.get("name")
            if not isinstance(name, str) or _NAME_PATTERN.fullmatch(name) is None:
                invalid[path] = name
        assert not invalid, (
            "First-party plugin machine names must be lower kebab-case because "
            "they are durable `plugin:<name>` source identifiers:\n"
            + "\n".join(
                f"  {path}: {name!r}" for path, name in sorted(invalid.items())
            )
        )
