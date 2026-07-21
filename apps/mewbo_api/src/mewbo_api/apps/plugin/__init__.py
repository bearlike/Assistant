"""Plugin-suite anchor for the agent-side Mewbo Apps suite (spec §4B).

``PLUGIN_ROOT`` is THIS package directory, and this directory IS the plugin
suite: its manifest sits at ``plugin/.claude-plugin/plugin.json`` (the
``builtin_plugins/widget_builder/`` layout), so ``plugin/`` itself is the
``mewbo-apps-builder`` suite — the ``app-builder``/``app-repair`` AgentDefs, the
``submit_app``/``app_data`` SessionTools, the injected SDK, the linter and the
reference examples all live directly under it (in ``app_builder/``), NOT in a
sub-suite of it.

Discovery therefore registers the PARENT, not this directory:
``discover_builtin_plugins(root)`` scans a root's IMMEDIATE subdirectories for a
``<suite>/.claude-plugin/plugin.json``, so ``backend.py`` pushes
``PLUGIN_ROOT.parent`` (the ``mewbo_api.apps`` package dir) to
``mewbo_core.plugins.register_builtin_root`` — the down-only seam that mirrors
how ``mewbo_graph`` contributes its ``wiki``/``scg`` suites — and ``plugin/`` is
the one suite it finds inside. See ``backend.py:init_apps`` (authoritative) + the
startup discovery guard there.

This module holds ONLY the ``PLUGIN_ROOT`` constant; nothing else belongs here.
"""

from __future__ import annotations

from pathlib import Path

# This directory IS the suite (manifest at ``plugin/.claude-plugin/plugin.json``).
# The API pushes ``PLUGIN_ROOT.parent`` to ``register_builtin_root`` so discovery
# scans the parent's subdirectories and finds ``plugin/`` as the one suite. Keep
# this the only symbol in this module.
PLUGIN_ROOT: Path = Path(__file__).resolve().parent

__all__ = ["PLUGIN_ROOT"]
