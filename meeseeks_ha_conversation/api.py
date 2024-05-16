""" Meeseeks API Client. """
from __future__ import annotations

import aiohttp
import async_timeout
import json

# User-defined imports
from .exceptions import (
    ApiClientError,
    ApiCommError,
    ApiJsonError,
    ApiTimeoutError
)
from .const import LOGGER


class MeeseeksApiClient:
    """Meeseeks API Client."""

    def __init__(
        self,
        base_url: str,
        timeout: int,
        session: aiohttp.ClientSession,
    ) -> None:
        """Sample API Client."""
        self._base_url = base_url.rstrip("/")
        self._api_key = 'msk-strong-password'
        self.timeout = timeout
        self._session = session

    async def async_get_heartbeat(self) -> bool:
        """Get heartbeat from the API."""
        # TODO: Implement a heartbeat check
        return True

    async def async_get_models(self) -> any:
        """Get models from the API."""
        # TODO: This is monkey-patched for now
        response_data = {
            "models": [
                {
                    "name": "meeseeks",
                    "modified_at": "2023-11-01T00:00:00.000000000-04:00",
                    "size": 0,
                    "digest": None
                }
            ]
        }
        return json.dumps(response_data)

    async def async_generate(self, data: dict | None = None,) -> any:
        """Generate a completion from the API."""
        url_query = f"{self._base_url}/api/query"
        data_custom = {
            'query': str(data["prompt"]).strip(),
        }
        # Pass headers as None to use the default headers
        return await self._meeseeks_api_wrapper(
            method="post",
            url=url_query,
            data=data_custom,
            headers=None,
        )

    async def _meeseeks_api_wrapper(
        self,
        method: str,
        url: str,
        data: dict | None = None,
        headers: dict | None = None,
        decode_json: bool = True,
    ) -> any:
        """Get information from the API."""
        if headers is None:
            headers = {
                'accept': 'application/json',
                'X-API-KEY': self._api_key,
                'Content-Type': 'application/json',
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
                response_data = await response.json()
                if response.status == 404:
                    raise ApiJsonError(response_data["error"])
                LOGGER.debug(f"Response data: {response_data}")
                response_data["response"] = response_data["task_result"]
                response_data["context"] = response_data["task_result"]
                return response_data
            else:
                LOGGER.debug("Fallback to text response")
                return await response.text()
