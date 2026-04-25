"""Nextcloud Talk channel adapter.

Implements the :class:`ChannelAdapter` protocol using the Nextcloud Talk
Bot API v1 (bots-v1 capability, Talk 17.1+). Supports threading on
Talk 22+ via ``threadId``/``replyTo`` parameters.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import urllib.request
from typing import Any

from truss_core.common import get_logger

from truss_api.channels.base import InboundMessage

logger = get_logger(name="channels.nextcloud-talk")

# Rich-Object placeholder pattern: ``{placeholder-name}``
_RICH_OBJECT_RE = re.compile(r"\{[a-zA-Z0-9_-]+\}")


class NextcloudTalkAdapter:
    """Adapter bridging Nextcloud Talk webhooks to Truss sessions."""

    platform: str = "nextcloud-talk"

    def __init__(
        self,
        *,
        bot_secret: str,
        nextcloud_url: str,
        allowed_backends: list[str] | None = None,
        host_header: str | None = None,
        trigger_keyword: str = "@Truss",
    ) -> None:
        """Initialise with the shared HMAC secret and Nextcloud base URL.

        Args:
            bot_secret: Shared secret from ``occ talk:bot:install``.
            nextcloud_url: Base URL of the Nextcloud instance
                (e.g. ``https://cloud.example.com`` or
                ``http://localhost:8180`` for internal routing).
            allowed_backends: Optional origin allowlist for the
                ``X-Nextcloud-Talk-Backend`` header.  When empty or
                *None*, all backends are accepted.
            host_header: Optional ``Host`` header override for outbound
                requests (useful when *nextcloud_url* is an internal
                address that differs from the public hostname).
            trigger_keyword: Literal keyword (e.g. ``@Truss``)
                that must appear in a message to trigger an LLM run.
                Empty string means respond to every message.
        """
        self._secret = bot_secret.encode()
        self._base_url = nextcloud_url.rstrip("/")
        self._host_header = host_header
        self.trigger_keyword = trigger_keyword
        self._allowed: set[str] = set()
        for url in allowed_backends or []:
            self._allowed.add(_normalize_origin(url))

    @property
    def system_context(self) -> str:
        """Brief system prompt note for LLM awareness."""
        from truss_api.channels.routes import _COMMANDS

        cmds = ", ".join(f"/{c}" for c in _COMMANDS)
        return (
            "This conversation is via Nextcloud Talk. "
            f"The user triggers you with {self.trigger_keyword}. "
            f"Quick commands: {cmds}. "
            "Keep responses concise and chat-friendly."
        )

    # ------------------------------------------------------------------
    # ChannelAdapter protocol
    # ------------------------------------------------------------------

    def verify_request(self, headers: dict[str, str], body: bytes) -> bool:
        """Verify HMAC-SHA256 signature from Nextcloud Talk.

        Headers used:
        - ``X-Nextcloud-Talk-Signature``: hex-encoded HMAC digest.
        - ``X-Nextcloud-Talk-Random``: random string mixed into the
          digest.
        - ``X-Nextcloud-Talk-Backend``: origin of the Nextcloud
          instance.
        """
        sig = _get_header(headers, "X-Nextcloud-Talk-Signature")
        random = _get_header(headers, "X-Nextcloud-Talk-Random")
        backend = _get_header(headers, "X-Nextcloud-Talk-Backend")
        if not sig or not random:
            return False

        # Backend origin allowlist check
        if self._allowed:
            if not backend or _normalize_origin(backend) not in self._allowed:
                return False

        expected = hmac.new(
            self._secret,
            (random + body.decode("utf-8", errors="replace")).encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, sig.lower())

    def parse_inbound(self, headers: dict[str, str], body: bytes) -> InboundMessage | None:
        """Parse an ActivityStreams 2.0 webhook payload.

        Only ``type: "Create"`` events produce an :class:`InboundMessage`.
        All other types (Update, Delete, Join, Leave) return ``None``.
        """
        try:
            payload: dict[str, Any] = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Failed to decode webhook payload")
            return None

        if payload.get("type") != "Create":
            return None

        actor = payload.get("actor", {})
        obj = payload.get("object", {})
        target = payload.get("target", {})

        text = _extract_message_text(obj.get("content", ""))
        attachments = _extract_attachments(obj.get("content", ""))

        thread_id: str | None = None
        raw_thread = obj.get("threadId")
        if raw_thread is not None:
            thread_id = str(raw_thread)

        return InboundMessage(
            platform=self.platform,
            channel_id=str(target.get("id", "")),
            thread_id=thread_id,
            message_id=str(obj.get("id", "")),
            sender_id=str(actor.get("id", "")),
            sender_name=str(actor.get("name", "")),
            text=text,
            timestamp=str(obj.get("published", "")),
            room_name=str(target.get("name", "")),
            attachments=attachments,
            raw=payload,
        )

    def send_response(
        self,
        channel_id: str,
        text: str,
        thread_id: str | None = None,
        reply_to: str | None = None,
    ) -> str | None:
        """Send a message to a Nextcloud Talk conversation.

        Uses the OCS Bot API endpoint::

            POST /ocs/v2.php/apps/spreed/api/v1/bot/{token}/message

        The request body is HMAC-signed with the shared secret.
        """
        url = f"{self._base_url}/ocs/v2.php/apps/spreed/api/v1/bot/{channel_id}/message"

        body_dict: dict[str, Any] = {"message": text[:32000]}
        if reply_to is not None:
            try:
                body_dict["replyTo"] = int(reply_to)
            except (ValueError, TypeError):
                pass
        elif thread_id is not None:
            try:
                body_dict["threadId"] = int(thread_id)
            except (ValueError, TypeError):
                pass

        body_bytes = json.dumps(body_dict).encode()
        random_hex = secrets.token_hex(32)
        # NC Talk ChecksumVerificationService signs the message TEXT,
        # not the full JSON body.
        message_text = text[:32000]
        sig = hmac.new(
            self._secret,
            (random_hex + message_text).encode(),
            hashlib.sha256,
        ).hexdigest()

        headers_dict: dict[str, str] = {
            "Content-Type": "application/json",
            "OCS-APIRequest": "true",
            "X-Nextcloud-Talk-Bot-Random": random_hex,
            "X-Nextcloud-Talk-Bot-Signature": sig,
        }
        if self._host_header:
            headers_dict["Host"] = self._host_header
        req = urllib.request.Request(
            url,
            data=body_bytes,
            method="POST",
            headers=headers_dict,
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                if resp.status == 201:
                    logger.info("Sent response to NC Talk room %s", channel_id)
                    # OCS API returns XML by default; don't bother
                    # parsing — 201 confirms delivery.
                    return "sent"
                logger.warning(
                    "Unexpected status %d from NC Talk room %s",
                    resp.status,
                    channel_id,
                )
        except Exception:
            logger.warning(
                "Failed to send response to NC Talk room %s",
                channel_id,
                exc_info=True,
            )
        return None


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _normalize_origin(url: str) -> str:
    """Extract and lower-case the origin from a URL.

    Standard ports (80 for http, 443 for https) are stripped so that
    ``https://host`` and ``https://host:443`` compare as equal.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    port = parsed.port
    # Strip standard ports to normalise origins.
    if port == 443 and parsed.scheme == "https":
        port = None
    elif port == 80 and parsed.scheme == "http":
        port = None
    port_suffix = f":{port}" if port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port_suffix}".lower()


