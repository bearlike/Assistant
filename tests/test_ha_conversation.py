"""Tests for the Home Assistant conversation integration."""

import asyncio
import sys
import types
from contextlib import asynccontextmanager


def _install_homeassistant_stubs():
    homeassistant = types.ModuleType("homeassistant")
    sys.modules["homeassistant"] = homeassistant

    components = types.ModuleType("homeassistant.components")
    conversation = types.ModuleType("homeassistant.components.conversation")
    conversation.DOMAIN = "conversation"

    class AbstractConversationAgent:
        pass

    class ConversationInput:
        def __init__(self, conversation_id, text, language="en"):
            self.conversation_id = conversation_id
            self.text = text
            self.language = language

    class ConversationResult:
        def __init__(self, response, conversation_id):
            self.response = response
            self.conversation_id = conversation_id

    def async_set_agent(hass, entry, agent):
        hass._agent = agent

    def async_unset_agent(hass, entry):
        hass._agent = None

    conversation.AbstractConversationAgent = AbstractConversationAgent
    conversation.ConversationInput = ConversationInput
    conversation.ConversationResult = ConversationResult
    conversation.async_set_agent = async_set_agent
    conversation.async_unset_agent = async_unset_agent
    components.conversation = conversation

    homeassistant_components_ha = types.ModuleType(
        "homeassistant.components.homeassistant.exposed_entities"
    )
    homeassistant_components_ha.async_should_expose = lambda hass, domain, entity_id: True

    config_entries = types.ModuleType("homeassistant.config_entries")

    class ConfigEntry:
        def __init__(self, data=None, options=None):
            self.data = data or {}
            self.options = options or {}
            self.entry_id = "entry"

        def add_update_listener(self, callback):
            return callback

        def async_on_unload(self, callback):
            self._unload = callback

    class ConfigFlow:
        def __init_subclass__(cls, **kwargs):
            return super().__init_subclass__()

        def _async_current_entries(self, include_ignore=False):
            return []

        def async_show_form(self, step_id=None, data_schema=None, errors=None):
            return {
                "type": "form",
                "step_id": step_id,
                "data_schema": data_schema,
                "errors": errors or {},
            }

        def async_abort(self, reason=None):
            return {"type": "abort", "reason": reason}

        def async_create_entry(self, title=None, data=None, options=None):
            return {"type": "create_entry", "title": title, "data": data, "options": options}

    class OptionsFlow:
        def async_show_menu(self, step_id=None, menu_options=None):
            return {"type": "menu", "step_id": step_id, "menu_options": menu_options}

    config_entries.ConfigEntry = ConfigEntry
    config_entries.ConfigFlow = ConfigFlow
    config_entries.OptionsFlow = OptionsFlow

    data_entry_flow = types.ModuleType("homeassistant.data_entry_flow")
    data_entry_flow.FlowResult = dict

    const = types.ModuleType("homeassistant.const")
    const.MATCH_ALL = "*"

    core = types.ModuleType("homeassistant.core")

    class HomeAssistant:
        def __init__(self):
            self.data = {}
            self.config = types.SimpleNamespace(location_name="Home")
            self.states = types.SimpleNamespace(async_all=lambda: [])

    core.HomeAssistant = HomeAssistant

    exceptions = types.ModuleType("homeassistant.exceptions")

    class HomeAssistantError(Exception):
        pass

    class ConfigEntryNotReady(HomeAssistantError):
        pass

    exceptions.HomeAssistantError = HomeAssistantError
    exceptions.ConfigEntryNotReady = ConfigEntryNotReady

    helpers = types.ModuleType("homeassistant.helpers")
    intent = types.ModuleType("homeassistant.helpers.intent")

    class IntentResponse:
        def __init__(self, language=None):
            self.language = language
            self.speech = None
            self.error = None

        def async_set_error(self, code, message):
            self.error = (code, message)

        def async_set_speech(self, speech):
            self.speech = speech

    class IntentResponseErrorCode:
        UNKNOWN = "unknown"

    intent.IntentResponse = IntentResponse
    intent.IntentResponseErrorCode = IntentResponseErrorCode

    template = types.ModuleType("homeassistant.helpers.template")

    class Template:
        def __init__(self, raw, hass):
            self.raw = raw
            self.hass = hass

        def async_render(self, data, parse_result=False):
            return self.raw

    template.Template = Template

    aiohttp_client = types.ModuleType("homeassistant.helpers.aiohttp_client")

    async def async_get_clientsession(hass):
        return None

    def async_create_clientsession(hass):
        return None

    aiohttp_client.async_get_clientsession = async_get_clientsession
    aiohttp_client.async_create_clientsession = async_create_clientsession

    entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")

    def async_get(hass):
        return types.SimpleNamespace(
            async_get=lambda entity_id: types.SimpleNamespace(aliases=["alias"])
        )

    entity_registry.async_get = async_get

    config_validation = types.ModuleType("homeassistant.helpers.config_validation")
    config_validation.url_no_path = lambda value: value

    update_coordinator = types.ModuleType("homeassistant.helpers.update_coordinator")

    class DataUpdateCoordinator:
        def __init__(self, hass=None, logger=None, name=None, update_interval=None):
            self.hass = hass

    class UpdateFailed(Exception):
        pass

    update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    update_coordinator.UpdateFailed = UpdateFailed

    helpers.intent = intent
    helpers.template = template
    helpers.aiohttp_client = aiohttp_client
    helpers.entity_registry = entity_registry
    helpers.config_validation = config_validation
    helpers.update_coordinator = update_coordinator

    util = types.ModuleType("homeassistant.util")
    ulid = types.ModuleType("homeassistant.util.ulid")
    ulid.ulid = lambda: "ulid"
    util.ulid = ulid

    sys.modules["homeassistant.components"] = components
    sys.modules["homeassistant.components.conversation"] = conversation
    sys.modules["homeassistant.components.homeassistant.exposed_entities"] = (
        homeassistant_components_ha
    )
    sys.modules["homeassistant.config_entries"] = config_entries
    sys.modules["homeassistant.data_entry_flow"] = data_entry_flow
    sys.modules["homeassistant.const"] = const
    sys.modules["homeassistant.core"] = core
    sys.modules["homeassistant.exceptions"] = exceptions
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.intent"] = intent
    sys.modules["homeassistant.helpers.template"] = template
    sys.modules["homeassistant.helpers.aiohttp_client"] = aiohttp_client
    sys.modules["homeassistant.helpers.entity_registry"] = entity_registry
    sys.modules["homeassistant.helpers.config_validation"] = config_validation
    sys.modules["homeassistant.helpers.update_coordinator"] = update_coordinator
    sys.modules["homeassistant.util"] = util
    sys.modules["homeassistant.util.ulid"] = ulid


