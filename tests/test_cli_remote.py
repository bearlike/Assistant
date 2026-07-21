#!/usr/bin/env python3
"""Tests for the CLI local-first remote seam.

Covers the ``cli_remote`` atomic units — the transcript mirror, the synthesized
Mewbo MCP server entry, and the honest sink strings — plus the ``cli.remote``
config validation and the header's sink indicator. The HTTP boundary is stubbed
(injected poster / patched ``urlopen``); no live server is touched.
"""

from __future__ import annotations

from io import StringIO

import pytest
from mewbo_cli.cli_remote import (
    RemoteTranscriptSync,
    expand_env,
    mewbo_mcp_server_config,
    sink_facet,
    sink_label,
)
from mewbo_cli.tui.widgets.header import HeaderContext, HeaderView
from rich.console import Console

# ── RemoteTranscriptSync ────────────────────────────────────────────────────


def _sync(active, posts, **kw):
    """Build a worker-less sync capturing POSTs into ``posts``."""
    return RemoteTranscriptSync(
        "https://mewbo.example/",
        "tok-123",
        session_id_provider=lambda: active,
        poster=lambda url, payload: posts.append((url, payload)),
        autostart_worker=False,
        **kw,
    )


def test_sync_posts_active_session_event_to_ingest_route():
    """A matching event POSTs to the session's /events ingest route as a batch."""
    posts: list = []
    sync = _sync("sid-1", posts)
    sync("sid-1", {"type": "agent_message", "payload": {"text": "hi"}})
    sync.flush()
    assert len(posts) == 1
    url, payload = posts[0]
    assert url == "https://mewbo.example/api/sessions/sid-1/events"
    assert payload == {"records": [{"type": "agent_message", "payload": {"text": "hi"}}]}


def test_sync_filters_non_active_session():
    """Events for a session other than the live active one are dropped."""
    posts: list = []
    sync = _sync("sid-1", posts)
    sync("sid-OTHER", {"type": "tool_result", "payload": {}})
    sync.flush()
    assert posts == []


def test_sync_batches_multiple_events_into_one_post():
    """A burst of events for one session coalesces into a single POST."""
    posts: list = []
    sync = _sync("sid-1", posts)
    sync("sid-1", {"type": "agent_message_delta", "payload": {"text": "a"}})
    sync("sid-1", {"type": "agent_message_delta", "payload": {"text": "b"}})
    sync.flush()
    assert len(posts) == 1
    _, payload = posts[0]
    assert [r["payload"]["text"] for r in payload["records"]] == ["a", "b"]


def test_sync_no_active_filter_mirrors_all():
    """A None provider disables the filter (mirror every CLI-process session)."""
    posts: list = []
    sync = _sync(None, posts)
    sync("sid-a", {"type": "user", "payload": {}})
    sync("sid-b", {"type": "user", "payload": {}})
    sync.flush()
    assert {url for url, _ in posts} == {
        "https://mewbo.example/api/sessions/sid-a/events",
        "https://mewbo.example/api/sessions/sid-b/events",
    }


def test_sync_failure_is_isolated():
    """A raising poster never propagates — the local transcript stays authoritative."""

    def boom(url, payload):
        raise RuntimeError("remote down")

    sync = RemoteTranscriptSync(
        "https://x", "t", session_id_provider=lambda: "s", poster=boom, autostart_worker=False
    )
    sync("s", {"type": "completion", "payload": {}})
    sync.flush()  # must not raise


def test_sync_enqueue_never_raises_into_publish_path():
    """__call__ swallows a provider error (runs on the append hot path)."""

    def bad_provider():
        raise RuntimeError("provider blew up")

    sync = RemoteTranscriptSync(
        "https://x", "t", session_id_provider=bad_provider, autostart_worker=False
    )
    sync("s", {"type": "user", "payload": {}})  # must not raise


