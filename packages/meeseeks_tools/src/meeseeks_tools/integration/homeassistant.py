#!/usr/bin/env python3
"""Home Assistant integration tools and data models."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from typing import Any, Concatenate, ParamSpec, Protocol, TypedDict, TypeVar, runtime_checkable

import requests
from dotenv import load_dotenv
from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.messages.ai import AIMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate, HumanMessagePromptTemplate
from meeseeks_core.classes import AbstractTool, ActionStep
from meeseeks_core.common import MockSpeaker, get_logger, get_mock_speaker, ha_render_system_prompt
from pydantic.v1 import BaseModel, Field, validator
from typing_extensions import NotRequired

logging = get_logger(name="tools.integration.homeassistant")
load_dotenv()

# ! BUG: Error correction for model parsing errors is not implemented yet.
# !     Currently, if there are parsing errors, the tool is allowed to fail.
# TODO: Implement OutputFixingParser for error correction.

P = ParamSpec("P")
R = TypeVar("R")
SelfT = TypeVar("SelfT", bound="CacheHolder")


class HomeAssistantCache(TypedDict):
    """Cached Home Assistant entity and service metadata."""

    entity_ids: list[str]
    sensor_ids: list[str]
    entities: list[dict[str, Any]]
    services: list[dict[str, Any]]
    sensors: list[dict[str, Any]]
    allowed_domains: list[str]
    sensor: NotRequired[list[dict[str, Any]]]


@runtime_checkable
class CacheHolder(Protocol):
    """Protocol describing objects with a Home Assistant cache attribute.

    Attributes:
        cache: Home Assistant cache payload.
    """

    cache: HomeAssistantCache


class SupportsInvoke(Protocol):
    """Protocol for runnable chains that return HomeAssistantCall."""

    def invoke(self, input_data: dict[str, Any]) -> HomeAssistantCall:
        """Invoke the chain with structured input.

        Args:
            input_data: Input payload for the chain.

        Returns:
            Parsed HomeAssistantCall.
        """
        ...


def cache_monitor(func: Callable[Concatenate[SelfT, P], R]) -> Callable[Concatenate[SelfT, P], R]:
    """Decorator to monitor and update the cache.

    Args:
        func: Method that updates a portion of the cache.

    Returns:
        Wrapped function that normalizes cache contents after execution.
    """

    def sort_by_entity_id(dict_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Sort a list of entities by the entity_id field.

        Args:
            dict_list: List of entity dictionaries.

        Returns:
            Sorted list of entities.
        """
        return sorted(dict_list, key=lambda x: x["entity_id"])

    def clean_entities(
        self: CacheHolder,
        forbidden_prefixes: list[str],
        forbidden_substrings: list[str],
    ) -> HomeAssistantCache:
        """Filter and normalize entities while populating sensors.

        Args:
            self: Cache holder to mutate.
            forbidden_prefixes: Entity ID prefixes to exclude.
            forbidden_substrings: Entity ID substrings to exclude.

        Returns:
            Updated HomeAssistantCache payload.
        """
        for idx, entity in enumerate(self.cache["entities"]):
            if "context" in entity:
                self.cache["entities"][idx].pop("context")
                self.cache["entities"][idx].pop("last_changed")
                self.cache["entities"][idx].pop("last_reported")
                self.cache["entities"][idx].pop("last_updated")

            if "attributes" in entity:
                self.cache["entities"][idx]["attributes"].pop("icon", None)
                self.cache["entities"][idx]["attributes"].pop("monitor_cert_days_remaining", None)
                self.cache["entities"][idx]["attributes"].pop("monitor_cert_is_valid", None)
                self.cache["entities"][idx]["attributes"].pop("monitor_hostname", None)
                self.cache["entities"][idx]["attributes"].pop("monitor_port", None)

            if any(entity["entity_id"].startswith(prefix) for prefix in forbidden_prefixes):
                self.cache["entities"].remove(entity)

            if any(substring in entity["entity_id"] for substring in forbidden_substrings):
                self.cache["entities"].remove(entity)

            if entity["entity_id"].startswith("scene."):
                self.cache["entities"][idx].pop("state", None)

            if entity["entity_id"].startswith("sensor.") or entity["entity_id"].startswith(
                "binary_sensor."
            ):
                self.cache["sensors"].append(entity)
                self.cache["entities"].pop(idx)

        self.cache["entities"] = sort_by_entity_id(self.cache["entities"])
        self.cache["sensors"] = sort_by_entity_id(self.cache["sensors"])
        return self.cache

    def wrapper(self: SelfT, *args: P.args, **kwargs: P.kwargs) -> R:
        """Invoke the wrapped function and normalize cache content.

        Args:
            self: Cache holder instance.
            *args: Positional arguments forwarded to the wrapped function.
            **kwargs: Keyword arguments forwarded to the wrapped function.

        Returns:
            Result of the wrapped function.
        """
        result = func(self, *args, **kwargs)

        forbidden_prefixes = [
            "alarm_control_panel.",
            "automation.",
            "binary_sensor.remote_ui",
            "camera.",
            "climate",
            "conversation",
            "device_tracker.kraken_raspberry_pi_5",
            "media_player.axios",
            "media_player.axios_2",
            "media_player.chrome",
            "media_player.fire_tv_192_168_1_12",
            "person.",
            "remote.",
            "script.higher",
            "sensor.hacs",
            "sensor.hacs",
            "sensor.kraken_raspberry_pi_5_",
            "sensor.sonarr_commands",
            "sensor.sun",
            "sensor.uptimekuma_",
            "stt.",
            "sun.",
            "switch.",
            "switch.adam",
            "switch.bedroom_camera_camera_motion_detection",
            "tts.",
            "update.",
            "zone.home",
        ]
        forbidden_substrings = ["blink_kk_bedroom"]
        self.cache["sensor"] = []
        # Clean entities
        self.cache = clean_entities(self, forbidden_prefixes, forbidden_substrings)

        # Clean services
        self.cache["services"] = [
            service
            for service in self.cache["services"]
            if service["domain"] in self.cache["allowed_domains"]
        ]

        # Retrieve entity and sensor IDs
        self.cache["entity_ids"] = sorted(self.cache["entity_ids"])
        self.cache["sensor_ids"] = sorted(self.cache["sensor_ids"])

        logging.info(
            (
                "`{}` modified cache to <(len) Entity IDs: {}; (len) Entities: {}; "
                "(len) Sensors: {}; (len) Services: {};>"
            ),
            func.__name__,
            len(self.cache["entity_ids"]),
            len(self.cache["entities"]),
            len(self.cache["sensors"]),
            len(self.cache["services"]),
        )

        return result

    return wrapper


