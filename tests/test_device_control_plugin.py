#!/usr/bin/env python3
"""The device-control plugin's capability contract.

The playbook only ever reaches a model if THREE names agree: the plugin
manifest's ``requires-capabilities``, the skill frontmatter's, and the string
the Android client puts on ``X-Mewbo-Capabilities``. A mismatch in any of them
is silent — the skill is simply never activated, and a screenshot-driven run
proceeds without the guidance that makes the two-tool split pay for itself.

That is precisely how it was first written, so these pin it.
"""

from __future__ import annotations

from pathlib import Path

import mewbo_core.builtin_plugins as builtin_plugins
from mewbo_core.tooling.plugins import discover_builtin_plugins

CAPABILITY = "device_control"

_ROOT = Path(builtin_plugins.__file__).parent
_PLUGIN = _ROOT / "device_control"


def _components():
    found = [
        pc for pc in discover_builtin_plugins(_ROOT) if pc.manifest.name == "device-control"
    ]
    assert found, "the device-control plugin was not discovered at all"
    return found[0]


class TestDeviceControlPlugin:
    def test_the_plugin_is_discovered_as_a_builtin(self):
        assert _components().manifest.name == "device-control"

    def test_the_manifest_gates_on_the_device_control_capability(self):
        assert _components().manifest.requires_capabilities == (CAPABILITY,)

    def test_the_skill_directory_is_discovered(self):
        # A SKILL.md that discovery does not find is a file nobody reads.
        skill_dirs = _components().skill_dirs
        assert skill_dirs, "no skills/ directory was discovered for device-control"
        assert (Path(skill_dirs[0]) / "device-control" / "SKILL.md").is_file()

    def test_the_skill_frontmatter_gates_on_the_SAME_capability(self):
        # Manifest and frontmatter are two independent declarations of one fact.
        text = (_PLUGIN / "skills" / "device-control" / "SKILL.md").read_text()
        assert f'requires-capabilities: ["{CAPABILITY}"]' in text

    def test_the_android_client_advertises_that_exact_string(self):
        # The third declaration, and the one that was missing: without it the
        # skill is never activated and the plugin is dead weight.
        module = (
            Path(__file__).resolve().parents[1]
            / "apps/mewbo_aura/app/src/main/java/com/mewbo/aura/di/DataModule.kt"
        )
        assert f'DEVICE_CONTROL_CAPABILITY_ID = "{CAPABILITY}"' in module.read_text()

    def test_the_skill_teaches_that_apps_are_driven_ONE_at_a_time(self):
        # Android shows one foreground app, so a run that opens several and works
        # them in parallel is acting on screens that are not there. Observed on a
        # real device, and cheap to state once in the playbook — a tool schema
        # carrying it is re-sent at full price on every call.
        # This sentence is the anchor: reword the paragraph around it, keep it.
        text = (_PLUGIN / "skills" / "device-control" / "SKILL.md").read_text().lower()
        assert "one app is on screen at a time" in text

    def test_the_skill_teaches_the_cost_asymmetry_it_exists_for(self):
        # The two-tool split's whole advantage over a screenshot-only harness is
        # that the model CAN skip the image. If the playbook does not say so,
        # the split buys nothing and this plugin has no reason to exist.
        text = (_PLUGIN / "skills" / "device-control" / "SKILL.md").read_text().lower()
        assert "screenshot" in text
        assert "element list" in text
        assert "index" in text
