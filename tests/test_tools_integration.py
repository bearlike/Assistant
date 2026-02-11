"""Integration tests for core tools and adapters."""

import asyncio
import sys
import types

import pytest
from meeseeks_core.common import get_mock_speaker
from meeseeks_core.config import set_mcp_config_path
from meeseeks_tools.integration.homeassistant import HomeAssistant, cache_monitor
from meeseeks_tools.integration.mcp import (
    MCPToolRunner,
    _load_mcp_config,
    _prepare_mcp_input,
    discover_mcp_tool_details_with_failures,
    discover_mcp_tools,
)


def test_mcp_config_requires_env(monkeypatch):
    """Raise when MCP config environment variable is missing."""
    set_mcp_config_path(None)
    with pytest.raises(ValueError):
        _load_mcp_config()


def test_mcp_config_normalizes_legacy_keys(monkeypatch, tmp_path):
    """Normalize legacy MCP keys like http_headers/transport=http."""
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        '{"servers": {"srv": {"transport": "http", "http_headers": {"a": "b"}}}}',
        encoding="utf-8",
    )
    set_mcp_config_path(str(config_path))
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
    step = types.SimpleNamespace(tool_input="ping")
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


def test_mcp_discovery_schema_and_runtime_failure(monkeypatch, tmp_path):
    """Cover schema extraction and runtime error logging in one flow."""
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        '{"servers": {"good": {"transport": "stdio"}, "bad": {"transport": "stdio"}}}'
    )
    set_mcp_config_path(str(config_path))

    class SchemaModelJson:
        def model_json_schema(self):
            return {
                "required": "bad",
                "properties": {"query": "bad"},
            }

    class SchemaLegacy:
        def schema(self):
            return {
                "required": ["query"],
                "properties": "bad",
            }

    class SchemaNonDict:
        def schema(self):
            return "bad"

    class ToolA:
        name = "tool_a"
        args_schema = SchemaModelJson()

        async def ainvoke(self, _input):
            raise DummyError()

    class ToolB:
        name = "tool_b"
        args_schema = SchemaLegacy()

    class ToolC:
        name = "tool_c"
        args_schema = SchemaNonDict()

    class ToolD:
        name = "tool_d"
        args_schema = object()

    class ToolE:
        name = "tool_e"
        args_schema = SchemaLegacy()

        async def ainvoke(self, _input):
            raise RuntimeError("plain")

    class DummyError(Exception):
        def __init__(self):
            super().__init__("boom")
            self.exceptions = (RuntimeError("sub1"), RuntimeError("sub2"))

    class DummyClient:
        def __init__(self, servers):
            self.servers = servers

        async def get_tools(self, server_name):
            if server_name == "bad":
                raise DummyError()
            if server_name == "plain":
                raise RuntimeError("plain")
            return [ToolA(), ToolB(), ToolC(), ToolD(), ToolE()]

    module = types.ModuleType("langchain_mcp_adapters.client")
    module.MultiServerMCPClient = DummyClient
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters.client", module)

    discovered, failures = discover_mcp_tool_details_with_failures(
        {
            "servers": {
                "good": {"transport": "stdio"},
                "bad": {"transport": "stdio"},
                "plain": {"transport": "stdio"},
            }
        }
    )
    schema = discovered["good"][0]["schema"]
    assert schema["required"] == []
    assert "bad" in failures

    runner = MCPToolRunner(server_name="good", tool_name="tool_a")
    with pytest.raises(DummyError):
        asyncio.run(runner._invoke_async("hi"))
    runner = MCPToolRunner(server_name="good", tool_name="tool_e")
    with pytest.raises(RuntimeError):
        asyncio.run(runner._invoke_async("hi"))


def test_mcp_invoke_async_success(monkeypatch, tmp_path):
    """Invoke an MCP tool successfully with stubbed client."""
    config_path = tmp_path / "mcp.json"
    config_path.write_text('{"servers": {"srv": {"transport": "stdio"}}}', encoding="utf-8")
    set_mcp_config_path(str(config_path))

    class DummyTool:
        name = "tool"

        async def ainvoke(self, input_text):
            if isinstance(input_text, dict):
                return f"out:{input_text.get('query')}"
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


@pytest.mark.parametrize(
    ("schema", "argument", "expected"),
    [
        (types.SimpleNamespace(model_fields={"query": object()}), "hi", {"query": "hi"}),
        (
            types.SimpleNamespace(model_fields={"query": object(), "other": object()}),
            "hello",
            {"query": "hello"},
        ),
        (
            types.SimpleNamespace(model_fields={"alpha": object(), "beta": object()}),
            "hi",
            "hi",
        ),
        (
            {
                "required": ["query"],
                "properties": {"query": {"type": "string"}},
            },
            "hello",
            {"query": "hello"},
        ),
        (
            types.SimpleNamespace(__fields__={"question": object()}),
            "hi",
            {"question": "hi"},
        ),
        (
            {
                "required": ["query"],
                "properties": {"query": {"type": "array", "items": {"type": "string"}}},
            },
            "hi",
            {"query": ["hi"]},
        ),
        (
            {"properties": "bad"},
            "hello",
            "hello",
        ),
        (
            {"properties": {"query": {"type": "string"}}},
            {"query": "hi"},
            {"query": "hi"},
        ),
        (
            {"properties": {"query": {"type": "string"}}},
            123,
            123,
        ),
    ],
)
def test_mcp_prepare_input_variants(schema, argument, expected):
    """Normalize string payloads for MCP tools with schemas."""
    tool = types.SimpleNamespace(args_schema=schema)
    payload = _prepare_mcp_input(tool, argument)
    assert payload == expected


