"""Constants for meeseeks_conversation."""
from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

NAME = "Meeseeks"
DOMAIN = "meeseeks_conversation"

MENU_OPTIONS = ["all_set"]
# MENU_OPTIONS = ["general_config", "model_config", "prompt_system"]

CONF_BASE_URL = "base_url"
CONF_API_KEY = "api_key"
CONF_TIMEOUT = "timeout"
CONF_MODEL = "chat_model"
CONF_CTX_SIZE = "ctx_size"
CONF_MAX_TOKENS = "max_tokens"
CONF_MIROSTAT_MODE = "mirostat_mode"
CONF_MIROSTAT_ETA = "mirostat_eta"
CONF_MIROSTAT_TAU = "mirostat_tau"
CONF_TEMPERATURE = "temperature"
CONF_REPEAT_PENALTY = "repeat_penalty"
CONF_TOP_K = "top_k"
CONF_TOP_P = "top_p"
CONF_PROMPT_SYSTEM = "prompt"

DEFAULT_BASE_URL = "http://meeseeks.server:5123"
DEFAULT_API_KEY = "msk-strong-password"
DEFAULT_TIMEOUT = 60
DEFAULT_MODEL = "llama2:latest"
DEFAULT_CTX_SIZE = 2048
DEFAULT_MAX_TOKENS = 128
DEFAULT_MIROSTAT_MODE = "0"
DEFAULT_MIROSTAT_ETA = 0.1
DEFAULT_MIROSTAT_TAU = 5.0
DEFAULT_TEMPERATURE = 0.8
DEFAULT_REPEAT_PENALTY = 1.1
DEFAULT_TOP_K = 40
DEFAULT_TOP_P = 0.9

DEFAULT_PROMPT_SYSTEM = ""
