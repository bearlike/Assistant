"""Custom integration to integrate meeseeks_conversation with Home Assistant.

For more details about this integration, please refer to
https://github.com/bearlike/personal-Assistant/
"""
from __future__ import annotations

from typing import Literal

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import intent, template
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import ulid

from .api import MeeseeksApiClient
from .const import (
    DOMAIN, LOGGER,
    CONF_BASE_URL,
    CONF_TIMEOUT,
    DEFAULT_TIMEOUT,
)
# User-defined imports
from .coordinator import MeeseeksDataUpdateCoordinator
from .exceptions import (
    ApiClientError,
    ApiCommError,
    ApiJsonError,
    ApiTimeoutError
)
# from .helpers import get_exposed_entities

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Meeseeks conversation using UI."""
    # https://developers.home-assistant.io/docs/config_entries_index/#setting-up-an-entry
    hass.data.setdefault(DOMAIN, {})
    client = MeeseeksApiClient(
        base_url=entry.data[CONF_BASE_URL],
        timeout=entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
        session=async_get_clientsession(hass),
    )

    hass.data[DOMAIN][entry.entry_id] = coordinator = MeeseeksDataUpdateCoordinator(
        hass,
        client,
    )
    # https://developers.home-assistant.io/docs/integration_fetching_data#coordinated-single-api-poll-for-data-for-all-entities
    await coordinator.async_config_entry_first_refresh()

    try:
        # TODO: Heartbeat check is not implemented but it is still wrapped.
        response = await client.async_get_heartbeat()
        if not response:
            raise ApiClientError("Invalid Meeseeks server")
    except ApiClientError as err:
        raise ConfigEntryNotReady(err) from err

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    conversation.async_set_agent(
        hass, entry, MeeseeksAgent(hass, entry, client))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Meeseeks conversation."""
    conversation.async_unset_agent(hass, entry)
    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload Meeseeks conversation."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)


class MeeseeksAgent(conversation.AbstractConversationAgent):
    """Meeseeks conversation agent."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: MeeseeksApiClient) -> None:
        """Initialize the agent."""
        self.hass = hass
        self.entry = entry
        self.client = client
        self.history: dict[str, dict] = {}

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Return a list of supported languages."""
        return MATCH_ALL

    async def async_process(
        self, user_input: conversation.ConversationInput
    ) -> conversation.ConversationResult:
        """Process a sentence."""
        # * If needeed in the future, uncomment the following lines
        # raw_system_prompt = self.entry.options.get(
        #     CONF_PROMPT_SYSTEM, DEFAULT_PROMPT_SYSTEM)
        # exposed_entities = get_exposed_entities(self.hass)
        # ! Currently, history is not used but still implemented for future use
        if user_input.conversation_id in self.history:
            conversation_id = user_input.conversation_id
            messages = self.history[conversation_id]
        else:
            conversation_id = ulid.ulid()
            system_prompt = ""
            messages = {
                "system": system_prompt,
                "context": None,
            }

        messages["prompt"] = user_input.text

        try:
            response = await self.query(messages)
        except HomeAssistantError as err:
            LOGGER.error("Something went wrong: %s", err)
            intent_response = intent.IntentResponse(
                language=user_input.language)
            intent_response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                "Something went wrong, please check the logs for more information.",
            )
            return conversation.ConversationResult(
                response=intent_response, conversation_id=conversation_id
            )

        messages["context"] = response["context"]
        self.history[conversation_id] = messages

        intent_response = intent.IntentResponse(language=user_input.language)
        intent_response.async_set_speech(response["response"])
        return conversation.ConversationResult(
            response=intent_response, conversation_id=conversation_id
        )

    def _async_generate_prompt(self, raw_prompt: str, exposed_entities) -> str:
        """Generate a prompt for the user."""
        return template.Template(raw_prompt, self.hass).async_render(
            {
                "ha_name": self.hass.config.location_name,
                "exposed_entities": exposed_entities,
            },
            parse_result=False,
        )

    async def query(
        self,
        messages
    ):
        """Process a sentence."""
        # model = self.entry.options.get(CONF_MODEL, DEFAULT_MODEL)
        # LOGGER.debug("Prompt for %s: %s", model, messages["prompt"])

        # TODO: $context, and $system are not used but still implemented for
        #        future use
        # * Generator
        result = await self.client.async_generate({
            "context": messages["context"],
            "system": messages["system"],
            "prompt": messages["prompt"],
        })
        response: str = result["task_result"]
        LOGGER.debug("Response %s", response)
        return result
