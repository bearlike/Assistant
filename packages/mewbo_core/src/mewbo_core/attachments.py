#!/usr/bin/env python3
"""Attachment handling: type gating, vision detection, document parsing.

Single source of truth for "what file types do we accept and how do we
turn them into LLM-friendly content?" — referenced by both the upload
endpoint (validation) and the context loader (rendering).

Design:
- Documents (PDF, DOCX, XLSX, PPTX, CSV, TXT, JSON, etc.) are parsed at
  upload time via `markitdown` into Markdown stored alongside the raw file.
  The context loader then ships the cached `.md` — never re-parses.
- Images are NOT parsed; they ride through as base64 data URIs into the
  LLM's `image_url` content parts on vision-capable models, and are
  rejected (or warned) on non-vision models.
- Vision capability is detected via `litellm.supports_vision`. Unknown
  models default to ``False`` (fail closed — better to warn than to
  send 1MB of base64 to a text-only endpoint).
"""

from __future__ import annotations

import base64
import os
from functools import lru_cache

from mewbo_core.common import get_logger

logging = get_logger(name="core.attachments")


# ---------------------------------------------------------------------
# Supported types — single source of truth
# ---------------------------------------------------------------------

# Document MIME types we can convert to Markdown via markitdown.
DOCUMENT_MIME_TYPES: frozenset[str] = frozenset({
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # .xlsx
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",  # .pptx
    "text/csv",
    "text/plain",
    "text/markdown",
    "application/json",
    "application/xml",
    "text/xml",
    "application/x-yaml",
    "text/yaml",
    "text/html",
})

# Image MIME types — sent inline to vision-capable models.
IMAGE_MIME_TYPES: frozenset[str] = frozenset({
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/gif",
    "image/webp",
})

# File-extension fallback — browsers/clients sometimes send octet-stream.
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".csv", ".txt", ".md", ".json", ".xml", ".yaml", ".yml", ".html",
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
})


def is_image(content_type: str) -> bool:
    """Return True if content_type is a supported image type."""
    return (content_type or "").lower() in IMAGE_MIME_TYPES


def is_document(content_type: str) -> bool:
    """Return True if content_type is a supported document/text type."""
    ct = (content_type or "").lower()
    if ct in DOCUMENT_MIME_TYPES:
        return True
    # text/* fallback for niche subtypes (text/markdown variants, etc.)
    return ct.startswith("text/")


def is_supported(content_type: str, filename: str | None = None) -> bool:
    """Return True if we accept this attachment.

    Checks MIME first, then falls back to extension — browsers
    occasionally upload `application/octet-stream` for known types.
    """
    if is_image(content_type) or is_document(content_type):
        return True
    if filename:
        _, ext = os.path.splitext(filename.lower())
        if ext in SUPPORTED_EXTENSIONS:
            return True
    return False


# ---------------------------------------------------------------------
# Vision capability
# ---------------------------------------------------------------------


def _strip_provider_prefix(model_name: str) -> str:
    if "/" in model_name:
        return model_name.split("/", 1)[1]
    return model_name


@lru_cache(maxsize=128)
def model_supports_vision(model_name: str | None) -> bool:
    """Return True if the model accepts image inputs.

    Uses ``litellm.supports_vision`` as the source of truth. Unknown
    models return ``False`` (fail closed). Caches results since the
    LiteLLM model catalogue is static per process.
    """
    if not model_name:
        return False
    try:
        import litellm

        canonical = _strip_provider_prefix(model_name)
        # supports_vision raises for unknown models in some versions;
        # also try the raw name first for proxy aliases.
        for name in (model_name, canonical):
            try:
                if litellm.supports_vision(model=name):
                    return True
            except Exception:  # noqa: BLE001 - litellm raises bare exceptions
                continue
    except Exception:  # noqa: BLE001
        pass
    return False


# ---------------------------------------------------------------------
# Parsing — markitdown wrapper
# ---------------------------------------------------------------------


def parse_to_markdown(path: str) -> str | None:
    """Convert a document at ``path`` to Markdown via markitdown.

    Returns the Markdown text on success, ``None`` if markitdown is not
    installed or parsing failed. Lazy import keeps test/import surface
    light when attachments aren't used.
    """
    try:
        from markitdown import MarkItDown
    except Exception as exc:  # noqa: BLE001
        logging.warning("markitdown not available: %s", exc)
        return None
    try:
        md = MarkItDown(enable_plugins=False)
        result = md.convert(path)
        text = getattr(result, "text_content", None) or getattr(result, "markdown", None)
        if isinstance(text, str) and text.strip():
            return text
    except Exception as exc:  # noqa: BLE001
        logging.warning("markitdown failed for %s: %s", path, exc)
    return None


def parsed_sidecar_path(stored_path: str) -> str:
    """Return the conventional sidecar path for the parsed Markdown."""
    return stored_path + ".md"


# ---------------------------------------------------------------------
# Image inline encoding
# ---------------------------------------------------------------------


# Per-image cap for inline base64 — 4 MB raw → ~5.4 MB base64. Keeps a
# single image well under provider per-message limits.
_MAX_IMAGE_BYTES = 4 * 1024 * 1024


def encode_image_data_uri(path: str, content_type: str) -> str | None:
    """Read an image and return a ``data:...;base64,...`` URI.

    Returns ``None`` if the file is missing or oversized. Callers should
    surface a friendly placeholder instead.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size > _MAX_IMAGE_BYTES:
        return None
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    b64 = base64.b64encode(data).decode("ascii")
    ct = content_type or "image/png"
    return f"data:{ct};base64,{b64}"
