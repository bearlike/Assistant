"""Constants for meeseeks_conversation."""

from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

NAME: str = "Meeseeks"
DOMAIN: str = "meeseeks_conversation"

MENU_OPTIONS: list[str] = ["all_set"]
# MENU_OPTIONS = ["general_config", "model_config", "prompt_system"]

CONF_BASE_URL: str = "base_url"
CONF_API_KEY: str = "api_key"
CONF_TIMEOUT: str = "timeout"
CONF_MODEL: str = "chat_model"
CONF_CTX_SIZE: str = "ctx_size"
CONF_MAX_TOKENS: str = "max_tokens"
CONF_MIROSTAT_MODE: str = "mirostat_mode"
CONF_MIROSTAT_ETA: str = "mirostat_eta"
CONF_MIROSTAT_TAU: str = "mirostat_tau"
CONF_TEMPERATURE: str = "temperature"
CONF_REPEAT_PENALTY: str = "repeat_penalty"
CONF_TOP_K: str = "top_k"
CONF_TOP_P: str = "top_p"
CONF_PROMPT_SYSTEM: str = "prompt"

DEFAULT_BASE_URL: str = "http://meeseeks.server:5123"
DEFAULT_API_KEY: str = "msk-strong-password"
DEFAULT_TIMEOUT: int = 60
DEFAULT_MODEL: str = "llama2:latest"
DEFAULT_CTX_SIZE: int = 2048
DEFAULT_MAX_TOKENS: int = 128
DEFAULT_MIROSTAT_MODE: str = "0"
DEFAULT_MIROSTAT_ETA: float = 0.1
DEFAULT_MIROSTAT_TAU: float = 5.0
DEFAULT_TEMPERATURE: float = 0.8
DEFAULT_REPEAT_PENALTY: float = 1.1
DEFAULT_TOP_K: int = 40
DEFAULT_TOP_P: float = 0.9

DEFAULT_PROMPT_SYSTEM: str = ""
