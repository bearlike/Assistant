#!/usr/bin/env python3
"""Controllable model → tool-variant map (Gitea #113, Phase A).

The sibling of the prompt registry: where the registry layers a per-model prompt
DELTA, this module decides which *variant of a tool* a model gets. Today that is
the edit tool (``structured_patch`` vs ``search_replace_block``) — the seam that
already existed in-code as ``llm.model_prefers_structured_patch``'s hardcoded
model list. That list is migrated onto an OPERATOR-TUNABLE data file
(``prompts/model_variants.yaml``) loaded here through one validated atomic class,
so onboarding a model or flipping a preference is a data edit, not a code change.

Design rules (mirror ``prompt_registry.py``)
--------------------------------------------
- **Atomic class.** ``ModelVariantRegistry`` holds the parsed, validated map and
  exposes behaviour over it; the file schema is validated at definition
  (Pydantic, ``extra="forbid"``). DI: an in-memory map is injectable for tests.
- **Longest-prefix match.** ``match`` is a model-name prefix compared against the
  provider-stripped, lowercased model id; the longest matching prefix wins —
  identical semantics to the registry's ``kind: model`` overrides, so the two
  files PAIR by the same key (a model's tool variant + its prompt nudge share one
  prefix).
- **Sane built-in default.** A missing/unparseable file degrades to an empty map
  whose ``defaults.edit_tool`` is the conservative ``search_replace_block`` — the
  selector never crashes the loop. The shipped file carries the migrated
  defaults, and operators override it freely.
"""

from __future__ import annotations

from importlib import resources
from typing import Literal

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, model_validator

from mewbo_core.common import get_logger

logging = get_logger(name="core.model_variants")

# The two edit-tool variants the engine ships (see ``tool_registry`` /
# ``mewbo_tools.integration.{file_edit_tool,aider_edit_blocks}``).
EditToolVariant = Literal["structured_patch", "search_replace_block"]

# The data file's package location — sibling of the prompt ``registry/`` dir.
_DATA_PACKAGE = "mewbo_core"
_DATA_RELPATH = ("prompts", "model_variants.yaml")


def _strip_provider(model_name: str | None) -> str:
    """Drop a ``provider/`` prefix and lowercase (``openai/gpt-5`` → ``gpt-5``)."""
    if not model_name:
        return ""
    return model_name.split("/", 1)[-1].strip().lower()


# ---------------------------------------------------------------------------
# Schema (validate at definition — Pydantic, extra="forbid")
# ---------------------------------------------------------------------------


class ModelProfile(BaseModel):
    """One model-capability profile: a prefix and the tool variants it prefers.

    ``match`` is a model-name PREFIX (provider stripped, lowercased). ``edit_tool``
    is the edit-tool variant the model works best with. ``note`` is a free-text
    rationale (operator-facing; ignored at runtime).
    """

    model_config = ConfigDict(extra="forbid")

    match: str = Field(min_length=1)
    edit_tool: EditToolVariant
    note: str | None = None

    @model_validator(mode="after")
    def _normalize_match(self) -> ModelProfile:
        object.__setattr__(self, "match", self.match.strip().lower())
        if not self.match:
            raise ValueError("model profile 'match' must be a non-empty prefix")
        return self


class ModelVariantDefaults(BaseModel):
    """The fallback variants used when no profile matches the active model."""

    model_config = ConfigDict(extra="forbid")

    edit_tool: EditToolVariant = "search_replace_block"


class ModelVariantMap(BaseModel):
    """The whole ``model_variants.yaml`` document, validated at load."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    defaults: ModelVariantDefaults = Field(default_factory=ModelVariantDefaults)
    profiles: list[ModelProfile] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# The registry (atomic class)
# ---------------------------------------------------------------------------


class ModelVariantRegistry:
    """Resolve per-model tool variants from the controllable data file.

    State: the parsed :class:`ModelVariantMap`. Behaviour: longest-prefix lookup
    of a model's profile and its edit-tool variant, plus a definition-time
    ``validate_all`` lint gate.
    """

    def __init__(self, data: ModelVariantMap) -> None:
        """Build over a pre-parsed, validated map (DI for tests)."""
        self._data = data

    # -- construction --------------------------------------------------------

    @classmethod
    def from_package(cls) -> ModelVariantRegistry:
        """Load the shipped ``prompts/model_variants.yaml``.

        A missing or unparseable file is non-fatal: it degrades to an empty map
        (conservative ``search_replace_block`` default) so the edit-tool selector
        keeps working — the operator's edit can never break the loop.
        """
        try:
            handle = resources.files(_DATA_PACKAGE)
            for part in _DATA_RELPATH:
                handle = handle.joinpath(part)
            raw = yaml.safe_load(handle.read_text(encoding="utf-8")) or {}
            return cls(ModelVariantMap(**raw))
        except (ModuleNotFoundError, FileNotFoundError):
            logging.debug("model_variants.yaml not found; using built-in defaults.")
            return cls(ModelVariantMap())
        except Exception as exc:  # noqa: BLE001 — never crash the loop on bad data
            logging.warning("Failed to load model_variants.yaml: {}; using defaults.", exc)
            return cls(ModelVariantMap())

    # -- read ----------------------------------------------------------------

    def profile_for(self, model_name: str | None) -> ModelProfile | None:
        """Return the longest-prefix-matching profile for *model_name*, or None."""
        normalized = _strip_provider(model_name)
        if not normalized:
            return None
        best: ModelProfile | None = None
        for profile in self._data.profiles:
            if normalized.startswith(profile.match) and (
                best is None or len(profile.match) > len(best.match)
            ):
                best = profile
        return best

    def edit_tool_for(self, model_name: str | None) -> EditToolVariant:
        """Return the edit-tool variant for *model_name* (profile or default)."""
        profile = self.profile_for(model_name)
        return profile.edit_tool if profile is not None else self._data.defaults.edit_tool

    # -- validation (CI / definition-time gate) ------------------------------

    def validate_all(self) -> None:
        """Assert the map is internally consistent; raise on the first problem.

        Pydantic already enforces per-entry shape at load; this lints
        cross-entry invariants (no two profiles share an identical ``match``,
        which would make selection ambiguous for that exact prefix).
        """
        seen: set[str] = set()
        for profile in self._data.profiles:
            if profile.match in seen:
                raise ValueError(
                    f"model_variants: duplicate profile match {profile.match!r}"
                )
            seen.add(profile.match)


# ---------------------------------------------------------------------------
# Singleton accessor (mirrors get_prompt_registry)
# ---------------------------------------------------------------------------

_SINGLETON: ModelVariantRegistry | None = None


def get_model_variant_registry() -> ModelVariantRegistry:
    """Return the process-wide model-variant registry, built once from the file."""
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = ModelVariantRegistry.from_package()
    return _SINGLETON


def reset_model_variant_registry() -> None:
    """Drop the cached singleton (test isolation seam)."""
    global _SINGLETON
    _SINGLETON = None


__all__ = [
    "EditToolVariant",
    "ModelProfile",
    "ModelVariantDefaults",
    "ModelVariantMap",
    "ModelVariantRegistry",
    "get_model_variant_registry",
    "reset_model_variant_registry",
]
