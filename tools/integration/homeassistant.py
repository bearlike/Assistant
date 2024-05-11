#!/usr/bin/env python3
import os
import re
from collections import namedtuple
from typing import Optional, Tuple, List

import requests
from dotenv import load_dotenv
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.messages.ai import AIMessage
from langchain_core.pydantic_v1 import BaseModel, Field, validator
from langchain.output_parsers import PydanticOutputParser
from langchain.prompts import ChatPromptTemplate, HumanMessagePromptTemplate

from core.common import get_mock_speaker
from core.common import get_logger, ha_render_system_prompt
from core.classes import AbstractTool, ActionStep

logging = get_logger(name="tools.integration.homeassistant")
load_dotenv()

# ! BUG: Error correction for model parsing errors is not implemented yet.
# !     Currently, if there are parsing errors, the tool is allowed to fail.
# TODO: Implement OutputFixingParser for error correction.

def cache_monitor(func):
    """Decorator to monitor and update the cache."""
    @staticmethod
    def sort_by_entity_id(dict_list: List[dict]) -> List[dict]:
        return sorted(dict_list, key=lambda x: x['entity_id'])

    def clean_entities(self, forbidden_prefixes, forbidden_substrings):
        for idx, entity in enumerate(self.cache['entities']):
            if 'context' in entity:
                self.cache['entities'][idx].pop('context')
                self.cache['entities'][idx].pop('last_changed')
                self.cache['entities'][idx].pop('last_reported')
                self.cache['entities'][idx].pop('last_updated')

            if 'attributes' in entity:
                self.cache['entities'][idx]['attributes'].pop('icon', None)
                self.cache['entities'][idx]['attributes'].pop(
                    "monitor_cert_days_remaining", None)
                self.cache['entities'][idx]['attributes'].pop(
                    "monitor_cert_is_valid", None)
                self.cache['entities'][idx]['attributes'].pop(
                    "monitor_hostname", None)
                self.cache['entities'][idx]['attributes'].pop(
                    "monitor_port", None)

            if any(entity['entity_id'].startswith(prefix) for prefix in forbidden_prefixes):
                self.cache['entities'].remove(entity)

            if any(substring in entity['entity_id'] for substring in forbidden_substrings):
                self.cache['entities'].remove(entity)

            if entity['entity_id'].startswith('scene.'):
                self.cache['entities'][idx].pop('state', None)

            if entity['entity_id'].startswith('sensor.') or entity['entity_id'].startswith('binary_sensor.'):
                self.cache['sensors'].append(entity)
                self.cache['entities'].pop(idx)

        self.cache['entities'] = sort_by_entity_id(self.cache['entities'])
        self.cache['sensors'] = sort_by_entity_id(self.cache['sensors'])
        return self.cache

    def wrapper(self, *args, **kwargs):
        result = func(self, *args, **kwargs)

        forbidden_prefixes = [
            'alarm_control_panel.', 'automation.', 'binary_sensor.remote_ui',
            'camera.', 'climate', 'conversation', 'device_tracker.kraken_raspberry_pi_5',
            'media_player.axios', 'media_player.axios_2', 'media_player.chrome',
            'media_player.fire_tv_192_168_1_12', 'person.', 'remote.',
            'script.higher', 'sensor.hacs', 'sensor.hacs',
            'sensor.kraken_raspberry_pi_5_', 'sensor.sonarr_commands',
            'sensor.sun', 'sensor.uptimekuma_', 'stt.', 'sun.', 'switch.',
            'switch.adam', 'switch.bedroom_camera_camera_motion_detection',
            'tts.', 'update.', 'zone.home'
        ]
        forbidden_substrings = ['blink_kk_bedroom']
        self.cache["sensor"] = []
        # Clean entities
        self.cache = clean_entities(
            self, forbidden_prefixes, forbidden_substrings)

        # Clean services
        self.cache['services'] = [
            service for service in self.cache['services'] if service['domain'] in self.cache["allowed_domains"]
        ]

        # Retrieve entity and sensor IDs
        self.cache['entity_ids'] = sorted(self.cache['entity_ids'])
        self.cache['sensor_ids'] = sorted(self.cache['sensor_ids'])

        logging.info(
            "`%s` modified cache to <(len) Entity IDs: %s; (len) Entities: %s; (len) Sensors: %s; (len) Services: %s;>",
            func.__name__, len(self.cache['entity_ids']),
            len(self.cache['entities']), len(self.cache['sensors']),
            len(self.cache['services'])
        )

        return result
    return wrapper


