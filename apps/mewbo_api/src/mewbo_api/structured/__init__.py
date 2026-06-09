"""Structured-response REST API — POST /v1/structured.

``init_structured`` wires the namespace and captures the session runtime. See
``routes.py`` for the wire contract; the engine is
``mewbo_core.structured_response.StructuredResponder`` (down-only compose).
"""
from .routes import init_structured, structured_ns

__all__ = ["init_structured", "structured_ns"]
