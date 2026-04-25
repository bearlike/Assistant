"""PlantUML diagram card — Streamlit-native, KISS.

Renders any PlantUML diagram inline by forwarding the source to a
PlantUML server (public or self-hosted) and displaying the returned SVG
via ``st.image``.  No Java, no subprocess — just a URL.

Copy-paste contract
-------------------
Everything between ``# ── PlantUMLCard ─`` and
``# ── end PlantUMLCard ─`` is the reusable block.  Extract with::

    awk '/^# ── PlantUMLCard ─/,/^# ── end PlantUMLCard ─/' \\
        plantuml_card.py >> app.py

Call as ``PlantUMLCard(diagram_dict).render()``.

Preview standalone: ``streamlit run plantuml_card.py``.

Agent-side lint (call before submitting the widget)
---------------------------------------------------
``lint_plantuml(source)`` hits the server's ``/check`` endpoint and
reads the ``X-PlantUML-Diagram-Error`` / ``X-PlantUML-Diagram-Error-Line``
response headers — giving you the exact error message and line number
without any regex guessing.  Returns an empty list when the diagram is
valid.

Run from aider_shell_tool::

    python "$CLAUDE_PLUGIN_ROOT/examples/components/plantuml_card.py" \\
        --lint "@startuml\\nAlice -> Bob: Hello\\n@enduml"

Or call the function directly in a one-liner::

    python -c "
    import sys; sys.path.insert(0, '$CLAUDE_PLUGIN_ROOT/examples/components')
    from plantuml_card import lint_plantuml
    errors = lint_plantuml(open('source.puml').read())
    if errors: [print(e) for e in errors]; sys.exit(1)
    print('OK')
    "
"""

from __future__ import annotations

from typing import TypedDict

import streamlit as st

# ── PlantUMLCard ──────────────────────────────────────────────────────────

_PLANTUML_DEFAULT_SERVER = "https://www.plantuml.com/plantuml"


class PlantUMLDiagram(TypedDict, total=False):
    """State for :class:`PlantUMLCard`.  Only ``source`` is required."""

    source: str      # PlantUML source text
    title: str       # optional bold header above the diagram
    caption: str     # optional muted footer below
    server_url: str  # swap for a self-hosted server; default is plantuml.com


class PlantUMLCard:
    """One atomic card that renders a PlantUML diagram fetched from a server."""

    def __init__(self, diagram: PlantUMLDiagram) -> None:
        self.diagram = diagram

    @staticmethod
    def _image_url(source: str, server_url: str) -> str:
        encoded = source.encode("utf-8").hex()
        return f"{server_url.rstrip('/')}/svg/~h{encoded}"

    def render(self) -> None:
        d = self.diagram
        source = (d.get("source") or "").strip()
        server = d.get("server_url") or _PLANTUML_DEFAULT_SERVER

        with st.container(border=True):
            if title := d.get("title"):
                st.markdown(f"**{title}**")
            if not source:
                st.warning("No diagram source provided.")
                return
            st.image(PlantUMLCard._image_url(source, server))
            if caption := d.get("caption"):
                st.caption(caption)


# ── end PlantUMLCard ──────────────────────────────────────────────────────


# Agent-side validation — NOT part of the copy-paste block.
# Uses urllib (stdlib); requires network; runs at widget build time, not in stlite.

def lint_plantuml(
    source: str,
    server_url: str = _PLANTUML_DEFAULT_SERVER,
) -> list[str]:
    """Validate PlantUML source via the server's /check endpoint.

    Reads ``X-PlantUML-Diagram-Error`` and ``X-PlantUML-Diagram-Error-Line``
    response headers — no body parsing, no regex.

    Returns a list of ``"line N: <message>"`` strings; empty list means valid.
    """
    from urllib.error import URLError
    from urllib.request import urlopen  # noqa: TID251

    encoded = source.encode("utf-8").hex()
    url = f"{server_url.rstrip('/')}/check/~h{encoded}"
    try:
        with urlopen(url, timeout=10) as resp:
            msg = resp.headers.get("X-PlantUML-Diagram-Error", "")
            line = resp.headers.get("X-PlantUML-Diagram-Error-Line", "")
    except URLError as exc:
        return [f"Server unreachable: {exc}"]
    if not msg:
        return []
    return [f"line {line}: {msg}" if line else msg]


# Canonical demo payloads — outside the copy region; the agent brings its own.
_SEQUENCE: PlantUMLDiagram = {
    "source": (
        "@startuml\n"
        "Alice -> Bob: Authentication Request\n"
        "Bob --> Alice: Authentication Response\n"
        "Alice -> Bob: Another authentication Request\n"
        "Alice <-- Bob: Another authentication Response\n"
        "@enduml"
    ),
    "title": "Authentication Sequence",
    "caption": "Rendered via plantuml.com",
}
_CLASS: PlantUMLDiagram = {
    "source": (
        "@startuml\n"
        "class Animal {\n"
        "  +name: str\n"
        "  +speak(): str\n"
        "}\n"
        "class Dog {\n"
        "  +speak(): str\n"
        "}\n"
        "Animal <|-- Dog\n"
        "@enduml"
    ),
    "title": "Class hierarchy",
}


if __name__ == "__main__":
    import sys

    if "--lint" in sys.argv:
        idx = sys.argv.index("--lint")
        if idx + 1 >= len(sys.argv):
            print("Usage: plantuml_card.py --lint '<plantuml source>'", file=sys.stderr)
            sys.exit(2)
        errors = lint_plantuml(sys.argv[idx + 1])
        if errors:
            for e in errors:
                print(e)
            sys.exit(1)
        print("OK: diagram is valid")
        sys.exit(0)

    st.set_page_config(page_title="PlantUML card", layout="centered")
    st.title("PlantUML card — demo")
    PlantUMLCard(_SEQUENCE).render()
    st.write("")
    PlantUMLCard(_CLASS).render()
