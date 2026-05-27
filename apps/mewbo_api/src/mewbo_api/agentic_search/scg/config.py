"""``ScgConfig`` — the single read-point for SCG config code-defaults.

The whole SCG feature is opt-in behind ``scg.enabled`` (default ``False``); the
traversal budget knobs and the map-source depth cap live under the same ``scg``
namespace the already-committed deterministic core reads (``map_job.py`` /
``orchestrated_runner.py`` / ``entity_resolution.py`` all key off ``scg.*``).

This atomic class centralizes those reads + their code-defaults in one place so
no module hand-spells a config key or a default twice (DRY). Every value falls
back to a spec-calibrated default, so a config file edit is **never required** —
the defaults ship the feature off, with sane traversal budgets when enabled.
"""

from __future__ import annotations

from mewbo_core.config import get_config_value

# Spec-calibrated code-defaults (#19 — "Implementation Plan v2"). The feature
# ships off; the traversal knobs are one budget surface over the single loop.
_DEFAULT_ENABLED = False
_DEFAULT_MAP_MAX_DEPTH = 3
_DEFAULT_BEAM_WIDTH = 3
_DEFAULT_TRAVERSAL_MAX_DEPTH = 4
_DEFAULT_TIER = "auto"


class ScgConfig:
    """Read-only accessor over the ``scg.*`` config namespace + its defaults.

    All-staticmethod by design: there is no per-instance state — these are pure
    reads of the process config with a baked-in default, so callers spell the
    *intent* (``ScgConfig.enabled()``) rather than a key path + magic default.
    """

    @staticmethod
    def enabled() -> bool:
        """Master gate — is the SCG feature turned on? (default ``False``)."""
        return bool(get_config_value("scg", "enabled", default=_DEFAULT_ENABLED))

    @staticmethod
    def map_max_depth() -> int:
        """Max introspection depth when mapping a source (default ``3``)."""
        return int(
            get_config_value("scg", "map_max_depth", default=_DEFAULT_MAP_MAX_DEPTH)
        )

    @staticmethod
    def beam_width() -> int:
        """Best-first traversal beam width (default ``3``)."""
        return int(
            get_config_value(
                "scg", "traversal", "beam_width", default=_DEFAULT_BEAM_WIDTH
            )
        )

    @staticmethod
    def traversal_max_depth() -> int:
        """Max traversal hop depth (default ``4``)."""
        return int(
            get_config_value(
                "scg", "traversal", "max_depth", default=_DEFAULT_TRAVERSAL_MAX_DEPTH
            )
        )

    @staticmethod
    def default_tier() -> str:
        """Default search tier budget knob (default ``"auto"``)."""
        return str(
            get_config_value(
                "scg", "traversal", "default_tier", default=_DEFAULT_TIER
            )
        )


__all__ = ["ScgConfig"]