def _install_other_stubs():
    aiohttp = types.ModuleType("aiohttp")

    class ClientSession:
        pass

    aiohttp.ClientSession = ClientSession
    sys.modules["aiohttp"] = aiohttp

    async_timeout = types.ModuleType("async_timeout")

    @asynccontextmanager
    async def timeout(_):
        yield

    async_timeout.timeout = timeout
    sys.modules["async_timeout"] = async_timeout

    voluptuous = types.ModuleType("voluptuous")

    class Invalid(Exception):
        pass

    voluptuous.Invalid = Invalid
    voluptuous.Required = lambda key, default=None: key
    voluptuous.Schema = lambda schema: schema
    sys.modules["voluptuous"] = voluptuous


_install_homeassistant_stubs()
_install_other_stubs()

import meeseeks_ha_conversation as ha_module  # noqa: E402
from meeseeks_ha_conversation import (  # noqa: E402
    MeeseeksAgent,
)
from meeseeks_ha_conversation.api import MeeseeksApiClient  # noqa: E402
from meeseeks_ha_conversation.config_flow import (  # noqa: E402
    MeeseeksConfigFlow,
    MeeseeksOptionsFlow,
)
from meeseeks_ha_conversation.coordinator import (  # noqa: E402
    MeeseeksDataUpdateCoordinator,
)
from meeseeks_ha_conversation.exceptions import ApiClientError  # noqa: E402
from meeseeks_ha_conversation.helpers import get_exposed_entities  # noqa: E402


