"""Adds config flow for Meeseeks."""
from __future__ import annotations

import types
from typing import Any
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    TemplateSelector,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    SelectOptionDict
)
# User-defined imports
from .api import MeeseeksApiClient
from .const import (
    DOMAIN, LOGGER,
    MENU_OPTIONS,

    CONF_BASE_URL,
    CONF_API_KEY,
    CONF_TIMEOUT,
    CONF_MODEL,
    CONF_CTX_SIZE,
    CONF_MAX_TOKENS,
    CONF_MIROSTAT_MODE,
    CONF_MIROSTAT_ETA,
    CONF_MIROSTAT_TAU,
    CONF_TEMPERATURE,
    CONF_REPEAT_PENALTY,
    CONF_TOP_K,
    CONF_TOP_P,
    CONF_PROMPT_SYSTEM,

    DEFAULT_BASE_URL,
    DEFAULT_API_KEY,
    DEFAULT_TIMEOUT,
    DEFAULT_MODEL,
    DEFAULT_CTX_SIZE,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MIROSTAT_MODE,
    DEFAULT_MIROSTAT_ETA,
    DEFAULT_MIROSTAT_TAU,
    DEFAULT_TEMPERATURE,
    DEFAULT_REPEAT_PENALTY,
    DEFAULT_TOP_K,
    DEFAULT_TOP_P,
    DEFAULT_PROMPT_SYSTEM
)
from .exceptions import (
    ApiClientError,
    ApiCommError,
    ApiTimeoutError
)


STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_BASE_URL, default=DEFAULT_BASE_URL): str,
        vol.Required(CONF_API_KEY, default=DEFAULT_API_KEY): str,
        vol.Required(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): int,
    }
)

DEFAULT_OPTIONS = types.MappingProxyType(
    {
        CONF_BASE_URL: DEFAULT_BASE_URL,
        CONF_API_KEY: DEFAULT_API_KEY,
        CONF_TIMEOUT: DEFAULT_TIMEOUT,
        CONF_MODEL: DEFAULT_MODEL,
        CONF_PROMPT_SYSTEM: DEFAULT_PROMPT_SYSTEM
    }
)


class MeeseeksConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Meeseeks Conversation. Handles UI wizard."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        if user_input is None:
            return self.async_show_form(
                step_id="user", data_schema=STEP_USER_DATA_SCHEMA
            )

        # Search for duplicates with the same CONF_BASE_URL value.
        for existing_entry in self._async_current_entries(include_ignore=False):
            if existing_entry.data.get(CONF_BASE_URL) == user_input[CONF_BASE_URL]:
                return self.async_abort(reason="already_configured")

        errors = {}
        try:
            self.client = MeeseeksApiClient(
                base_url=cv.url_no_path(user_input[CONF_BASE_URL]),
                timeout=user_input[CONF_TIMEOUT],
                session=async_create_clientsession(self.hass),
            )
            response = await self.client.async_get_heartbeat()
            if not response:
                raise vol.Invalid("Invalid Meeseeks server")
        # except vol.Invalid:
        #     errors["base"] = "invalid_url"
        # except ApiTimeoutError:
        #     errors["base"] = "timeout_connect"
        # except ApiCommError:
        #     errors["base"] = "cannot_connect"
        # except ApiClientError as exception:
        #     LOGGER.exception("Unexpected exception: %s", exception)
        #     errors["base"] = "unknown"
        except Exception as exception:
            LOGGER.exception("Unexpected exception: %s", exception)
            errors["base"] = "unknown"
        else:
            return self.async_create_entry(
                title=f"Meeseeks - {user_input[CONF_BASE_URL]}",
                data={
                    CONF_BASE_URL: user_input[CONF_BASE_URL]
                },
                options={
                    CONF_TIMEOUT: user_input[CONF_TIMEOUT]
                }
            )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return MeeseeksOptionsFlow(config_entry)


class MeeseeksOptionsFlow(config_entries.OptionsFlow):
    """Meeseeks config flow options handler."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry
        self.options = dict(config_entry.options)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        return self.async_show_menu(
            step_id="init",
            menu_options=MENU_OPTIONS
        )

    async def async_step_all_set(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        return self.async_show_menu(
            step_id="init",
            menu_options=MENU_OPTIONS
        )

    async def async_step_general_config(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        return self.async_show_menu(
            step_id="init",
            menu_options=MENU_OPTIONS
        )

    async def async_step_prompt_system(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        return self.async_show_menu(
            step_id="init",
            menu_options=MENU_OPTIONS
        )

    async def async_step_model_config(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        return self.async_show_menu(
            step_id="init",
            menu_options=MENU_OPTIONS
        )