class HomeAssistantCall(BaseModel):
    """Structured Home Assistant service call extracted from the model output."""

    cache: CacheHolder | None = Field(alias="_ha_cache", default=None)
    domain: str = Field(
        description=("The category of the service to call, such as 'light', 'switch', or 'scene'.")
    )
    service: str = Field(
        description=(
            "The specific action to perform within the domain, such as 'turn_on', "
            "'turn_off', or 'set_temperature'."
        )
    )
    entity_id: str = Field(
        description=(
            "The ID of the specific device or entity within the domain to apply the "
            "service to, such as 'scene.heater'."
        )
    )

    @validator("entity_id", allow_reuse=True)
    # pylint: disable=E0213,W0613
    def validate_entity_id(cls, entity_id: str, values: dict[str, Any], **kwargs: Any) -> str:
        """Validate the entity_id against the cache when available.

        Args:
            cls: Pydantic model class.
            entity_id: Candidate entity identifier.
            values: Parsed model values.
            **kwargs: Additional validator arguments.

        Returns:
            Validated entity identifier.

        Raises:
            ValueError: If the entity ID is not found in the cache.
        """
        # ! BUG: The entity_id may not be validated correctly as the cache
        # !     is not passed to the validator.
        ha_cache = values.get("ha_cache")
        if ha_cache and entity_id not in ha_cache.cache["entity_ids"]:
            raise ValueError(f"Entity ID '{entity_id}' is not in the Home Assistant cache.")
        return entity_id

    @validator("domain", allow_reuse=True)
    # pylint: disable=E0213,W0613
    def validate_domain(cls, domain: str, values: dict[str, Any], **kwargs: Any) -> str:
        """Validate the domain against the cache when available.

        Args:
            cls: Pydantic model class.
            domain: Domain string to validate.
            values: Parsed model values.
            **kwargs: Additional validator arguments.

        Returns:
            Validated domain string.

        Raises:
            ValueError: If the domain is not found in the cache.
        """
        # ! BUG: The entity_id may not be validated correctly as the cache
        # !     is not passed to the validator.
        ha_cache = values.get("ha_cache")
        if ha_cache and domain not in ha_cache.cache["allowed_domains"]:
            raise ValueError(f"Domain '{domain}' is not in the Home Assistant cache.")
        return domain

    class Config:
        """Pydantic configuration for HomeAssistantCall."""

        arbitrary_types_allowed = True