class DummySession:
    """Stub aiohttp session for API client testing."""

    def __init__(self, payload):
        """Initialize the session with a payload."""
        self.payload = payload
        self.last_request = None

    async def request(self, method=None, url=None, headers=None, json=None):
        """Capture the request metadata and return a dummy response."""
        self.last_request = {"method": method, "url": url, "headers": headers, "json": json}
        return DummyResponse(self.payload)


class DummyResponse:
    """Stub response object for aiohttp interactions."""

    def __init__(self, payload):
        """Initialize the response with a payload."""
        self._payload = payload
        self.status = 200

    def raise_for_status(self):
        """Return without raising to mimic successful responses."""
        return None

    async def json(self):
        """Return the response payload as JSON."""
        return dict(self._payload)

    async def text(self):
        """Return a placeholder text response."""
        return "ok"


def test_api_generate_includes_session_id():
    """Include session_id in API request payloads."""
    session = DummySession({"task_result": "ok"})
    client = MeeseeksApiClient(base_url="http://test", timeout=10, session=session)
    result = asyncio.run(client.async_generate({"prompt": "hello", "session_id": "abc"}))
    assert session.last_request["json"]["session_id"] == "abc"
    assert result["response"] == "ok"
    assert result["context"] == "ok"


def test_api_generate_requires_prompt():
    """Raise when API requests omit a prompt."""
    session = DummySession({"task_result": "ok"})
    client = MeeseeksApiClient(base_url="http://test", timeout=10, session=session)
    try:
        asyncio.run(client.async_generate({}))
    except ValueError:
        assert True
    else:
        assert False


def test_coordinator_update_success():
    """Update coordinator successfully when heartbeat passes."""

    class Client:
        async def async_get_heartbeat(self):
            return True

    coordinator = MeeseeksDataUpdateCoordinator(hass=None, client=Client())
    result = asyncio.run(coordinator._async_update_data())
    assert result is True


def test_coordinator_update_failure():
    """Raise UpdateFailed when heartbeat errors."""

    class Client:
        async def async_get_heartbeat(self):
            raise ApiClientError("bad")

    coordinator = MeeseeksDataUpdateCoordinator(hass=None, client=Client())
    try:
        asyncio.run(coordinator._async_update_data())
    except Exception as exc:
        assert exc.__class__.__name__ == "UpdateFailed"
    else:
        assert False


def test_helpers_get_exposed_entities():
    """Return exposed entities with aliases."""
    hass = types.SimpleNamespace(
        states=types.SimpleNamespace(
            async_all=lambda: [
                types.SimpleNamespace(entity_id="light.kitchen", name="Kitchen", state="on")
            ]
        )
    )
    entities = get_exposed_entities(hass)
    assert entities[0]["entity_id"] == "light.kitchen"
    assert entities[0]["aliases"] == ["alias"]


def test_agent_process_success():
    """Process conversation input successfully."""
    hass = types.SimpleNamespace()
    entry = types.SimpleNamespace()
    client = types.SimpleNamespace()
    agent = MeeseeksAgent(hass, entry, client)

    async def fake_query(messages):
        return {"response": "hi", "context": {}, "session_id": "sid", "task_result": "hi"}

    agent.query = fake_query

    ConversationInput = sys.modules["homeassistant.components.conversation"].ConversationInput
    user_input = ConversationInput("conv1", "hello", "en")
    result = asyncio.run(agent.async_process(user_input))
    assert result.response.speech == "hi"


