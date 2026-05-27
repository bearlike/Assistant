"""Source Capability Graph (SCG) — search-owned reachability graph.

The SCG indexes *reachability* (schemas + pathways, **never data**) behind the
Agentic Search runner so traversal can deploy sub-agents along qualified paths.
The whole feature ships behind the ``scg.enabled`` config flag (default off).

See ``apps/mewbo_api/src/mewbo_api/agentic_search/CLAUDE.md`` and Gitea
issue ``bearlike/Assistant#19`` for the spec.
"""

from __future__ import annotations
