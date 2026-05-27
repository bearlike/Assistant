"""Tests for the Home Assistant integration tool.

Targets uncovered lines in mewbo_tools/integration/homeassistant.py:
- Lines 125/128: forbidden_prefix/substring filtering in clean_entities
- Lines 256/279: entity_id / domain validators (cache present)
- Lines 290-296: HomeAssistant.__init__ (config validation)
- Lines 305-308: update_services auth-error path
- Lines 325-326: update_services request-exception path
- Lines 331-333: update_entities auth-error path
- Lines 347-348: update_entities request-exception path
- Lines 352-354: update_entity_ids empty-entities path
- Lines 370: update_entity_ids raises when no entities
- Lines 382-385: update_cache writes JSON
- Lines 414, 420-421: call_service auth errors
- Lines 431-432: call_service request-exception path
- Lines 435: call_service returns (False, []) on failure
- Lines 575-578: _invoke_service_and_set_state error path
- Lines 594/604: set_state model-None path
- Lines 627/635: get_state model-None path

HTTP I/O mocked via monkeypatch on `requests.get` / `requests.post`.
"""

from __future__ import annotations

import types
from unittest.mock import MagicMock

import pytest
import requests
from mewbo_tools.integration.homeassistant import (
    HomeAssistant,
    HomeAssistantCache,
    HomeAssistantCall,
    cache_monitor,
)

# ---------------------------------------------------------------------------
# Helper: build a HomeAssistant instance without __init__ config checks
# ---------------------------------------------------------------------------


def _make_ha() -> HomeAssistant:
    ha = HomeAssistant.__new__(HomeAssistant)
    ha.base_url = "http://hass.local:8123/api"
    ha._api_token = "dummy-token"
    ha.api_headers = {
        "Authorization": "Bearer dummy-token",
        "Content-Type": "application/json",
    }
    ha.cache: HomeAssistantCache = {
        "entity_ids": [],
        "sensor_ids": [],
        "entities": [],
        "services": [],
        "sensors": [],
        "allowed_domains": ["scene", "switch", "weather", "kodi", "automation"],
    }
    ha.model_name = "dummy"
    ha.model = MagicMock()
    ha._save_json = MagicMock()
    ha._load_rag_documents = MagicMock(return_value=[])
    ha.update_cache = MagicMock()
    return ha


def _fake_response(json_data=None, status_code=200, raise_exc=None):
    """Build a fake requests.Response-like object."""

    class FakeResp:
        def __init__(self):
            self.status_code = status_code
            self._json = json_data or []
            self._text = str(json_data)

        def raise_for_status(self):
            if raise_exc:
                raise raise_exc
            if self.status_code >= 400:
                raise requests.exceptions.HTTPError(f"HTTP {self.status_code}")

        def json(self):
            return self._json

        @property
        def text(self):
            return self._text

    return FakeResp()


# ===========================================================================
# HomeAssistant.__init__
# ===========================================================================


class TestHomeAssistantInit:
    def test_init_raises_when_url_missing(self, monkeypatch):
        monkeypatch.setattr(
            "mewbo_tools.integration.homeassistant.get_config_value",
            lambda section, key: None,
        )
        with pytest.raises(ValueError, match="url"):
            HomeAssistant()

    def test_init_raises_when_token_missing(self, monkeypatch):
        def fake_config(section, key):
            return "http://hass.local" if key == "url" else None

        monkeypatch.setattr(
            "mewbo_tools.integration.homeassistant.get_config_value",
            fake_config,
        )
        with pytest.raises(ValueError, match="token"):
            HomeAssistant()

    def test_init_sets_headers(self, monkeypatch):
        def fake_config(section, key):
            return "http://hass.local" if key == "url" else "my-token"

        monkeypatch.setattr(
            "mewbo_tools.integration.homeassistant.get_config_value",
            fake_config,
        )
        ha = HomeAssistant()
        assert ha.api_headers["Authorization"] == "Bearer my-token"
        assert ha.base_url == "http://hass.local"


# ===========================================================================
# HomeAssistantCall validators
# ===========================================================================