def test_agent_process_error():
    """Return error responses when agent processing fails."""
    hass = types.SimpleNamespace()
    entry = types.SimpleNamespace()
    client = types.SimpleNamespace()
    agent = MeeseeksAgent(hass, entry, client)

    HomeAssistantError = sys.modules["homeassistant.exceptions"].HomeAssistantError

    async def fake_query(messages):
        raise HomeAssistantError("boom")

    agent.query = fake_query

    ConversationInput = sys.modules["homeassistant.components.conversation"].ConversationInput
    user_input = ConversationInput("conv1", "hello", "en")
    result = asyncio.run(agent.async_process(user_input))
    assert result.response.error is not None


def test_agent_process_uses_history():
    """Reuse history when conversation_id already exists."""
    hass = types.SimpleNamespace()
    entry = types.SimpleNamespace()
    client = types.SimpleNamespace()
    agent = MeeseeksAgent(hass, entry, client)

    async def fake_query(messages):
        return {"response": "hi", "context": {}, "session_id": "sid", "task_result": "hi"}

    agent.query = fake_query
    ConversationInput = sys.modules["homeassistant.components.conversation"].ConversationInput
    user_input = ConversationInput("conv1", "hello", "en")
    first = asyncio.run(agent.async_process(user_input))
    assert first.conversation_id in agent.history
    followup = ConversationInput(first.conversation_id, "hello again", "en")
    asyncio.run(agent.async_process(followup))
    assert first.conversation_id in agent.history


def test_config_flow_show_form():
    """Show config flow form on initial step."""
    flow = MeeseeksConfigFlow()
    flow.hass = object()
    result = asyncio.run(flow.async_step_user(None))
    assert result["type"] == "form"


def test_config_flow_duplicate(monkeypatch):
    """Abort config flow when duplicate entries exist."""
    flow = MeeseeksConfigFlow()
    flow.hass = object()
    flow._async_current_entries = lambda include_ignore=False: [
        types.SimpleNamespace(data={"base_url": "http://test"})
    ]
    user_input = {
        "base_url": "http://test",
        "api_key": "token",
        "timeout": 10,
    }
    result = asyncio.run(flow.async_step_user(user_input))
    assert result["type"] == "abort"


def test_config_flow_success(monkeypatch):
    """Create config entries after successful validation."""
    flow = MeeseeksConfigFlow()
    flow.hass = object()

    async def fake_heartbeat(self):
        return True

    monkeypatch.setattr(MeeseeksApiClient, "async_get_heartbeat", fake_heartbeat)
    user_input = {
        "base_url": "http://test",
        "api_key": "token",
        "timeout": 10,
    }
    result = asyncio.run(flow.async_step_user(user_input))
    assert result["type"] == "create_entry"
    assert result["data"]["base_url"] == "http://test"


def test_config_flow_error(monkeypatch):
    """Return error form when heartbeat raises."""
    flow = MeeseeksConfigFlow()
    flow.hass = object()

    async def fake_heartbeat(self):
        raise RuntimeError("boom")

    monkeypatch.setattr(MeeseeksApiClient, "async_get_heartbeat", fake_heartbeat)
    user_input = {
        "base_url": "http://test",
        "api_key": "token",
        "timeout": 10,
    }
    result = asyncio.run(flow.async_step_user(user_input))
    assert result["type"] == "form"
    assert result["errors"]["base"] == "unknown"


def test_options_flow_menu():
    """Return options flow menu results."""
    flow = MeeseeksOptionsFlow(types.SimpleNamespace(options={}))
    result = asyncio.run(flow.async_step_init())
    assert result["type"] == "menu"


def test_options_flow_other_steps():
    """Cover other options flow steps."""
    flow = MeeseeksOptionsFlow(types.SimpleNamespace(options={}))
    assert asyncio.run(flow.async_step_all_set())["type"] == "menu"
    assert asyncio.run(flow.async_step_general_config())["type"] == "menu"
    assert asyncio.run(flow.async_step_prompt_system())["type"] == "menu"
    assert asyncio.run(flow.async_step_model_config())["type"] == "menu"


