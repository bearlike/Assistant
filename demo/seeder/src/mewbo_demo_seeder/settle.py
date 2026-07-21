#!/usr/bin/env python3
"""Post-seed notification settling — one atomic class, REST-only.

The api materialises a "'<title>' completed" notification LAZILY, on the first
``GET /api/sessions/<id>/events`` after a completion event exists
(``NotificationService.emit_completion``), and dedupes forever on
``(session_id, completion_ts)`` — checking dismissed records too. Seeded
sessions therefore toast exactly once per api container: nondeterministically,
in whichever capture first touches them. Settling makes that deterministic:
touch every seeded session's events endpoint (materialise now), then dismiss
everything — after which the dedupe guarantees the notification can never
come back, however many times the bundle is re-seeded.

REST is the only possible seam here: ``NotificationStore`` is a JSON file
inside the api container's ``MEWBO_HOME``, unreachable from this process's
filesystem — there is no store contract to drive across that boundary.
"""

from __future__ import annotations

import json
import urllib.request


class NotificationSettler:
    """Materialise-then-dismiss the api's notifications for seeded sessions.

    Collaborators are injected as fields — the api base URL, the key, and the
    seeded session ids — so a test can point it at a stub HTTP server; the
    class owns no global state and creates no client at import time.
    """

    def __init__(self, *, base_url: str, api_key: str, session_ids: list[str]) -> None:
        """Store the injected api endpoint, credential, and seeded ids."""
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._session_ids = session_ids

    def settle(self) -> int:
        """Materialise + dismiss all notifications; return how many were dismissed."""
        for session_id in self._session_ids:
            self._request("GET", f"/api/sessions/{session_id}/events")
        listing = self._request("GET", "/api/notifications")
        ids = [
            str(item["id"])
            for item in listing.get("notifications", [])
            if not item.get("dismissed")
        ]
        if not ids:
            return 0
        self._request("POST", "/api/notifications/dismiss", {"ids": ids})
        return len(ids)

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        """One authed JSON round-trip against the api."""
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=data,
            method=method,
            headers={
                "X-Api-Key": self._api_key,
                **({"Content-Type": "application/json"} if data else {}),
            },
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode() or "{}")