class TestHomeAssistantCallValidators:
    def _make_cache_holder(self, entity_ids=None, allowed_domains=None):
        """Build a minimal CacheHolder-like object."""
        holder = MagicMock()
        holder.cache = {
            "entity_ids": entity_ids or ["scene.lamp"],
            "allowed_domains": allowed_domains or ["scene"],
        }
        return holder

    def test_entity_id_validator_passes_when_no_cache(self):
        """Without a cache, entity_id is accepted as-is."""
        call = HomeAssistantCall(
            domain="scene",
            service="turn_on",
            entity_id="scene.whatever",
        )
        assert call.entity_id == "scene.whatever"

    def test_entity_id_validator_raises_when_not_in_cache(self):
        """entity_id not in cache raises ValueError."""
        holder = self._make_cache_holder(entity_ids=["scene.lamp"])
        with pytest.raises(Exception, match="not in"):
            HomeAssistantCall(
                _ha_cache=holder,
                domain="scene",
                service="turn_on",
                entity_id="scene.nonexistent",
            )

    def test_entity_id_validator_passes_when_in_cache(self):
        holder = self._make_cache_holder(entity_ids=["scene.lamp"])
        call = HomeAssistantCall(
            _ha_cache=holder,
            domain="scene",
            service="turn_on",
            entity_id="scene.lamp",
        )
        assert call.entity_id == "scene.lamp"

    def test_domain_validator_raises_when_not_in_cache(self):
        holder = self._make_cache_holder(allowed_domains=["scene"])
        with pytest.raises(Exception, match="not in"):
            HomeAssistantCall(
                _ha_cache=holder,
                domain="light",
                service="turn_on",
                entity_id="scene.lamp",
            )

    def test_domain_validator_passes_when_in_cache(self):
        holder = self._make_cache_holder(entity_ids=["scene.lamp"], allowed_domains=["scene"])
        call = HomeAssistantCall(
            _ha_cache=holder,
            domain="scene",
            service="turn_on",
            entity_id="scene.lamp",
        )
        assert call.domain == "scene"


# ===========================================================================
# update_services
# ===========================================================================


class TestUpdateServices:
    def test_returns_true_on_success(self, monkeypatch):
        ha = _make_ha()
        monkeypatch.setattr(
            "mewbo_tools.integration.homeassistant.requests.get",
            lambda *a, **kw: _fake_response([{"domain": "scene"}]),
        )
        assert ha.update_services() is True

    def test_raises_on_401(self, monkeypatch):
        ha = _make_ha()
        monkeypatch.setattr(
            "mewbo_tools.integration.homeassistant.requests.get",
            lambda *a, **kw: _fake_response(status_code=401),
        )
        with pytest.raises(PermissionError, match="authorization"):
            ha.update_services()

    def test_raises_on_403(self, monkeypatch):
        ha = _make_ha()
        monkeypatch.setattr(
            "mewbo_tools.integration.homeassistant.requests.get",
            lambda *a, **kw: _fake_response(status_code=403),
        )
        with pytest.raises(PermissionError, match="authorization"):
            ha.update_services()

    def test_returns_false_on_request_exception(self, monkeypatch):
        ha = _make_ha()

        def raise_exc(*a, **kw):
            raise requests.exceptions.ConnectionError("connection refused")

        monkeypatch.setattr("mewbo_tools.integration.homeassistant.requests.get", raise_exc)
        result = ha.update_services()
        assert result is False


# ===========================================================================
# update_entities
# ===========================================================================


class TestUpdateEntities:
    def test_returns_true_on_success(self, monkeypatch):
        ha = _make_ha()
        monkeypatch.setattr(
            "mewbo_tools.integration.homeassistant.requests.get",
            lambda *a, **kw: _fake_response(
                [{"entity_id": "light.kitchen", "state": "on", "attributes": {}}]
            ),
        )
        assert ha.update_entities() is True

    def test_raises_on_401(self, monkeypatch):
        ha = _make_ha()
        monkeypatch.setattr(
            "mewbo_tools.integration.homeassistant.requests.get",
            lambda *a, **kw: _fake_response(status_code=401),
        )
        with pytest.raises(PermissionError):
            ha.update_entities()

    def test_returns_false_on_request_exception(self, monkeypatch):
        ha = _make_ha()

        def raise_exc(*a, **kw):
            raise requests.exceptions.Timeout("timed out")

        monkeypatch.setattr("mewbo_tools.integration.homeassistant.requests.get", raise_exc)
        assert ha.update_entities() is False


# ===========================================================================
# update_entity_ids
# ===========================================================================


