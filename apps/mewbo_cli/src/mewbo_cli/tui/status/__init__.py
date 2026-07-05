#!/usr/bin/env python3
"""Status-line / context-meter subpackage for the Mewbo TUI (issue #156, epic #149).

Holds the file-contract status surfaces that hang off the second sidebar slot:

- :mod:`context_meter` — :class:`ContextMeter`: tokens → context-window % (config
  lookup + estimate fallback) and tokens → cost ($), with honest markers.
- :mod:`statusline` — :class:`StatusLineRunner`: build the stable JSON payload,
  run a user script with that JSON on stdin, capture stdout, refresh on interval.
- :mod:`terminal_title` — :func:`set_terminal_title`: emit an OSC title sequence.
- :mod:`install` — :func:`make_sidebar_installer`: register the faceted sidebar
  sections (Fleet · Plan · Context) and start the statusline interval + title.

The :class:`~mewbo_cli.tui.widgets.fleet_panel.FleetPanel`,
:class:`~mewbo_cli.tui.widgets.todo_panel.TodoPanel` and
:class:`~mewbo_cli.tui.widgets.status_bar.StatusBar` widgets render these.
"""

from __future__ import annotations