class HomeAssistantCall(BaseModel):
    cache: Optional[dict] = Field(alias="_ha_cache", default={})
    domain: str = Field(
        description="The category of the service to call, such as 'light', 'switch', or 'scene'.")
    service: str = Field(
        description="The specific action to perform within the domain, such as 'turn_on', 'turn_off', or 'set_temperature'.")
    entity_id: str = Field(
        description="The ID of the specific device or entity within the domain to apply the service to, such as 'scene.heater'.")

    @validator("entity_id", allow_reuse=True)
    # pylint: disable=E0213,W0613
    def validate_entity_id(cls, entity_id, values, **kwargs):
        # ! BUG: The entity_id may not be validated correctly as the cache
        # !     is not passed to the validator.
        ha_cache = values.get("ha_cache")
        if ha_cache and entity_id not in ha_cache.cache["entity_ids"]:
            raise ValueError(
                f"Entity ID '{entity_id}' is not in the Home Assistant cache.")
        return entity_id

    @validator("domain", allow_reuse=True)
    # pylint: disable=E0213,W0613
    def validate_domain(cls, domain, values, **kwargs):
        # ! BUG: The entity_id may not be validated correctly as the cache
        # !     is not passed to the validator.
        ha_cache = values.get("ha_cache")
        if ha_cache and domain not in ha_cache.cache["allowed_domains"]:
            raise ValueError(
                f"Domain '{domain}' is not in the Home Assistant cache.")
        return domain