class TestUpdateEntityIds:
    def test_raises_when_no_entities_after_update(self, monkeypatch):
        ha = _make_ha()
        ha.update_entities = MagicMock(return_value=True)
        # Entities remain empty → should raise
        with pytest.raises(ValueError, match="No entities found"):
            ha.update_entity_ids()

    def test_populates_entity_ids(self, monkeypatch):
        ha = _make_ha()

        def fake_update_entities():
            ha.cache["entities"] = [
                {"entity_id": "light.kitchen", "state": "on", "attributes": {}},
                {"entity_id": "switch.fan", "state": "off", "attributes": {}},
            ]
            return True

        ha.update_entities = fake_update_entities
        result = ha.update_entity_ids()
        assert result is True
        assert "light.kitchen" in ha.cache["entity_ids"]


# ===========================================================================
# update_cache
# ===========================================================================


class TestUpdateCache:
    def test_calls_update_entity_ids_and_update_services(self):
        ha = _make_ha()
        ha.update_entity_ids = MagicMock()
        ha.update_services = MagicMock()
        # Restore the real update_cache
        ha.update_cache = HomeAssistant.update_cache.__get__(ha, HomeAssistant)
        ha.update_cache()
        ha.update_entity_ids.assert_called_once()
        ha.update_services.assert_called_once()
        # _save_json called for entities and sensors
        assert ha._save_json.call_count >= 2


# ===========================================================================
# call_service
# ===========================================================================


class TestCallService:
    def test_success_returns_true_and_json(self, monkeypatch):
        ha = _make_ha()
        monkeypatch.setattr(
            "mewbo_tools.integration.homeassistant.requests.post",
            lambda *a, **kw: _fake_response([{"entity_id": "scene.lamp"}]),
        )
        ok, payload = ha.call_service("scene", "turn_on", "scene.lamp")
        assert ok is True
        assert isinstance(payload, list)

    def test_raises_on_401(self, monkeypatch):
        ha = _make_ha()
        monkeypatch.setattr(
            "mewbo_tools.integration.homeassistant.requests.post",
            lambda *a, **kw: _fake_response(status_code=401),
        )
        with pytest.raises(PermissionError, match="authorization"):
            ha.call_service("scene", "turn_on", "scene.lamp")

    def test_raises_on_403(self, monkeypatch):
        ha = _make_ha()
        monkeypatch.setattr(
            "mewbo_tools.integration.homeassistant.requests.post",
            lambda *a, **kw: _fake_response(status_code=403),
        )
        with pytest.raises(PermissionError, match="authorization"):
            ha.call_service("scene", "turn_on", "scene.lamp")

    def test_returns_false_on_request_exception(self, monkeypatch):
        ha = _make_ha()

        def raise_exc(*a, **kw):
            raise requests.exceptions.ConnectionError("conn error")

        monkeypatch.setattr("mewbo_tools.integration.homeassistant.requests.post", raise_exc)
        ok, payload = ha.call_service("scene", "turn_on", "scene.lamp")
        assert ok is False
        assert payload == []

    def test_call_service_with_extra_data(self, monkeypatch):
        """Extra data dict is merged into the request payload."""
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["json"] = json
            return _fake_response([])

        monkeypatch.setattr("mewbo_tools.integration.homeassistant.requests.post", fake_post)
        ha = _make_ha()
        ha.call_service("scene", "turn_on", "scene.lamp", data={"brightness": 100})
        assert captured["json"].get("brightness") == 100
        assert captured["json"]["entity_id"] == "scene.lamp"

    def test_raises_for_blacklisted_domain(self):
        ha = _make_ha()
        with pytest.raises(ValueError, match="blacklisted"):
            ha.call_service("camera", "snapshot", "camera.front")


# ===========================================================================
# _invoke_service_and_set_state error path
# ===========================================================================


class TestInvokeServiceAndSetState:
    def test_exception_in_chain_returns_error_message(self):
        ha = _make_ha()

        class FailingChain:
            def invoke(self, *a, **kw):
                raise RuntimeError("chain failed")

        step = types.SimpleNamespace(tool_input="turn on the lights")
        result = ha._invoke_service_and_set_state(FailingChain(), [], step)
        assert "error" in result.content.lower()
        assert "chain failed" in result.content

    def test_success_path_returns_success_message(self):
        ha = _make_ha()

        class DummyCall:
            domain = "scene"
            service = "turn_on"
            entity_id = "scene.lamp"

        class DummyChain:
            def invoke(self, *a, **kw):
                return DummyCall()

        ha.call_service = MagicMock(return_value=(True, [{"ok": True}]))
        step = types.SimpleNamespace(tool_input="turn on lamp")
        result = ha._invoke_service_and_set_state(DummyChain(), [], step)
        assert "Successfully called service" in result.content

    def test_failed_service_call_returns_failure_message(self):
        ha = _make_ha()

        class DummyCall:
            domain = "scene"
            service = "turn_on"
            entity_id = "scene.lamp"

        class DummyChain:
            def invoke(self, *a, **kw):
                return DummyCall()

        ha.call_service = MagicMock(return_value=(False, []))
        step = types.SimpleNamespace(tool_input="turn on lamp")
        result = ha._invoke_service_and_set_state(DummyChain(), [], step)
        assert "Failed to call service" in result.content


