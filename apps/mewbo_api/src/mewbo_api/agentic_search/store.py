"""In-memory store for the Agentic Search mock API.

This is the substitution boundary for the real implementation. Each function
here returns the data shape the HTTP layer expects. When the real backend
lands, the bodies swap (Mongo for workspaces, real sub-agent fan-out for
runs) — call sites in ``routes.py`` stay identical.
"""

from __future__ import annotations

import threading
import uuid
from copy import deepcopy
from typing import Any

from . import fixtures

# State guard. Routes are called from Flask request handlers which may run
# in a thread pool, so any mutation needs to be serialised.
_lock = threading.Lock()
_workspaces: dict[str, dict] = {w["id"]: w for w in fixtures.fresh_workspaces()}


# -- Sources ---------------------------------------------------------------


def list_sources() -> list[dict]:
    """Return the static MCP source catalog."""
    return deepcopy(fixtures.SOURCE_CATALOG)


# -- Workspaces ------------------------------------------------------------


def list_workspaces() -> list[dict]:
    """Return all workspaces in seeded/insertion order."""
    with _lock:
        return [deepcopy(w) for w in _workspaces.values()]


def get_workspace(workspace_id: str) -> dict | None:
    """Return one workspace by id, or None if absent."""
    with _lock:
        ws = _workspaces.get(workspace_id)
        return deepcopy(ws) if ws is not None else None


def create_workspace(payload: dict) -> dict:
    """Create a new workspace and return it.

    The id is server-generated (``ws-<uuid>``) so the FE never has to
    invent one.
    """
    workspace_id = f"ws-{uuid.uuid4().hex[:8]}"
    workspace = {
        "id": workspace_id,
        "name": payload.get("name") or "Untitled workspace",
        "desc": payload.get("desc") or "",
        "sources": list(payload.get("sources") or []),
        "instructions": payload.get("instructions") or "",
        "created": _today_label(),
        "past_queries": [],
    }
    with _lock:
        _workspaces[workspace_id] = workspace
        return deepcopy(workspace)


def update_workspace(workspace_id: str, payload: dict) -> dict | None:
    """Apply a partial update to a workspace; return the new state."""
    with _lock:
        existing = _workspaces.get(workspace_id)
        if existing is None:
            return None
        for key in ("name", "desc", "sources", "instructions"):
            if key in payload and payload[key] is not None:
                existing[key] = payload[key]
        return deepcopy(existing)


def delete_workspace(workspace_id: str) -> bool:
    """Remove a workspace; return True if it existed."""
    with _lock:
        return _workspaces.pop(workspace_id, None) is not None


# -- Runs ------------------------------------------------------------------


def run_search(workspace_id: str, query: str) -> dict | None:
    """Build a canned run payload tailored to the workspace's sources.

    The mock returns the same demo answer for every query but filters the
    streamed ``results`` and ``trace`` to the workspace's enabled sources,
    so each workspace produces a coherent view. Each run also appends to
    the workspace's ``past_queries`` history.
    """
    with _lock:
        workspace = _workspaces.get(workspace_id)
        if workspace is None:
            return None
        enabled = set(workspace["sources"])

    results = [deepcopy(r) for r in fixtures.DEMO_RESULTS if r["source"] in enabled]
    trace = [deepcopy(a) for a in fixtures.DEMO_TRACE if a["source_id"] in enabled]

    answer = deepcopy(fixtures.DEMO_ANSWER)
    visible_ids = {r["id"] for r in results}
    answer["bullets"] = [
        {**bullet, "cites": [c for c in bullet["cites"] if c in visible_ids]}
        for bullet in answer["bullets"]
        if any(c in visible_ids for c in bullet["cites"])
    ]
    answer["sources_count"] = len(results)

    payload: dict[str, Any] = {
        "run_id": f"run-{uuid.uuid4().hex[:10]}",
        "query": query,
        "workspace_id": workspace_id,
        "total_ms": fixtures.DEMO_TOTAL_MS,
        "answer": answer,
        "results": results,
        "trace": trace,
        "related_questions": list(fixtures.DEMO_RELATED_QUESTIONS),
        "related_people": deepcopy(fixtures.DEMO_RELATED_PEOPLE),
    }

    _record_past_query(workspace_id, query, len(results))
    return payload


def _record_past_query(workspace_id: str, query: str, results_count: int) -> None:
    """Prepend a new entry to the workspace's history (capped at 10)."""
    with _lock:
        workspace = _workspaces.get(workspace_id)
        if workspace is None:
            return
        history = workspace.setdefault("past_queries", [])
        history.insert(0, {"q": query, "when": "just now", "results": results_count})
        del history[10:]


def _today_label() -> str:
    """Format like 'Apr 27, 2026' to match the seeded ``created`` field."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%b %d, %Y")


def reset_for_tests() -> None:
    """Drop all in-memory state and reseed. Test-only helper."""
    global _workspaces
    with _lock:
        _workspaces = {w["id"]: w for w in fixtures.fresh_workspaces()}
