"""Custom integration to integrate meeseeks_conversation with Home Assistant.

For more details about this integration, please refer to
https://github.com/bearlike/personal-Assistant/
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypedDict

from .const import DOMAIN, LOGGER

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


class MeeseeksMessage(TypedDict, total=False):
    """Message structure used to call the Meeseeks API."""

    system: str
    context: str | None
    session_id: str | None
    prompt: str


class MeeseeksResponse(TypedDict):
    """Response payload returned from the Meeseeks API."""

    task_result: str
    response: str
    context: str
    session_id: str | None


try:
    from homeassistant.components import conversation
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.const import MATCH_ALL
    from homeassistant.core import HomeAssistant
    from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
    from homeassistant.helpers import intent, template
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from homeassistant.util import ulid

    from .api import MeeseeksApiClient
    from .const import CONF_BASE_URL, CONF_TIMEOUT, DEFAULT_TIMEOUT
    from .coordinator import MeeseeksDataUpdateCoordinator
    from .exceptions import ApiClientError

    _HOMEASSISTANT_AVAILABLE = True
except ModuleNotFoundError as exc:  # pragma: no cover - handled by runtime checks
    conversation = None
    MATCH_ALL = "*"
    _HOMEASSISTANT_AVAILABLE = False
    _HOMEASSISTANT_IMPORT_ERROR = exc


def _missing_homeassistant_error() -> RuntimeError:
    error = RuntimeError(
        "Home Assistant is not installed. Install meeseeks-ha-conversation[homeassistant]."
    )
    error.__cause__ = _HOMEASSISTANT_IMPORT_ERROR
    return error


if _HOMEASSISTANT_AVAILABLE:

    class _BaseConversationAgent(conversation.AbstractConversationAgent):
        """Base agent when Home Assistant is available."""

else:

    class _BaseConversationAgent:  # type: ignore[no-redef]
        """Base agent placeholder when Home Assistant is unavailable."""


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Meeseeks conversation using UI.

    Args:
        hass: Home Assistant core instance.
        entry: Configuration entry to initialize.

    Returns:
        True when setup succeeds.

    Raises:
        ConfigEntryNotReady: If the Meeseeks API is unreachable.
    """
    if not _HOMEASSISTANT_AVAILABLE:
        raise _missing_homeassistant_error()

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

    conversation.async_set_agent(hass, entry, MeeseeksAgent(hass, entry, client))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Meeseeks conversation.

    Args:
        hass: Home Assistant core instance.
        entry: Configuration entry to unload.

    Returns:
        True when unload succeeds.
    """
    if not _HOMEASSISTANT_AVAILABLE:
        raise _missing_homeassistant_error()

    conversation.async_unset_agent(hass, entry)
    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload Meeseeks conversation.

    Args:
        hass: Home Assistant core instance.
        entry: Configuration entry to reload.
    """
    if not _HOMEASSISTANT_AVAILABLE:
        raise _missing_homeassistant_error()

    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)


class MeeseeksAgent(_BaseConversationAgent):
    """Meeseeks conversation agent."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: MeeseeksApiClient,
    ) -> None:
        """Initialize the agent."""
        if not _HOMEASSISTANT_AVAILABLE:
            raise _missing_homeassistant_error()

        self.hass = hass
        self.entry = entry
        self.client = client
        self.history: dict[str, MeeseeksMessage] = {}

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Return a list of supported languages.

        Returns:
            Language identifiers or "*" for all languages.
        """
        if not _HOMEASSISTANT_AVAILABLE:
            raise _missing_homeassistant_error()

        return MATCH_ALL

    async def async_process(
        self, user_input: conversation.ConversationInput
    ) -> conversation.ConversationResult:
        """Process a user utterance into a conversation result.

        Args:
            user_input: Incoming conversation input.

        Returns:
            ConversationResult populated with a response.
        """
        if not _HOMEASSISTANT_AVAILABLE:
            raise _missing_homeassistant_error()

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
                "session_id": None,
            }

        messages["prompt"] = user_input.text

        try:
            response = await self.query(messages)
        except HomeAssistantError as err:
            LOGGER.error("Something went wrong: %s", err)
            intent_response = intent.IntentResponse(language=user_input.language)
            intent_response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                "Something went wrong, please check the logs for more information.",
            )
            return conversation.ConversationResult(
                response=intent_response, conversation_id=conversation_id
            )

        messages["context"] = response["context"]
        messages["session_id"] = response.get("session_id")
        self.history[conversation_id] = messages

        intent_response = intent.IntentResponse(language=user_input.language)
        intent_response.async_set_speech(response["response"])
        return conversation.ConversationResult(
            response=intent_response, conversation_id=conversation_id
        )

    def _async_generate_prompt(
        self, raw_prompt: str, exposed_entities: list[dict[str, Any]]
    ) -> str:
        """Generate a prompt for the user.

        Args:
            raw_prompt: Template string for prompt rendering.
            exposed_entities: Entities exposed to the conversation agent.

        Returns:
            Rendered prompt string.
        """
        if not _HOMEASSISTANT_AVAILABLE:
            raise _missing_homeassistant_error()

        return template.Template(raw_prompt, self.hass).async_render(
            {
                "ha_name": self.hass.config.location_name,
                "exposed_entities": exposed_entities,
            }
        )