class HomeAssistant(AbstractTool):
    """A service to manage and interact with Home Assistant."""

    def __init__(self):
        super().__init__(
            name="Home Assistant",
            description="A service to manage and interact with Home Assistant"
        )
        self.base_url = os.getenv("HA_URL", None)
        self._api_token = os.getenv("HA_TOKEN", None)
        self.cache = {
            "entity_ids": [],
            "sensor_ids": [],
            "entities": [],
            "services": [],
            "sensors": [],
            "allowed_domains": [
                "scene", "switch", "weather", "kodi", "automation"]
        }

        if not self.base_url or not self._api_token:
            raise ValueError(
                "HA_URL and HA_TOKEN must be set in the environment.")

        self.api_headers = {
            "Authorization": f"Bearer {self._api_token}",
            "Content-Type": "application/json"
        }

    @cache_monitor
    def update_services(self):
        """Update the list of services from Home Assistant."""
        url = f"{self.base_url}/services"
        try:
            response = requests.get(url, headers=self.api_headers, timeout=30)
            response.raise_for_status()
            self.cache["services"] = response.json()
            self._save_json(self.cache["services"], "services.json")
            return True
        except requests.exceptions.RequestException as e:
            logging.error("Error: %s", e)
            return False

    @cache_monitor
    def update_entities(self):
        """Update the list of entities from Home Assistant."""
        url = f"{self.base_url}/states"
        try:
            response = requests.get(url, headers=self.api_headers, timeout=30)
            response.raise_for_status()
            self.cache["entities"] = response.json()
            return True
        except requests.exceptions.RequestException as e:
            logging.error("Error: %s", e)
            return False

    @cache_monitor
    def update_entity_ids(self):
        """Update the list of entity IDs from Home Assistant."""
        # TODO: Always assumes blacklist by default due to cache_monitor.
        self.update_entities()
        entities = self.cache["entities"]
        if not entities:
            raise ValueError("No entities found while updating entity IDs.")
        self.cache["entity_ids"] = [entity["entity_id"] for entity in entities]
        logging.info("Entity IDs updated.")
        return True

    @cache_monitor
    def update_cache(self):
        """Update the entire cache."""
        self.update_entity_ids()
        self.update_services()
        self._save_json(self.cache["entities"], "entities.json")
        self._save_json(self.cache["sensors"], "sensors.json")

    def call_service(self, domain: str, service: str, entity_id: str, data: Optional[dict] = None) -> Tuple[bool, list]:
        """Call a service in Home Assistant."""
        if domain not in self.cache["allowed_domains"]:
            raise ValueError(f"Domain does not exist or blacklisted: {domain}")

        url = f"{self.base_url}/services/{domain}/{service}"
        payload = {"entity_id": entity_id}
        if data:
            payload.update(data)

        try:
            response = requests.post(
                url, headers=self.api_headers, json=payload, timeout=30)
            response.raise_for_status()
            logging.info("Service <%s.%s> called on entity <%s> returned `%s`.",
                         domain, service, entity_id, response.text)
            return True, response.json()
        except requests.exceptions.RequestException as e:
            logging.error(
                "Unable to call service <%s.%s> on entity <%s>: %s", domain, service, entity_id, e)
            return False, []

    @staticmethod
    def _create_set_prompt(system_prompt: str, parser: PydanticOutputParser) -> ChatPromptTemplate:
        example = HomeAssistantCall(
            domain="scene", service="turn_on", entity_id="scene.lamp_power_on")
        prompt = ChatPromptTemplate(
            messages=[
                SystemMessage(content=system_prompt),
                HumanMessage(content="Turn on the lamp lights."),
                AIMessage(example.json()),
                HumanMessagePromptTemplate.from_template(
                    "The user asked you to `{action_step}`. You must use the information provided to pick the right Home Assistant service call values only considering the current user query.\n\n## Format Instructions\n{format_instructions}\n\n## Home Assistant Entities and Domain-Services\n```\n{context}```\n"
                ),
            ],
            partial_variables={
                "format_instructions": parser.get_format_instructions()},
            input_variables=["action_step"]
        )
        return prompt

    @staticmethod
    def _create_get_prompt(system_prompt: str) -> ChatPromptTemplate:
        prompt = ChatPromptTemplate(
            messages=[
                SystemMessage(content=system_prompt),
                HumanMessage(content="How is the air quality today?"),
                AIMessage(content="AccuWeather reported today's air quality in your home as good. This level of air quality ensures that the environment is healthy, supporting your daily activities and wellbeing without any air quality-related risks."),
                HumanMessagePromptTemplate.from_template(
                    "The user asked you to `{action_step}`. You must use the sensor information to answer the user's query. Keep your answer analytical, brief and useful.\n\n## Home Assistant Sensors\n```\n{context}```\n"
                ),
            ],
            input_variables=["action_step"]
        )
        return prompt

    @staticmethod
    def _clean_answer(answer: str) -> str:
        """Clean the answer by removing/replacing characters."""
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
            "\"": ""
        }

        # Replace using the dictionary
        for old, new in replacements.items():
            answer = answer.replace(old, new)

        # Remove extra spaces and new lines, condense all multiple spaces
        #   to a single space
        answer = re.sub(r'\s+', ' ', answer).strip()

        return answer

    def _invoke_service_and_set_state(
        self, chain: 'langchain_core.runnables.base.RunnableSequence',
        rag_documents: list,
        action_step: ActionStep
    ) -> namedtuple:
        """Invoke the service and set the state."""
        MockSpeaker = get_mock_speaker()

        try:
            action_step_curr = action_step.action_argument.strip()
            call_service_values = chain.invoke(
                {
                    "action_step": action_step_curr,
                    "context": rag_documents,
                    "cache": self.cache
                },
            )
            logging.debug(
                "Call Service Values for `%s`: `%s`",
                action_step_curr, call_service_values
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
            logging.error("Error: %s", err_mesaage)
            tmp_return_message = f"I received an error - `{err_mesaage}`"
        return MockSpeaker(content=tmp_return_message)

    def set_state(self, action_step: ActionStep) -> "MockSpeaker":
        """ Predict and call a service for a given action step based
                on sensor information.
        Args:
            action_step (ActionStep): The natural language action
                                        step to be performed.
        Returns:
            Response from the chain.
        """
        self.update_cache()
        rag_documents = self._load_rag_documents([
            "entities.json", "services.json"
        ])
        system_prompt = ha_render_system_prompt(
            name="homeassistant-set-state", all_entities=self.cache["entity_ids"])

        parser = PydanticOutputParser(pydantic_object=HomeAssistantCall)
        prompt = self._create_set_prompt(system_prompt, parser)
        chain = prompt | self.model | parser

        logging.info("Invoking `set` action chain using `%s` for `%s`.",
                     self.model_name, action_step)
        # TODO: Interpret the response from call service.
        return self._invoke_service_and_set_state(
            chain, rag_documents, action_step)

    def get_state(self, action_step: ActionStep) -> "MockSpeaker":
        """ Generate response for a given action step based
                on sensor information.
        Args:
            action_step (ActionStep): The natural language action
                                        step to be performed.
        Returns:
            Response from the chain.
        """
        self.update_cache()
        rag_documents = self._load_rag_documents(["sensors.json"])

        system_prompt = ha_render_system_prompt(name="homeassistant-get-state")

        prompt = self._create_get_prompt(system_prompt)
        chain = prompt | self.model

        logging.info("Invoking `get` action chain using `%s`.",
                     self.model_name)
        message = chain.invoke(
            {
                "action_step": action_step.action_argument.strip(),
                "context": rag_documents,
            },
        )
        # Remove newline characters in the message.content
        message.content = self._clean_answer(message.content)
        return message


def test_homeassistant():
    """Test the HomeAssistant class."""
    ha = HomeAssistant()
    ha.update_cache()
    ha.call_service("scene", "turn_on", "scene.strip_lights_white")
    return ha
