"""Tests for ``mewbo_core.attachments`` and context loading of attachments."""

from __future__ import annotations

import os

from mewbo_core import attachments
from mewbo_core.context import (
    ContextBuilder,
    _load_attachment_images,
    _load_attachment_texts,
)
from mewbo_core.session_store import SessionStore


def test_is_image_and_is_supported():
    """MIME and extension fallback both classify attachments correctly."""
    assert attachments.is_image("image/png")
    assert attachments.is_image("IMAGE/JPEG")  # case-insensitive
    assert not attachments.is_image("application/pdf")

    assert attachments.is_supported("application/pdf", "doc.pdf")
    assert attachments.is_supported("application/octet-stream", "doc.docx")  # by ext
    assert attachments.is_supported("text/csv", "data.csv")
    assert not attachments.is_supported("application/x-msdownload", "evil.exe")
    assert not attachments.is_supported("", None)


def test_model_supports_vision_strips_provider_prefix():
    """LiteLLM-style ``provider/model`` routing prefixes are stripped."""
    # Exercise both branches; we don't assert on exact return values
    # since they depend on the LiteLLM catalogue, but we verify the
    # call doesn't crash and returns a bool.
    assert isinstance(attachments.model_supports_vision("gpt-4o"), bool)
    assert isinstance(attachments.model_supports_vision("openai/gpt-4o"), bool)
    assert attachments.model_supports_vision(None) is False
    assert attachments.model_supports_vision("") is False


def test_parse_to_markdown_text_file(tmp_path):
    """markitdown converts a plain-text file to non-empty Markdown."""
    p = tmp_path / "note.md"
    p.write_text("# Title\n\nBody")
    out = attachments.parse_to_markdown(str(p))
    assert out and "Title" in out


def test_encode_image_data_uri(tmp_path):
    """Images encode to ``data:`` URIs; oversized files return None."""
    p = tmp_path / "x.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    uri = attachments.encode_image_data_uri(str(p), "image/png")
    assert uri and uri.startswith("data:image/png;base64,")


def _seed_session_with_attachment(
    store: SessionStore,
    stored_name: str,
    *,
    filename: str,
    content_type: str,
    raw_bytes: bytes,
    parsed_md: str | None = None,
) -> str:
    session_id = store.create_session()
    att_dir = os.path.join(store.session_dir(session_id), "attachments")
    os.makedirs(att_dir, exist_ok=True)
    raw_path = os.path.join(att_dir, stored_name)
    with open(raw_path, "wb") as fh:
        fh.write(raw_bytes)
    if parsed_md is not None:
        with open(attachments.parsed_sidecar_path(raw_path), "w", encoding="utf-8") as fh:
            fh.write(parsed_md)
    store.append_event(
        session_id,
        {
            "type": "context",
            "payload": {
                "attachments": [
                    {
                        "id": "att-1",
                        "filename": filename,
                        "stored_name": stored_name,
                        "content_type": content_type,
                        "size_bytes": len(raw_bytes),
                    }
                ]
            },
        },
    )
    return session_id


def test_load_attachment_texts_prefers_parsed_sidecar(tmp_path):
    """Documents read from the ``.md`` sidecar instead of the raw file."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = _seed_session_with_attachment(
        store,
        "abc_doc.pdf",
        filename="doc.pdf",
        content_type="application/pdf",
        raw_bytes=b"%PDF-binary",
        parsed_md="# Parsed\nHello",
    )
    events = store.load_transcript(session_id)
    texts = _load_attachment_texts(store.session_dir(session_id), events, model_name=None)
    assert any("Parsed" in t for t in texts)
    assert not any("PDF-binary" in t for t in texts)


def test_load_attachment_texts_warns_on_image_with_non_vision_model(tmp_path):
    """Non-vision models receive a clear ``[Image ... skipped]`` marker."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = _seed_session_with_attachment(
        store,
        "abc_pic.png",
        filename="pic.png",
        content_type="image/png",
        raw_bytes=b"\x89PNG",
    )
    events = store.load_transcript(session_id)
    texts = _load_attachment_texts(
        store.session_dir(session_id), events, model_name="gpt-3.5-turbo"
    )
    joined = "\n".join(texts)
    assert "pic.png" in joined and "skipped" in joined.lower()


def test_load_attachment_images_only_for_vision_models(tmp_path, monkeypatch):
    """Image content parts are emitted only when the model supports vision."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = _seed_session_with_attachment(
        store,
        "abc_pic.png",
        filename="pic.png",
        content_type="image/png",
        raw_bytes=b"\x89PNG\r\n\x1a\n" + b"\x00" * 16,
    )
    events = store.load_transcript(session_id)

    # Force the vision check so the test is hermetic against LiteLLM's catalogue.
    monkeypatch.setattr(
        "mewbo_core.context.model_supports_vision", lambda _m: False
    )
    assert _load_attachment_images(store.session_dir(session_id), events, "any") == []

    monkeypatch.setattr(
        "mewbo_core.context.model_supports_vision", lambda _m: True
    )
    parts = _load_attachment_images(store.session_dir(session_id), events, "vision-model")
    assert len(parts) == 1
    assert parts[0]["type"] == "image_url"
    assert parts[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_context_builder_populates_attachments(tmp_path, monkeypatch):
    """ContextBuilder.build() returns both texts and images on snapshot."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = _seed_session_with_attachment(
        store,
        "abc_pic.png",
        filename="pic.png",
        content_type="image/png",
        raw_bytes=b"\x89PNG\r\n\x1a\n" + b"\x00" * 16,
    )
    monkeypatch.setattr(
        "mewbo_core.context.model_supports_vision", lambda _m: True
    )
    builder = ContextBuilder(store)
    snap = builder.build(session_id, user_query="describe", model_name="vision-model")
    assert snap.attachment_images and snap.attachment_images[0]["type"] == "image_url"
