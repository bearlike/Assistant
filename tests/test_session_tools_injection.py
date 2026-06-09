"""Contract test: an injected SessionTool reaches the loop via run_sync."""
from __future__ import annotations

from unittest.mock import patch

from mewbo_core.session_runtime import SessionRuntime
from mewbo_core.session_store import SessionStore
from mewbo_core.structured_response import EmitStructuredResponseTool


def test_run_sync_forwards_extra_session_tools_to_orchestrate(tmp_path):
    runtime = SessionRuntime(session_store=SessionStore(root_dir=str(tmp_path)))
    sid = runtime.resolve_session()
    emit = EmitStructuredResponseTool(
        session_id=sid, schema={"type": "object", "properties": {}}
    )
    with patch("mewbo_core.session_runtime.orchestrate_session") as mock_orch:
        mock_orch.return_value = object()
        runtime.run_sync(
            user_query="hi",
            session_id=sid,
            extra_session_tools=[emit],
        )
    _, kwargs = mock_orch.call_args
    assert kwargs["extra_session_tools"] == [emit]
