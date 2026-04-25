"""Mock implementation of the Agentic Search REST API.

See ``routes.py`` for the wire contract and ``store.py`` for the in-memory
data layer (the substitution boundary for the real implementation).
"""

from .routes import agentic_ns, init_agentic_search

__all__ = ["agentic_ns", "init_agentic_search"]