def test_mcp_prepare_input_parses_json_string():
    """Parse JSON object strings into dictionaries."""
    tool = types.SimpleNamespace(args_schema=None)
    payload = _prepare_mcp_input(tool, '{"question": "hi"}')
    assert payload == {"question": "hi"}
    fallback = _prepare_mcp_input(tool, "{bad json}")
    assert fallback == "{bad json}"


def test_mcp_invoke_async_wraps_string_for_schema(monkeypatch, tmp_path):
    """Ensure MCP invoke passes dict payloads when args_schema exists."""
    config_path = tmp_path / "mcp.json"
    config_path.write_text('{"servers": {"srv": {"transport": "stdio"}}}', encoding="utf-8")
    set_mcp_config_path(str(config_path))
    captured = {}

    class DummyArgsSchema:
        model_fields = {"query": object()}

    class DummyTool:
        name = "tool"
        args_schema = DummyArgsSchema

        async def ainvoke(self, input_text):
            captured["payload"] = input_text
            return "ok"

    class DummyClient:
        def __init__(self, servers):
            self.servers = servers

        async def get_tools(self, server_name):
            return [DummyTool()]

    module = types.ModuleType("langchain_mcp_adapters.client")
    module.MultiServerMCPClient = DummyClient
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters.client", module)

    runner = MCPToolRunner(server_name="srv", tool_name="tool")
    asyncio.run(runner._invoke_async("hi"))
    assert captured["payload"] == {"query": "hi"}


def test_mcp_invoke_async_missing_tool(monkeypatch, tmp_path):
    """Raise when the requested MCP tool is unavailable."""
    config_path = tmp_path / "mcp.json"
    config_path.write_text('{"servers": {"srv": {"transport": "stdio"}}}', encoding="utf-8")
    set_mcp_config_path(str(config_path))

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
        "meeseeks_tools.integration.homeassistant.requests.get",
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
        "meeseeks_tools.integration.homeassistant.requests.get",
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
        "meeseeks_tools.integration.homeassistant.requests.post",
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
    step = types.SimpleNamespace(tool_input="turn on lamp")
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
        "meeseeks_tools.integration.homeassistant.PydanticOutputParser",
        DummyParser,
    )
    monkeypatch.setattr(HomeAssistant, "_create_set_prompt", lambda *_a, **_k: DummyChain())
    monkeypatch.setattr(
        "meeseeks_tools.integration.homeassistant.ha_render_system_prompt",
        lambda *args, **kwargs: "prompt",
    )
    monkeypatch.setattr(
        HomeAssistant,
        "_invoke_service_and_set_state",
        lambda *a, **k: get_mock_speaker()(content="ok"),
    )
    step = types.SimpleNamespace(tool_input="turn on")
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
        "meeseeks_tools.integration.homeassistant.ha_render_system_prompt",
        lambda *args, **kwargs: "prompt",
    )
    step = types.SimpleNamespace(tool_input="status")
    result = ha.get_state(step)
    assert result.content == "answer"


def test_cache_monitor_cleans_entities():
    """Clean entity and service cache data through the decorator."""

    class DummyCacheHolder:
        def __init__(self):
            self.cache = {
                "entities": [
                    {
                        "entity_id": "light.kitchen",
                        "attributes": {"icon": "light"},
                        "state": "on",
                        "context": {},
                        "last_changed": "x",
                        "last_reported": "x",
                        "last_updated": "x",
                    },
                    {
                        "entity_id": "sensor.temp",
                        "attributes": {"icon": "sensor"},
                        "state": "5",
                    },
                    {
                        "entity_id": "scene.morning",
                        "attributes": {},
                        "state": "on",
                    },
                ],
                "services": [{"domain": "light"}, {"domain": "switch"}],
                "allowed_domains": ["light"],
                "entity_ids": ["light.kitchen", "sensor.temp", "scene.morning"],
                "sensor_ids": ["sensor.temp"],
                "sensors": [],
            }

        @cache_monitor
        def refresh(self):
            return "ok"

    holder = DummyCacheHolder()
    assert holder.refresh() == "ok"
    assert all(service["domain"] == "light" for service in holder.cache["services"])
    assert any(entity["entity_id"].startswith("light.") for entity in holder.cache["entities"])
    assert any(sensor["entity_id"].startswith("sensor.") for sensor in holder.cache["sensors"])