def _get_header(headers: dict[str, str], name: str) -> str | None:
    """Case-insensitive header lookup."""
    lower = name.lower()
    for key, value in headers.items():
        if key.lower() == lower:
            return value
    return None


def _extract_message_text(content: str) -> str:
    """Extract the human-readable message text from NC Talk content.

    NC Talk sends ``object.content`` as a JSON string like::

        {"message": "Hello {mention-user1}", "parameters": {...}}

    We parse the ``message`` field and strip Rich Object placeholders.
    """
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict) and "message" in parsed:
            raw = parsed["message"]
            return _RICH_OBJECT_RE.sub("", raw).strip()
    except (json.JSONDecodeError, TypeError):
        pass
    return content.strip()


def _extract_attachments(content: str) -> list[dict[str, str]]:
    """Extract file attachment metadata from Rich Object parameters."""
    try:
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            return []
        params = parsed.get("parameters", {})
        if not isinstance(params, dict):
            return []
        attachments: list[dict[str, str]] = []
        for _key, param in params.items():
            if isinstance(param, dict) and param.get("type") == "file":
                attachments.append(
                    {
                        "id": str(param.get("id", "")),
                        "name": str(param.get("name", "")),
                        "mimetype": str(param.get("mimetype", "")),
                        "size": str(param.get("size", "")),
                        "link": str(param.get("link", "")),
                    }
                )
        return attachments
    except (json.JSONDecodeError, TypeError):
        return []
