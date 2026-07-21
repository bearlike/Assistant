#!/usr/bin/env python3
"""Typed response contracts — an endpoint's SUCCESS shape is a model, not a dict.

The sibling of ``errors.py``. Together they close the contract: every endpoint
answers with a validated Pydantic model whether it succeeds or refuses, so
"what does this route return?" is answered by a type rather than by reading the
handler's ``return`` statements.

Two pieces, deliberately only two:

* :class:`ApiResponse` — the base every response model inherits. It carries
  ``extra="forbid"`` and, more usefully, **owns its own serialization**. The
  ``body.model_dump(mode="json"), 200`` pair was repeated at every return site;
  the mode and the status now live on the model, which is the only place that
  knows how the shape should reach the wire.
* :class:`Page` — the ONE paginated envelope. ``{items, total, limit, offset}``
  was independently redeclared per resource (four times on the IAM surface
  alone), which is exactly the drift the house rules forbid: four copies of one
  contract, each free to gain a field the others lack. One generic model over
  the item type replaces them, and a client parses one pager everywhere.

``mode="json"`` is not incidental. These models hold ``datetime`` fields
(``created_at``/``updated_at``) and tuples; ``mode="json"`` is what renders them
as ISO-8601 strings and lists rather than Python objects Flask cannot serialize.
A response model that dumped in ``"python"`` mode would 500 at ``jsonify``.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

#: The item type carried by a :class:`Page`.
ItemT = TypeVar("ItemT")


class ApiResponse(BaseModel):
    """Base for every endpoint SUCCESS body. Serializes itself.

    ``extra="forbid"`` is as load-bearing here as it is on a request model, for
    a different reason: on the way OUT it is what makes a field the handler
    invented — a debug value, a server-owned id, a half-migrated rename — fail
    at construction instead of silently reaching a client and becoming a
    contract someone then depends on.
    """

    model_config = ConfigDict(extra="forbid")

    def body(self) -> dict[str, Any]:
        """The JSON-ready wire body for this response."""
        return self.model_dump(mode="json")

    def response(self, status: int = 200) -> tuple[dict[str, Any], int]:
        """``(body, status)`` — the tuple a Flask handler returns.

        Mirrors :meth:`~mewbo_api.errors.ApiError.response` exactly, so a handler
        returns the same shape on the success path and the refusal path and a
        reader never has to check which one they are looking at.
        """
        return self.body(), status

    def created(self) -> tuple[dict[str, Any], int]:
        """``(body, 201)`` — the response for a resource this request minted."""
        return self.response(201)


class Page(ApiResponse, Generic[ItemT]):
    """The ONE paginated envelope: ``{items, total, limit, offset}``.

    ``total`` counts the FILTERED set, not the page — that is what lets a client
    render a pager without a second request, and it is the invariant every
    consumer of this envelope already relies on. Keep it that way: a ``total``
    that counted the page would make the field useless and the bug silent.

    Generic over the item type, so ``Page[UserView]`` validates its items as
    users while sharing one envelope definition. Parameterizing with a
    discriminated union (``Page[AuthAuditEventUnion]``) also works, and each
    variant then serializes with exactly the fields it owns.
    """

    items: tuple[ItemT, ...] = Field(default=(), description="This page's items, in order.")
    total: int = Field(description="Size of the whole filtered set, not of this page.")
    limit: int = Field(description="Page size actually applied, after clamping.")
    offset: int = Field(description="Index this page starts at.")


__all__ = ["ApiResponse", "ItemT", "Page"]
