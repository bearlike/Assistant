"""DataUpdateCoordinator for meeseeks_conversation."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import MeeseeksApiClient
from .const import DOMAIN, LOGGER
from .exceptions import ApiClientError


# https://developers.home-assistant.io/docs/integration_fetching_data#coordinated-single-api-poll-for-data-for-all-entities
class MeeseeksDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        client: MeeseeksApiClient,
    ) -> None:
        """Initialize the coordinator.

        Args:
            hass: Home Assistant core instance.
            client: API client for Meeseeks.
        """
        self.client = client
        super().__init__(
            hass=hass,
            logger=LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=5),
        )

    async def _async_update_data(self) -> bool:
        """Update data via library.

        Returns:
            True when the heartbeat check succeeds.

        Raises:
            UpdateFailed: If the API heartbeat fails.
        """
        try:
            return await self.client.async_get_heartbeat()
        except ApiClientError as exception:
            raise UpdateFailed(exception) from exception