def test_http_post_sends_api_key_header(monkeypatch):
    """The default poster POSTs JSON with the token as X-API-Key (I/O stubbed)."""
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        captured["data"] = req.data
        captured["method"] = req.get_method()
        return object()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    sync = RemoteTranscriptSync(
        "https://x", "sekret", session_id_provider=lambda: None, autostart_worker=False
    )
    sync._http_post("https://x/api/sessions/s/events", {"records": [{"type": "x"}]})
    assert captured["method"] == "POST"
    assert captured["url"] == "https://x/api/sessions/s/events"
    # urllib title-cases header keys; assert the token is present as a value.
    assert "sekret" in captured["headers"].values()
    assert b'"records"' in captured["data"]


# ── MCP server synthesis ────────────────────────────────────────────────────


def test_mewbo_mcp_server_config_shape():
    """The synthesized entry is a streamable_http server with a Bearer header."""
    cfg = mewbo_mcp_server_config("https://mewbo.example/", "tok-9")
    assert cfg == {
        "transport": "streamable_http",
        "url": "https://mewbo.example/mcp",
        "headers": {"Authorization": "Bearer tok-9"},
    }


# ── sink strings ────────────────────────────────────────────────────────────


def test_sink_label_and_facet():
    """The honest indicators reflect enabled vs local-only."""
    assert sink_label(enabled=False, base_url="") == "local-only"
    assert sink_label(enabled=True, base_url="https://m.example/") == "remote: https://m.example"
    assert sink_facet(enabled=True) == "synced"
    assert sink_facet(enabled=False) == "local-only"


def test_expand_env(monkeypatch):
    """expand_env resolves ${VAR}; unset refs degrade to themselves, never raise."""
    monkeypatch.setenv("MEWBO_REMOTE_TOK", "s3cret")
    assert expand_env("${MEWBO_REMOTE_TOK}") == "s3cret"
    assert expand_env("literal") == "literal"


# ── cli.remote config validation ────────────────────────────────────────────


def test_cli_remote_config_extra_forbid():
    """An unknown key in cli.remote is rejected (extra='forbid')."""
    from mewbo_core.config import CliRemoteConfig

    with pytest.raises(Exception):
        CliRemoteConfig.model_validate({"base_url": "x", "bogus": 1})


def test_cli_remote_config_enabled():
    """enabled keys off a non-empty (trimmed) base_url."""
    from mewbo_core.config import CliRemoteConfig

    assert CliRemoteConfig.model_validate({"base_url": "  https://x  "}).enabled
    assert not CliRemoteConfig.model_validate({}).enabled
    assert not CliRemoteConfig.model_validate({"base_url": "   "}).enabled


def test_cli_remote_is_cli_scoped():
    """The remote block hangs off cli only — no other surface exposes it."""
    from mewbo_core.config import AppConfig

    cfg = AppConfig.model_validate({"cli": {"remote": {"base_url": "https://x", "token": "t"}}})
    assert cfg.cli.remote.base_url == "https://x"
    assert not hasattr(cfg, "remote")


# ── header sink indicator ───────────────────────────────────────────────────


def _header_ctx(**overrides) -> HeaderContext:
    base = dict(
        title="Mewbo",
        version="0.0.13",
        status_label="Ready",
        status_color="green",
        model="openai/gpt-5.2",
        session_id="abc123",
        base_url="",
        langfuse_enabled=False,
        langfuse_reason=None,
        builtin_enabled=3,
        builtin_disabled=0,
        external_enabled=1,
        external_disabled=0,
        skill_count=2,
    )
    base.update(overrides)
    return HeaderContext(**base)


def _render(ctx: HeaderContext, width: int = 120) -> str:
    buf = StringIO()
    Console(file=buf, width=width, force_terminal=True).print(HeaderView(ctx).render(width))
    return buf.getvalue()


def test_header_shows_remote_sink():
    """A remote sink renders a 'sink' row naming the remote base."""
    out = _render(_header_ctx(transcript_sink="remote: https://mewbo.example"))
    assert "sink" in out
    assert "remote" in out
    assert "mewbo.example" in out


def test_header_defaults_to_local_only():
    """The default header advertises local-only honesty."""
    out = _render(_header_ctx())
    assert "local-only" in out
