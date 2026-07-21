#!/usr/bin/env python3
"""Modal screens for the Mewbo TUI session UX.

In-app ``ModalScreen`` surfaces — the session switcher (:mod:`.resume`), the
full transcript view (:mod:`.transcript_screen`) and reusable picker dialogs
(:mod:`.dialogs`) — that replace the blocking ``DialogFactory`` apps now that
the CLI runs inside ONE Textual loop. ``DialogFactory`` stays the plain
(no-TTY) fallback; these are the in-app path.
"""

from __future__ import annotations

__all__: list[str] = []