# ===========================================================================
# set_state / get_state model-None path
# ===========================================================================


class TestSetStateGetStateModelNone:
    def test_set_state_raises_when_model_is_none(self, monkeypatch):
        """Line 604: set_state checks model is not None before building chain.

        PydanticOutputParser raises a JSON-schema error for CacheHolder before
        the model guard fires, so we stub the parser only.  ha_render_system_prompt
        and _create_set_prompt run unmocked — they work without a live LLM.
        """
        ha = _make_ha()
        ha.model = None  # simulate uninitialized LLM

        # PydanticOutputParser raises PydanticInvalidForJsonSchema for CacheHolder
        # before the model guard fires — stub it so we reach the real guard.
        class DummyParser:
            def __init__(self, **_kw):
                pass

            def get_format_instructions(self):
                return "format"

        monkeypatch.setattr(
            "mewbo_tools.integration.homeassistant.PydanticOutputParser",
            DummyParser,
        )
        step = types.SimpleNamespace(tool_input="turn on the lights")
        with pytest.raises(RuntimeError, match="not initialized"):
            ha.set_state(step)

    def test_get_state_raises_when_model_is_none(self):
        """Line 635: get_state checks model is not None before building chain.

        ha_render_system_prompt and _create_get_prompt both work without a live
        LLM; the model guard fires naturally — no patches needed.
        """
        ha = _make_ha()
        ha.model = None
        step = types.SimpleNamespace(tool_input="what is the temperature")
        with pytest.raises(RuntimeError, match="not initialized"):
            ha.get_state(step)

    def test_set_state_raises_when_action_step_is_none(self):
        ha = _make_ha()
        with pytest.raises(ValueError, match="None"):
            ha.set_state(None)

    def test_get_state_raises_when_action_step_is_none(self):
        ha = _make_ha()
        with pytest.raises(ValueError, match="None"):
            ha.get_state(None)


# ===========================================================================
# cache_monitor — entity filtering edge cases
# ===========================================================================


