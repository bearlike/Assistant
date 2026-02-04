"""Meeseeks API client."""

from __future__ import annotations

import json
from typing import Any, TypedDict

import aiohttp
import async_timeout

from .const import LOGGER

# User-defined imports
from .exceptions import ApiJsonError


class ModelsResponse(TypedDict):
    """Schema for the models list endpoint response."""

    models: list[dict[str, Any]]


class MeeseeksQueryResponse(TypedDict):
    """Schema for the main query response."""

    task_result: str
    response: str
    context: str
    session_id: str | None


class MeeseeksApiClient:
    """Meeseeks API Client."""

    def __init__(
        self,
        base_url: str,
        timeout: int,
        session: aiohttp.ClientSession,
    ) -> None:
        """Initialize the API client.

        Args:
            base_url: Base URL for the Meeseeks API.
            timeout: Request timeout in seconds.
            session: Shared aiohttp client session.
        """
        self._base_url = base_url.rstrip("/")
        self._api_key = "msk-strong-password"
        self.timeout = timeout
        self._session = session

    async def async_get_heartbeat(self) -> bool:
        """Get heartbeat from the API.

        Returns:
            True when the service is considered healthy.
        """
        # TODO: Implement a heartbeat check
        return True

    async def async_get_models(self) -> str:
        """Get models from the API.

        Returns:
            JSON-serialized model list.
        """
        # TODO: This is monkey-patched for now
        response_data: ModelsResponse = {
            "models": [
                {
                    "name": "meeseeks",
                    "modified_at": "2023-11-01T00:00:00.000000000-04:00",
                    "size": 0,
                    "digest": None,
                }
            ]
        }
        return json.dumps(response_data)

    async def async_generate(self, data: dict[str, Any] | None = None) -> MeeseeksQueryResponse:
        """Generate a completion from the API.

        Args:
            data: Request payload including prompt and optional session ID.

        Returns:
            Parsed query response payload.

        Raises:
            ValueError: If prompt data is missing.
            ApiJsonError: If the API returns unexpected data.
        """
        if not data or "prompt" not in data:
            raise ValueError("Missing prompt in request data.")
        url_query = f"{self._base_url}/api/query"
        data_custom = {
            "query": str(data["prompt"]).strip(),
        }
        session_id = data.get("session_id") if isinstance(data, dict) else None
        if session_id:
            data_custom["session_id"] = session_id
        # Pass headers as None to use the default headers
        result = await self._meeseeks_api_wrapper(
            method="post",
            url=url_query,
            data=data_custom,
            headers=None,
        )
        if isinstance(result, str):
            raise ApiJsonError("Unexpected text response from Meeseeks API.")
        return result

    async def _meeseeks_api_wrapper(
        self,
        method: str,
        url: str,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        decode_json: bool = True,
    ) -> MeeseeksQueryResponse | str:
        """Perform an HTTP request to the Meeseeks API.

        Args:
            method: HTTP method to use.
            url: Fully qualified request URL.
            data: Optional JSON payload to send.
            headers: Optional HTTP headers override.
            decode_json: Whether to parse JSON responses.

        Returns:
            Parsed response payload or raw text depending on decode_json.

        Raises:
            ApiJsonError: If the API returns an error payload.
            aiohttp.ClientResponseError: For non-2xx responses.
        """
        if headers is None:
            headers = {
                "accept": "application/json",
                "X-API-KEY": self._api_key,
                "Content-Type": "application/json",
            }
        async with async_timeout.timeout(self.timeout):
            response = await self._session.request(
                method=method,
                url=url,
                headers=headers,
                json=data,
            )
            response.raise_for_status()

            if decode_json:
                raw_data: dict[str, Any] = await response.json()
                if response.status == 404:
                    raise ApiJsonError(raw_data.get("error", "Unknown error"))
                task_result = str(raw_data.get("task_result", ""))
                response_data: MeeseeksQueryResponse = {
                    "task_result": task_result,
                    "response": str(raw_data.get("response", task_result)),
                    "context": str(raw_data.get("context", task_result)),
                    "session_id": raw_data.get("session_id"),
                }
                LOGGER.debug("Response data: %s", response_data)
                return response_data
            else:
                LOGGER.debug("Fallback to text response")
                return await response.text()