def test_async_setup_and_unload_entry(monkeypatch):
    """Set up and unload the Home Assistant integration."""
    from meeseeks_ha_conversation.const import CONF_BASE_URL, CONF_TIMEOUT

    class DummyClient:
        def __init__(self, *args, **kwargs):
            pass

        async def async_get_heartbeat(self):
            return True

    class DummyCoordinator:
        def __init__(self, *_args, **_kwargs):
            pass

        async def async_config_entry_first_refresh(self):
            return None

    monkeypatch.setattr(ha_module, "MeeseeksApiClient", DummyClient)
    monkeypatch.setattr(ha_module, "MeeseeksDataUpdateCoordinator", DummyCoordinator)
    monkeypatch.setattr(ha_module, "async_get_clientsession", lambda _hass: None)

    ConfigEntry = sys.modules["homeassistant.config_entries"].ConfigEntry
    hass = types.SimpleNamespace(data={})
    entry = ConfigEntry(
        data={CONF_BASE_URL: "http://test"},
        options={CONF_TIMEOUT: 10},
    )
    assert asyncio.run(ha_module.async_setup_entry(hass, entry)) is True
    assert hass._agent is not None
    assert asyncio.run(ha_module.async_unload_entry(hass, entry)) is True
    assert hass._agent is None


def test_async_reload_entry(monkeypatch):
    """Reload calls unload and setup."""
    calls = []

    async def fake_unload(*_args, **_kwargs):
        calls.append("unload")
        return True

    async def fake_setup(*_args, **_kwargs):
        calls.append("setup")
        return True

    monkeypatch.setattr(ha_module, "async_unload_entry", fake_unload)
    monkeypatch.setattr(ha_module, "async_setup_entry", fake_setup)
    hass = types.SimpleNamespace()
    entry = types.SimpleNamespace()
    asyncio.run(ha_module.async_reload_entry(hass, entry))
    assert calls == ["unload", "setup"]


def test_supported_languages_and_prompt_render():
    """Expose supported languages and prompt rendering."""
    hass = types.SimpleNamespace(config=types.SimpleNamespace(location_name="Home"))
    entry = types.SimpleNamespace()
    client = types.SimpleNamespace()
    agent = MeeseeksAgent(hass, entry, client)
    assert agent.supported_languages == "*"
    rendered = agent._async_generate_prompt("hello", [])
    assert rendered == "hello"


def test_missing_homeassistant_error():
    """Raise when Home Assistant is unavailable."""
    original_available = ha_module._HOMEASSISTANT_AVAILABLE
    original_error = getattr(ha_module, "_HOMEASSISTANT_IMPORT_ERROR", None)
    try:
        ha_module._HOMEASSISTANT_AVAILABLE = False
        ha_module._HOMEASSISTANT_IMPORT_ERROR = ModuleNotFoundError("ha")
        error = ha_module._missing_homeassistant_error()
        assert isinstance(error, RuntimeError)
        assert isinstance(error.__cause__, ModuleNotFoundError)
    finally:
        ha_module._HOMEASSISTANT_AVAILABLE = original_available
        ha_module._HOMEASSISTANT_IMPORT_ERROR = original_error


def test_api_wrapper_decode_json_false():
    """Return text response when decode_json is disabled."""

    class Response:
        status = 200

        def raise_for_status(self):
            return None

        async def json(self):
            return {"task_result": "ok"}

        async def text(self):
            return "plain"

    class Session:
        async def request(self, **_kwargs):
            return Response()

    client = MeeseeksApiClient(base_url="http://test", timeout=10, session=Session())
    result = asyncio.run(client._meeseeks_api_wrapper("get", "http://test", decode_json=False))
    assert result == "plain"


def test_api_wrapper_handles_404():
    """Raise ApiJsonError when API returns 404 payload."""

    class Response:
        status = 404

        def raise_for_status(self):
            return None

        async def json(self):
            return {"error": "nope"}

        async def text(self):
            return "nope"

    class Session:
        async def request(self, **_kwargs):
            return Response()

    client = MeeseeksApiClient(base_url="http://test", timeout=10, session=Session())
    try:
        asyncio.run(client._meeseeks_api_wrapper("get", "http://test"))
    except Exception as exc:
        assert exc.__class__.__name__ == "ApiJsonError"
    else:
        assert False