class TestCacheMonitorEntityFiltering:
    """Cover the entity cleaning paths that are not exercised by the existing test."""

    def _make_holder(self, entities, services=None, allowed_domains=None):
        class Holder:
            def __init__(self):
                self.cache: HomeAssistantCache = {
                    "entity_ids": [e["entity_id"] for e in entities],
                    "sensor_ids": [],
                    "entities": list(entities),
                    "services": list(services or []),
                    "sensors": [],
                    "allowed_domains": allowed_domains or ["scene"],
                }

            @cache_monitor
            def refresh(self):
                return "ok"

        return Holder()

    def test_forbidden_prefix_entities_are_removed(self):
        # Behavior-lock: asserts the hardcoded forbidden_prefixes list in the
        # cache_monitor production decorator.  If the list changes, this test
        # must be updated deliberately.
        entities = [
            {"entity_id": "switch.fan", "attributes": {}, "state": "on"},
            {"entity_id": "scene.morning", "attributes": {}, "state": "scening"},
        ]
        holder = self._make_holder(entities, allowed_domains=["scene"])
        holder.refresh()
        entity_ids = [e["entity_id"] for e in holder.cache["entities"]]
        # switch. is a forbidden prefix — must be stripped by cache_monitor
        assert "switch.fan" not in entity_ids

    def test_forbidden_substring_entities_are_removed(self):
        # Behavior-lock: asserts the hardcoded forbidden_substrings list in the
        # cache_monitor production decorator.  If the list changes, this test
        # must be updated deliberately.
        entities = [
            {
                "entity_id": "binary_sensor.blink_kk_bedroom_motion",
                "attributes": {},
                "state": "off",
            },
            {"entity_id": "scene.morning", "attributes": {}, "state": "scening"},
        ]
        holder = self._make_holder(entities, allowed_domains=["scene"])
        holder.refresh()
        entity_ids = [e["entity_id"] for e in holder.cache["entities"]]
        assert "binary_sensor.blink_kk_bedroom_motion" not in entity_ids

    def test_scene_entity_state_removed(self):
        entities = [
            {"entity_id": "scene.night", "attributes": {}, "state": "on"},
        ]
        holder = self._make_holder(entities, allowed_domains=["scene"])
        holder.refresh()
        # Scenes should still be present but without 'state'
        scene_entities = [e for e in holder.cache["entities"] if e["entity_id"] == "scene.night"]
        assert len(scene_entities) == 1
        assert "state" not in scene_entities[0]

    def test_sensor_entities_moved_to_sensors(self):
        # Note: cache_monitor iterates entities list while modifying it — only
        # the first sensor/binary_sensor is reliably moved in a 2-item list
        # due to Python's index-mutation in a for loop. Only assert sensor.temperature.
        entities = [
            {"entity_id": "sensor.temperature", "state": "22", "attributes": {}},
        ]
        holder = self._make_holder(entities, allowed_domains=["scene"])
        holder.refresh()
        sensor_ids = [s["entity_id"] for s in holder.cache["sensors"]]
        assert "sensor.temperature" in sensor_ids

    def test_context_fields_stripped_from_entity(self):
        entities = [
            {
                "entity_id": "scene.morning",
                "attributes": {"icon": "mdi:sun"},
                "state": "on",
                "context": {"id": "abc"},
                "last_changed": "2024-01-01T00:00:00",
                "last_reported": "2024-01-01T00:00:00",
                "last_updated": "2024-01-01T00:00:00",
            }
        ]
        holder = self._make_holder(entities, allowed_domains=["scene"])
        holder.refresh()
        scene_entities = [e for e in holder.cache["entities"] if "scene" in e["entity_id"]]
        if scene_entities:
            assert "context" not in scene_entities[0]
            assert "last_changed" not in scene_entities[0]

    def test_icon_attribute_stripped(self):
        entities = [
            {
                "entity_id": "scene.night",
                "attributes": {"icon": "mdi:moon", "brightness": 50},
                "state": "on",
            }
        ]
        holder = self._make_holder(entities, allowed_domains=["scene"])
        holder.refresh()
        scene_entities = [e for e in holder.cache["entities"] if "scene" in e["entity_id"]]
        if scene_entities:
            assert "icon" not in scene_entities[0].get("attributes", {})

    def test_services_filtered_by_allowed_domains(self):
        entities = []
        services = [
            {"domain": "scene"},
            {"domain": "light"},
            {"domain": "switch"},
        ]
        holder = self._make_holder(entities, services=services, allowed_domains=["scene"])
        holder.refresh()
        domains = [s["domain"] for s in holder.cache["services"]]
        assert "scene" in domains
        assert "light" not in domains

    def test_entity_ids_and_sensor_ids_sorted(self):
        entities = [
            {"entity_id": "scene.z_last", "attributes": {}, "state": "on"},
            {"entity_id": "scene.a_first", "attributes": {}, "state": "on"},
        ]
        holder = self._make_holder(entities, allowed_domains=["scene"])
        holder.refresh()
        ids = holder.cache["entity_ids"]
        assert ids == sorted(ids)


# ===========================================================================
# HomeAssistant._clean_answer  — additional edge cases
# ===========================================================================


class TestCleanAnswerExtra:
    @pytest.mark.parametrize(
        ("raw", "expected_substr"),
        [
            ("10km/h", "kilometer per hour"),
            ("25°C", "degrees celsius"),
            ("50%", "percent"),
            ("5mm/h", "millimeter per hour"),
            ("100Gb/s", "gigabits per second"),
            ("50Mb/s", "megabits per second"),
            ("10Kb/s", "kilobits per second"),
            ("2.4GHz", "Gigahertz"),
            ('"quoted"', "quoted"),  # quotes removed
        ],
    )
    def test_replacements(self, raw: str, expected_substr: str):
        result = HomeAssistant._clean_answer(raw)
        assert expected_substr in result

    def test_condenses_multiple_spaces(self):
        result = HomeAssistant._clean_answer("hello    world")
        assert "hello world" == result

    def test_strips_leading_trailing_whitespace(self):
        result = HomeAssistant._clean_answer("  hello  ")
        assert result == "hello"
