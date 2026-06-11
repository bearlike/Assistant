"""``ScgConfig`` — the single read-point for SCG config.

The whole SCG feature is opt-in behind ``scg.enabled`` (default ``False``); the
default search tier lives under the same ``scg`` namespace. Defaults are NOT
re-spelled here: ``get_config_value`` resolves through the typed
:class:`mewbo_core.config.ScgConfig` model, whose field defaults are the single
default source — a config file edit is **never required** (the model ships the
feature off with a sane tier).
"""

from __future__ import annotations

from mewbo_core.config import get_config_value


class ScgConfig:
    """Read-only accessor over the ``scg.*`` config namespace.

    All-staticmethod by design: there is no per-instance state — these are pure
    reads of the process config, so callers spell the *intent*
    (``ScgConfig.enabled()``) rather than a key path.
    """

    @staticmethod
    def enabled() -> bool:
        """Master gate — is the SCG feature turned on? (default ``False``)."""
        return bool(get_config_value("scg", "enabled"))

    @staticmethod
    def default_tier() -> str:
        """Default search tier budget knob (default ``"auto"``)."""
        return str(get_config_value("scg", "traversal", "default_tier"))

    @staticmethod
    def model_for_tier(tier: str | None) -> str | None:
        """The LLM a tier runs on, or ``None`` for the configured default.

        The tier is the run's single user-facing knob; it picks the brain
        (fast→nano-class, auto→sonnet-class, deep→frontier) alongside the
        decomposition/fan-out budget. ``None`` (blank mapping or unknown
        tier) defers to ``llm.default_model`` — never raises, so a bad tier
        string degrades to the default model rather than failing the run.
        """
        if not tier:
            return None
        try:
            value = get_config_value("scg", "traversal", "tier_models", tier.lower())
        except Exception:  # noqa: BLE001 — unknown tier key ⇒ default model
            return None
        text = str(value or "").strip()
        return text or None


__all__ = ["ScgConfig"]
