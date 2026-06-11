"""Loader for the bundled scg AgentDef playbooks (trusted prompt extensions).

The scg AgentDef markdown ships with the ``scg`` plugin suite in
``mewbo_graph.plugins.scg`` (the library whose substrate the tools wrap); the
api-side lifecycle glue reads a playbook body here and passes it as
``skill_instructions`` — the ONLY trusted system-prompt extension. Untrusted
input (source descriptors, workspace instructions) never travels through this.
"""

from __future__ import annotations

from mewbo_core.agent_registry import parse_agent_file
from mewbo_core.common import get_logger

try:
    from mewbo_graph import plugins_root
except ImportError:  # the optional `wiki` extra is absent on a base install
    plugins_root = None

logging = get_logger(name="api.agentic_search.scg.playbooks")

# Directory of the bundled scg AgentDef markdown, resolved from the graph
# package's own plugin root (robust across wheels / editable / source trees).
_SCG_AGENTS_DIR = plugins_root() / "scg" / "agents" if plugins_root else None


def load_playbook(agent_name: str) -> str:
    """Read the ``<agent_name>.md`` AgentDef body. Empty string if missing."""
    if _SCG_AGENTS_DIR is None:
        logging.warning("mewbo-graph not installed; no playbook for {}", agent_name)
        return ""
    agent_md = _SCG_AGENTS_DIR / f"{agent_name}.md"
    if not agent_md.exists():  # pragma: no cover — bundled with the package
        logging.warning("{} not found at {}", agent_md.name, agent_md)
        return ""
    agent_def = parse_agent_file(agent_md, source="plugin:scg")
    return agent_def.body if agent_def else ""


__all__ = ["load_playbook"]
