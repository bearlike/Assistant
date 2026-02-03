"""Integration tests for core tools and adapters."""
import asyncio
import sys
import types

import pytest

from core.common import get_mock_speaker
from tools.core.talk_to_user import TalkToUser
from tools.integration.homeassistant import HomeAssistant
from tools.integration.mcp import MCPToolRunner, _load_mcp_config, discover_mcp_tools


def test_talk_to_user_set_state(monkeypatch, tmp_path):
    """Echo the action argument when setting state."""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    tool = TalkToUser()
    step = types.SimpleNamespace(action_argument="hello")
    result = tool.set_state(step)
    assert result.content == "hello"


def test_talk_to_user_requires_step(monkeypatch, tmp_path):
    """Require an action step for TalkToUser set_state."""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    tool = TalkToUser()
    with pytest.raises(ValueError):
        tool.set_state(None)


def test_talk_to_user_skips_llm(monkeypatch, tmp_path):
    """Ensure TalkToUser does not initialize an LLM client."""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))

    def _boom(*_args, **_kwargs):
        raise AssertionError("build_chat_model should not be called")

    monkeypatch.setattr("core.classes.build_chat_model", _boom)
    TalkToUser()


def test_mcp_config_requires_env(monkeypatch):
    """Raise when MCP config environment variable is missing."""
    monkeypatch.delenv("MESEEKS_MCP_CONFIG", raising=False)
    with pytest.raises(ValueError):
        _load_mcp_config()


def test_mcp_config_normalizes_legacy_keys(monkeypatch, tmp_path):
    """Normalize legacy MCP keys like http_headers/transport=http."""
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        '{"servers": {"srv": {"transport": "http", "http_headers": {"a": "b"}}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("MESEEKS_MCP_CONFIG", str(config_path))
    config = _load_mcp_config()
    server = config["servers"]["srv"]
    assert server["transport"] == "streamable_http"
    assert server["headers"] == {"a": "b"}
    assert "http_headers" not in server


def test_mcp_tool_runner_uses_async(monkeypatch):
    """Use async invocation path in MCP tool runner."""
    runner = MCPToolRunner(server_name="srv", tool_name="tool")
    async def _fake_invoke(_):
        return "ok"

    monkeypatch.setattr(runner, "_invoke_async", _fake_invoke)
    step = types.SimpleNamespace(action_argument="ping")
    result = runner.run(step)
    assert result.content == "ok"


def test_mcp_discovery_normalizes_config(monkeypatch):
    """Ensure discovery passes normalized MCP config to the client."""
    captured = {}

    class DummyTool:
        def __init__(self, name):
            self.name = name

    class DummyClient:
        def __init__(self, servers):
            captured["servers"] = servers

        async def get_tools(self, server_name):
            return [DummyTool("tool_one"), DummyTool("tool_two")]

    module = types.ModuleType("langchain_mcp_adapters.client")
    module.MultiServerMCPClient = DummyClient
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters.client", module)

    config = {
        "servers": {
            "srv": {
                "transport": "http",
                "url": "http://localhost/mcp",
                "http_headers": {"Authorization": "Bearer token"},
            }
        }
    }
    discovered = discover_mcp_tools(config)
    assert discovered == {"srv": ["tool_one", "tool_two"]}
    server = captured["servers"]["srv"]
    assert server["transport"] == "streamable_http"
    assert server["headers"] == {"Authorization": "Bearer token"}


def test_mcp_invoke_async_success(monkeypatch, tmp_path):
    """Invoke an MCP tool successfully with stubbed client."""
    config_path = tmp_path / "mcp.json"
    config_path.write_text('{"servers": {"srv": {"transport": "stdio"}}}', encoding="utf-8")
    monkeypatch.setenv("MESEEKS_MCP_CONFIG", str(config_path))

    class DummyTool:
        name = "tool"

        async def ainvoke(self, input_text):
            return f"out:{input_text}"

    class DummyClient:
        def __init__(self, servers):
            self.servers = servers

        async def get_tools(self, server_name):
            return [DummyTool()]

    module = types.ModuleType("langchain_mcp_adapters.client")
    module.MultiServerMCPClient = DummyClient
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters.client", module)

    runner = MCPToolRunner(server_name="srv", tool_name="tool")
    result = asyncio.run(runner._invoke_async("hi"))
    assert result == "out:hi"


def test_mcp_invoke_async_missing_tool(monkeypatch, tmp_path):
    """Raise when the requested MCP tool is unavailable."""
    config_path = tmp_path / "mcp.json"
    config_path.write_text('{"servers": {"srv": {"transport": "stdio"}}}', encoding="utf-8")
    monkeypatch.setenv("MESEEKS_MCP_CONFIG", str(config_path))

    class DummyClient:
        def __init__(self, servers):
            self.servers = servers

        async def get_tools(self, server_name):
            return []

    module = types.ModuleType("langchain_mcp_adapters.client")
    module.MultiServerMCPClient = DummyClient
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters.client", module)

    runner = MCPToolRunner(server_name="srv", tool_name="tool")
    with pytest.raises(ValueError):
        asyncio.run(runner._invoke_async("hi"))


def _make_homeassistant():
    ha = HomeAssistant.__new__(HomeAssistant)
    ha.base_url = "http://test"
    ha._api_token = "token"
    ha.api_headers = {"Authorization": "Bearer token", "Content-Type": "application/json"}
    ha.cache = {
        "entity_ids": [],
        "sensor_ids": [],
        "entities": [],
        "services": [],
        "sensors": [],
        "allowed_domains": ["scene", "switch"],
    }
    ha.model_name = "dummy"
    ha.model = object()
    ha._save_json = lambda *_args, **_kwargs: None
    ha._load_rag_documents = lambda *_args, **_kwargs: []
    ha.update_cache = lambda: None
    return ha