class HomeAssistant(AbstractTool):
    """A service to manage and interact with Home Assistant."""

    def __init__(self) -> None:
        """Initialize the Home Assistant tool with environment defaults."""
        super().__init__(
            name="Home Assistant",
            description="A service to manage and interact with Home Assistant",
        )
        self.base_url = os.getenv("HA_URL", None)
        self._api_token = os.getenv("HA_TOKEN", None)
        self.cache: HomeAssistantCache = {
            "entity_ids": [],
            "sensor_ids": [],
            "entities": [],
            "services": [],
            "sensors": [],
            "allowed_domains": ["scene", "switch", "weather", "kodi", "automation"],
        }

        if not self.base_url or not self._api_token:
            raise ValueError("HA_URL and HA_TOKEN must be set in the environment.")

        self.api_headers: dict[str, str] = {
            "Authorization": f"Bearer {self._api_token}",
            "Content-Type": "application/json",
        }

    @cache_monitor
    def update_services(self) -> bool:
        """Update the list of services from Home Assistant.

        Returns:
            True when services are fetched successfully.
        """
        url = f"{self.base_url}/services"
        try:
            response = requests.get(url, headers=self.api_headers, timeout=30)
            status_code = getattr(response, "status_code", None)
            if status_code in {401, 403}:
                logging.error("Home Assistant authorization failed with status {}.", status_code)
                raise PermissionError("Home Assistant authorization failed.")
            response.raise_for_status()
            self.cache["services"] = response.json()
            self._save_json(self.cache["services"], "services.json")
            return True
        except requests.exceptions.RequestException as e:
            logging.error("Error: {}", e)
            return False

    @cache_monitor
    def update_entities(self) -> bool:
        """Update the list of entities from Home Assistant.

        Returns:
            True when entities are fetched successfully.
        """
        url = f"{self.base_url}/states"
        try:
            response = requests.get(url, headers=self.api_headers, timeout=30)
            status_code = getattr(response, "status_code", None)
            if status_code in {401, 403}:
                logging.error("Home Assistant authorization failed with status {}.", status_code)
                raise PermissionError("Home Assistant authorization failed.")
            response.raise_for_status()
            self.cache["entities"] = response.json()
            return True
        except requests.exceptions.RequestException as e:
            logging.error("Error: {}", e)
            return False

    @cache_monitor
    def update_entity_ids(self) -> bool:
        """Update the list of entity IDs from Home Assistant.

        Returns:
            True when entity IDs are populated.

        Raises:
            ValueError: If no entities are available for ID extraction.
        """
        # TODO: Always assumes blacklist by default due to cache_monitor.
        self.update_entities()
        entities = self.cache["entities"]
        if not entities:
            raise ValueError("No entities found while updating entity IDs.")
        self.cache["entity_ids"] = [entity["entity_id"] for entity in entities]
        logging.info("Entity IDs updated.")
        return True

    @cache_monitor
    def update_cache(self) -> None:
        """Update the entire cache.

        Raises:
            ValueError: If entity IDs cannot be derived.
        """
        self.update_entity_ids()
        self.update_services()
        self._save_json(self.cache["entities"], "entities.json")
        self._save_json(self.cache["sensors"], "sensors.json")

    def call_service(
        self,
        domain: str,
        service: str,
        entity_id: str,
        data: dict | None = None,
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Call a service in Home Assistant.

        Args:
            domain: Home Assistant domain name (e.g., "light").
            service: Service name within the domain (e.g., "turn_on").
            entity_id: Entity ID to target.
            data: Optional extra payload for the service call.

        Returns:
            Tuple of success flag and JSON response payload.

        Raises:
            ValueError: If the domain is not allowed.
        """
        if domain not in self.cache["allowed_domains"]:
            raise ValueError(f"Domain does not exist or blacklisted: {domain}")

        url = f"{self.base_url}/services/{domain}/{service}"
        payload = {"entity_id": entity_id}
        if data:
            payload.update(data)

        try:
            response = requests.post(url, headers=self.api_headers, json=payload, timeout=30)
            status_code = getattr(response, "status_code", None)
            if status_code in {401, 403}:
                logging.error("Home Assistant authorization failed with status {}.", status_code)
                raise PermissionError("Home Assistant authorization failed.")
            response.raise_for_status()
            logging.info(
                "Service <{}.{}> called on entity <{}> returned `{}`.",
                domain,
                service,
                entity_id,
                response.text,
            )
            return True, response.json()
        except requests.exceptions.RequestException as e:
            logging.error(
                "Unable to call service <{}.{}> on entity <{}>: {}", domain, service, entity_id, e
            )
            return False, []

    @staticmethod
    def _create_set_prompt(
        system_prompt: str,
        parser: PydanticOutputParser,
    ) -> ChatPromptTemplate:
        """Create the prompt template for a set-state operation.

        Args:
            system_prompt: System prompt content.
            parser: Pydantic output parser for HomeAssistantCall.

        Returns:
            ChatPromptTemplate configured for set-state tasks.
        """
        example = HomeAssistantCall(
            domain="scene", service="turn_on", entity_id="scene.lamp_power_on"
        )
        prompt = ChatPromptTemplate(
            messages=[
                SystemMessage(content=system_prompt),
                HumanMessage(content="Turn on the lamp lights."),
                AIMessage(content=example.json()),
                HumanMessagePromptTemplate.from_template(
                    "The user asked you to `{action_step}`. You must use the information "
                    "provided to pick the right Home Assistant service call values only "
                    "considering the current user query.\n\n"
                    "## Format Instructions\n{format_instructions}\n\n"
                    "## Home Assistant Entities and Domain-Services\n```\n{context}```\n"
                ),
            ],
            partial_variables={"format_instructions": parser.get_format_instructions()},
            input_variables=["action_step"],
        )
        return prompt

    @staticmethod
    def _create_get_prompt(system_prompt: str) -> ChatPromptTemplate:
        """Create the prompt template for a get-state operation.

        Args:
            system_prompt: System prompt content.

        Returns:
            ChatPromptTemplate configured for get-state tasks.
        """
        prompt = ChatPromptTemplate(
            messages=[
                SystemMessage(content=system_prompt),
                HumanMessage(content="How is the air quality today?"),
                AIMessage(
                    content=(
                        "AccuWeather reported today's air quality in your home as good. "
                        "This level of air quality ensures that the environment is healthy, "
                        "supporting your daily activities and wellbeing without any air "
                        "quality-related risks."
                    )
                ),
                HumanMessagePromptTemplate.from_template(
                    "The user asked you to `{action_step}`. You must use the sensor "
                    "information to answer the user's query. Keep your answer "
                    "analytical, brief and useful.\n\n"
                    "## Home Assistant Sensors\n```\n{context}```\n"
                ),
            ],
            input_variables=["action_step"],
        )
        return prompt

    @staticmethod
    def _clean_answer(answer: str) -> str:
        """Clean the answer by removing/replacing characters.

        Args:
            answer: Raw answer string to normalize.

        Returns:
            Cleaned answer string.
        """
        replacements = {
            # Common entities
            "RealFeel": "Real Feel",
            # Confident Abbreviations
            "km/h": " kilometer per hour",
            "°C": " degrees celsius",
            "%": " percent",
            "mm/h": " millimeter per hour",
            "Gb/s": " gigabits per second",
            "Mb/s": " megabits per second",
            "Kb/s": " kilobits per second",
            "GHz": "Gigahertz",
            # Formatting
            '"': "",
        }

        # Replace using the dictionary
        for old, new in replacements.items():
            answer = answer.replace(old, new)

        # Remove extra spaces and new lines, condense all multiple spaces
        #   to a single space
        answer = re.sub(r"\s+", " ", answer).strip()

        return answer

    def _invoke_service_and_set_state(
        self,
        chain: SupportsInvoke,
        rag_documents: list[Document],
        action_step: ActionStep,
    ) -> MockSpeaker:
        """Invoke the service and set the state.

        Args:
            chain: Runnable chain that yields HomeAssistantCall.
            rag_documents: Context documents for the chain.
            action_step: Action step describing the request.

        Returns:
            MockSpeaker with a status message.
        """
        MockSpeaker = get_mock_speaker()

        try:
            action_step_curr = str(action_step.action_argument).strip()
            call_service_values = chain.invoke(
                {"action_step": action_step_curr, "context": rag_documents, "cache": self.cache},
            )
            logging.debug(
                "Call Service Values for `{}`: `{}`", action_step_curr, call_service_values
            )
            status_bool, response_json = self.call_service(
                domain=call_service_values.domain,
                service=call_service_values.service,
                entity_id=call_service_values.entity_id,
            )
            if status_bool:
                tmp_return_message = f"Successfully called service: `{response_json}`"
            else:
                tmp_return_message = f"Failed to call service: `{response_json}`"
        except Exception as err_mesaage:
            logging.error("Error: {}", err_mesaage)
            tmp_return_message = f"I received an error - `{err_mesaage}`"
        return MockSpeaker(content=tmp_return_message)

    def set_state(self, action_step: ActionStep | None = None) -> MockSpeaker:
        """Predict and call a service for a given action step.

        Args:
            action_step: Action step describing the desired change.

        Returns:
            MockSpeaker with a status message.

        Raises:
            ValueError: If action_step is None.
        """
        if action_step is None:
            raise ValueError("Action step cannot be None.")
        self.update_cache()
        rag_documents = self._load_rag_documents(["entities.json", "services.json"])
        system_prompt = ha_render_system_prompt(
            name="homeassistant-set-state", all_entities=self.cache["entity_ids"]
        )

        parser = PydanticOutputParser(pydantic_object=HomeAssistantCall)  # type: ignore[type-var]
        prompt = self._create_set_prompt(system_prompt, parser)
        if self.model is None:
            raise RuntimeError("LLM client not initialized for Home Assistant.")
        model = self.model
        chain: Any = prompt | model | parser

        logging.info(
            "Invoking `set` action chain using `{}` for `{}`.", self.model_name, action_step
        )
        # TODO: Interpret the response from call service.
        return self._invoke_service_and_set_state(chain, rag_documents, action_step)

    def get_state(self, action_step: ActionStep | None = None) -> MockSpeaker:
        """Generate response for a given action step based on sensors.

        Args:
            action_step: Action step describing the desired query.

        Returns:
            MockSpeaker with the generated response.

        Raises:
            ValueError: If action_step is None.
        """
        if action_step is None:
            raise ValueError("Action step cannot be None.")
        self.update_cache()
        rag_documents = self._load_rag_documents(["sensors.json"])

        system_prompt = ha_render_system_prompt(name="homeassistant-get-state")

        prompt = self._create_get_prompt(system_prompt)
        if self.model is None:
            raise RuntimeError("LLM client not initialized for Home Assistant.")
        model = self.model
        chain: Any = prompt | model

        logging.info("Invoking `get` action chain using `{}`.", self.model_name)
        message = chain.invoke(
            {
                "action_step": str(action_step.action_argument).strip(),
                "context": rag_documents,
            },
        )
        cleaned_message = self._clean_answer(str(message.content))
        MockSpeaker = get_mock_speaker()
        return MockSpeaker(content=cleaned_message)