def test_homeassistant_clean_answer():
    """Normalize Home Assistant response strings."""
    answer = HomeAssistant._clean_answer('RealFeel 10km/h "test"')
    assert "Real Feel" in answer
    assert "kilometer per hour" in answer
    assert '"' not in answer


def test_homeassistant_update_services(monkeypatch):
    """Update cached services from the Home Assistant API."""
    ha = _make_homeassistant()

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [{"domain": "scene"}]

    monkeypatch.setattr(
        "tools.integration.homeassistant.requests.get",
        lambda *args, **kwargs: DummyResponse(),
    )
    assert ha.update_services() is True
    assert ha.cache["services"]


def test_homeassistant_update_entities(monkeypatch):
    """Update cached entities and sensors from the API."""
    ha = _make_homeassistant()

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {"entity_id": "light.kitchen", "state": "on", "attributes": {}},
                {"entity_id": "sensor.temp", "state": "20", "attributes": {}},
            ]

    monkeypatch.setattr(
        "tools.integration.homeassistant.requests.get",
        lambda *args, **kwargs: DummyResponse(),
    )
    assert ha.update_entities() is True
    assert any(entity["entity_id"] == "light.kitchen" for entity in ha.cache["entities"])
    assert any(sensor["entity_id"] == "sensor.temp" for sensor in ha.cache["sensors"])


def test_homeassistant_update_entity_ids(monkeypatch):
    """Populate entity ID cache from fetched entities."""
    ha = _make_homeassistant()

    def fake_update_entities():
        ha.cache["entities"] = [
            {"entity_id": "light.kitchen", "state": "on", "attributes": {}},
            {"entity_id": "switch.fan", "state": "off", "attributes": {}},
        ]
        return True

    monkeypatch.setattr(ha, "update_entities", fake_update_entities)
    assert ha.update_entity_ids() is True
    assert "light.kitchen" in ha.cache["entity_ids"]


def test_homeassistant_prompt_builders():
    """Build prompts for set/get operations."""
    class DummyParser:
        def get_format_instructions(self):
            return "format"

    set_prompt = HomeAssistant._create_set_prompt("system", DummyParser())
    get_prompt = HomeAssistant._create_get_prompt("system")
    set_messages = set_prompt.format_prompt(
        action_step="turn on light",
        context="ctx",
    ).to_messages()
    get_messages = get_prompt.format_prompt(
        action_step="what is the temperature",
        context="ctx",
    ).to_messages()
    assert "Format Instructions" in set_messages[-1].content
    assert "Home Assistant Entities" in set_messages[-1].content
    assert "Home Assistant Sensors" in get_messages[-1].content


def test_homeassistant_call_service_success(monkeypatch):
    """Call a Home Assistant service successfully."""
    ha = _make_homeassistant()

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [{"ok": True}]

        @property
        def text(self):
            return "ok"

    monkeypatch.setattr(
        "tools.integration.homeassistant.requests.post",
        lambda *args, **kwargs: DummyResponse(),
    )
    ok, payload = ha.call_service("scene", "turn_on", "scene.lamp")
    assert ok is True
    assert payload == [{"ok": True}]


def test_homeassistant_call_service_invalid_domain():
    """Reject service calls with invalid domains."""
    ha = _make_homeassistant()
    with pytest.raises(ValueError):
        ha.call_service("invalid", "turn_on", "scene.lamp")


def test_homeassistant_invoke_service(monkeypatch):
    """Invoke a service call and return a success response."""
    ha = _make_homeassistant()

    class DummyCall:
        domain = "scene"
        service = "turn_on"
        entity_id = "scene.lamp"

    class DummyChain:
        def invoke(self, *args, **kwargs):
            return DummyCall()

    monkeypatch.setattr(ha, "call_service", lambda **kwargs: (True, {"ok": True}))
    step = types.SimpleNamespace(action_argument="turn on lamp")
    result = ha._invoke_service_and_set_state(DummyChain(), [], step)
    assert "Successfully called service" in result.content


def test_homeassistant_set_state(monkeypatch):
    """Set state via the Home Assistant tool flow."""
    ha = _make_homeassistant()

    class DummyChain:
        def __or__(self, _other):
            return self

    class DummyParser:
        def __init__(self, *args, **kwargs):
            pass

        def get_format_instructions(self):
            return "format"

    monkeypatch.setattr(
        "tools.integration.homeassistant.PydanticOutputParser",
        DummyParser,
    )
    monkeypatch.setattr(HomeAssistant, "_create_set_prompt", lambda *_a, **_k: DummyChain())
    monkeypatch.setattr(
        "tools.integration.homeassistant.ha_render_system_prompt",
        lambda *args, **kwargs: "prompt",
    )
    monkeypatch.setattr(
        HomeAssistant,
        "_invoke_service_and_set_state",
        lambda *a, **k: get_mock_speaker()(content="ok"),
    )
    step = types.SimpleNamespace(action_argument="turn on")
    result = ha.set_state(step)
    assert result.content == "ok"


def test_homeassistant_get_state(monkeypatch):
    """Fetch state via the Home Assistant tool flow."""
    ha = _make_homeassistant()

    class DummyChain:
        def __or__(self, _other):
            return self

        def invoke(self, *args, **kwargs):
            return types.SimpleNamespace(content="answer")

    monkeypatch.setattr(HomeAssistant, "_create_get_prompt", lambda *_a, **_k: DummyChain())
    monkeypatch.setattr(
        "tools.integration.homeassistant.ha_render_system_prompt",
        lambda *args, **kwargs: "prompt",
    )
    step = types.SimpleNamespace(action_argument="status")
    result = ha.get_state(step)
    assert result.content == "answer"
