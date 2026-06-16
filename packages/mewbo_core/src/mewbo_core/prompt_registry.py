#!/usr/bin/env python3
"""Central prompt registry — one seam for every engine prompt.

Today the engine's prompts live in four shapes and three templating dialects with
no shared schema. This module is the single home for them: schema'd entries
(``PromptEntry``) loaded from YAML, rendered through one Jinja2 dialect, with a
layered override system so a *scenario*, a *model*, or a *mod* can replace or
extend any prompt **without touching orchestration code**.

Why this matters for cross-model alignment
-------------------------------------------
The orchestration loop has an intended behavioural contract (delegate, emit a
terminal response, respect budgets, call tools as structured calls). Models
diverge from that contract in model-specific ways. Instead of forking the loop
per model, every model renders the SAME base template plus a small, declared
delta that closes *its* divergence:

    render order  =  base template
                      → declarative override   (scenario > model-prefix; longest wins)
                      → programmatic modifiers  (mods registered down-only)

A new model is onboarded by adding DATA (a ``model`` override with
``mode: append``), not a code path. A mod extends behaviour by pushing a
``PromptModifier`` (mirrors ``plugins.register_builtin_root`` /
``capabilities.register_session_capability_provider``) — core never imports up.

Design rules
------------
- **Atomic class.** ``PromptRegistry`` holds state (entries + a Jinja2
  environment + a compiled-template cache) and exposes behaviour over it. DI:
  in-memory entries / modifiers are injectable for tests.
- **templated ⟺ ``variables`` is non-empty.** An entry with no declared
  variables is returned VERBATIM and is never Jinja-parsed — so a static prompt
  containing literal ``{`` / ``{{`` (JSON, XML, code) passes through untouched.
  A templated entry declares its variables; ``validate_all`` asserts the
  declared set equals the Jinja AST's undeclared names (validation at definition).
- **No implicit stripping.** ``render`` returns exactly the template rendered;
  callers that historically stripped (``get_system_prompt``) keep doing so in
  their shim. YAML authors pick the block-scalar style (``|`` / ``|-``) to match
  the original bytes; golden tests enforce byte-equality.
"""

from __future__ import annotations

import re
from importlib import resources
from importlib.abc import Traversable
from typing import Literal, Protocol, runtime_checkable

import yaml  # type: ignore[import-untyped]
from jinja2 import Environment, StrictUndefined, Template, meta as jinja_meta
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mewbo_core.common import get_logger

logging = get_logger(name="core.prompt_registry")

# A prompt id is a stable dotted slug: ``compact.system``, ``loop.depth.leaf``,
# ``file.system``. Lowercase, digits, dot/dash/underscore — never whitespace.
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.\-]*$")

# The package subdir (under each registered root) that holds the YAML registry
# files. Core's lives at ``mewbo_core/prompts/registry/*.yaml``.
_REGISTRY_SUBDIR = "registry"


def _traverse(root: Traversable, *parts: str) -> Traversable:
    """Chain single-segment ``joinpath`` (the 3.10 Traversable-typed signature)."""
    handle = root
    for part in parts:
        handle = handle.joinpath(part)
    return handle


# ---------------------------------------------------------------------------
# Schema (validate at definition — Pydantic, extra="forbid")
# ---------------------------------------------------------------------------


class PromptOverride(BaseModel):
    """A conditional replacement/extension of a base template.

    ``kind="scenario"`` matches a caller-named key exactly (e.g. ``"caveman"``);
    ``kind="model"`` matches a model-name PREFIX (e.g. ``"gemma-"``) — at render
    time the longest matching prefix wins. ``mode`` decides how the override's
    own template combines with the base: ``replace`` (the historical override),
    ``append`` / ``prepend`` (the additive per-model nudge that lets a model
    keep the shared contract and only patch its divergence).
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["model", "scenario"]
    match: str = Field(min_length=1)
    mode: Literal["replace", "append", "prepend"] = "replace"
    template: str


class PromptEntry(BaseModel):
    """One schema'd prompt: a stable id, a human purpose, and a template.

    Exactly one of ``template`` (inline) or ``template_file`` (a path relative to
    the owning root's ``prompts/`` dir, for ``system.txt``-sized standalone
    prompts) must be set. ``variables`` declares the template's inputs;
    ``token_budget`` is a free-text auditing note (e.g. "keep < 300 tok; injected
    every turn"). The entry is *templated* iff ``variables`` is non-empty.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    purpose: str = Field(min_length=1)
    template: str | None = None
    template_file: str | None = None
    variables: list[str] = Field(default_factory=list)
    overrides: list[PromptOverride] = Field(default_factory=list)
    token_budget: str | None = None

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not _ID_RE.match(value):
            raise ValueError(
                f"prompt id {value!r} must match {_ID_RE.pattern} "
                "(lowercase dotted slug)"
            )
        return value

    @model_validator(mode="after")
    def _exactly_one_source(self) -> PromptEntry:
        if (self.template is None) == (self.template_file is None):
            raise ValueError(
                f"prompt {self.id!r}: set exactly one of "
                "'template' or 'template_file'"
            )
        return self

    @property
    def is_templated(self) -> bool:
        """A prompt is rendered through Jinja2 iff it declares variables."""
        return bool(self.variables)


# ---------------------------------------------------------------------------
# Programmatic injection seam (down-only push — mirrors register_builtin_root)
# ---------------------------------------------------------------------------


class PromptContext(BaseModel):
    """What a modifier sees: the prompt being rendered and its render keys."""

    model_config = ConfigDict(frozen=True)

    prompt_id: str
    model: str | None = None
    scenario: str | None = None


@runtime_checkable
class PromptModifier(Protocol):
    """A mod that may transform a rendered prompt.

    The extensibility seam the product needs: a capability library or installed
    mod registers one of these to append/transform any prompt for a given model
    or scenario, WITHOUT core importing up or a new code seam per prompt. Applied
    after the declarative override, in registration order.
    """

    def matches(self, ctx: PromptContext) -> bool:
        """True if this modifier should run for *ctx*."""
        ...

    def apply(self, ctx: PromptContext, rendered: str) -> str:
        """Return the (possibly transformed) prompt text."""
        ...


# A library/app/mod above core in the DAG pushes here on import (idempotent on
# identity). Empty by default — a lean install transforms nothing.
_PROMPT_MODIFIERS: list[PromptModifier] = []
# Extra registry roots contributed by libraries above core (package, subdir).
_EXTRA_ROOTS: list[tuple[str, str]] = []
_SINGLETON: PromptRegistry | None = None


def register_prompt_modifier(modifier: PromptModifier) -> None:
    """Register a mod that may transform rendered prompts (down-only, idempotent)."""
    if modifier not in _PROMPT_MODIFIERS:
        _PROMPT_MODIFIERS.append(modifier)
        _invalidate_singleton()


def register_prompt_root(package: str, subdir: str = "prompts") -> None:
    """Register an extra registry root contributed by a library above core.

    Mirrors ``plugins.register_builtin_root``: the library pushes its
    ``<package>/<subdir>/registry/*.yaml`` so its prompts join the registry
    without core importing up to discover them. Call at import time, before the
    first ``get_prompt_registry()``.
    """
    pair = (package, subdir)
    if pair not in _EXTRA_ROOTS:
        _EXTRA_ROOTS.append(pair)
        _invalidate_singleton()


def reset_prompt_modifiers() -> None:
    """Drop all registered modifiers (test isolation seam)."""
    _PROMPT_MODIFIERS.clear()
    _invalidate_singleton()


def _invalidate_singleton() -> None:
    global _SINGLETON
    _SINGLETON = None


# ---------------------------------------------------------------------------
# The registry (atomic class)
# ---------------------------------------------------------------------------


class PromptRegistry:
    """Load, render, and validate the engine's schema'd prompts.

    State: the resolved ``entries`` (id → ``PromptEntry``), the root each entry
    came from (for ``template_file`` resolution), one Jinja2 ``Environment``
    (``StrictUndefined`` — a missing variable is a render error, never a silent
    blank), a compiled-template cache (prompts like depth-guidance render every
    step), and the active modifier list.
    """

    def __init__(
        self,
        entries: dict[str, PromptEntry],
        *,
        roots: dict[str, tuple[str, str]] | None = None,
        modifiers: list[PromptModifier] | None = None,
    ) -> None:
        """Build a registry over pre-resolved *entries* (DI: roots + modifiers)."""
        self._entries = entries
        # id → (package, subdir) so template_file resolves against its own root.
        self._roots = roots or {}
        self._modifiers = modifiers if modifiers is not None else _PROMPT_MODIFIERS
        self._env = Environment(
            undefined=StrictUndefined,
            keep_trailing_newline=True,
            autoescape=False,
        )
        self._compiled: dict[str, Template] = {}

    # -- construction --------------------------------------------------------

    @classmethod
    def from_package(cls) -> PromptRegistry:
        """Build from core's registry plus every root pushed by a library above."""
        entries: dict[str, PromptEntry] = {}
        roots: dict[str, tuple[str, str]] = {}
        for package, subdir in [("mewbo_core", "prompts"), *_EXTRA_ROOTS]:
            cls._load_root(package, subdir, entries, roots)
        return cls(entries, roots=roots)

    @classmethod
    def _load_root(
        cls,
        package: str,
        subdir: str,
        entries: dict[str, PromptEntry],
        roots: dict[str, tuple[str, str]],
    ) -> None:
        try:
            reg_dir = _traverse(resources.files(package), subdir, _REGISTRY_SUBDIR)
        except (ModuleNotFoundError, FileNotFoundError):
            return
        if not reg_dir.is_dir():
            return
        cls._load_dir(reg_dir, (package, subdir), entries, roots)

    @staticmethod
    def _load_dir(
        reg_dir: Traversable,
        owner: tuple[str, str],
        entries: dict[str, PromptEntry],
        roots: dict[str, tuple[str, str]],
    ) -> None:
        """Parse every ``*.yaml`` in *reg_dir* into *entries* (id uniqueness enforced)."""
        for handle in sorted(reg_dir.iterdir(), key=lambda h: h.name):
            if not handle.name.endswith((".yaml", ".yml")):
                continue
            data = yaml.safe_load(handle.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                raise ValueError(
                    f"prompt registry {handle.name!r} must be a mapping of "
                    f"id → entry, got {type(data).__name__}"
                )
            for pid, body in data.items():
                entry = PromptEntry(id=pid, **(body or {}))
                if entry.id in entries:
                    raise ValueError(
                        f"duplicate prompt id {entry.id!r} "
                        f"(redefined in {handle.name!r})"
                    )
                entries[entry.id] = entry
                roots[entry.id] = owner

    # -- read ----------------------------------------------------------------

    def get(self, prompt_id: str) -> PromptEntry:
        """Return the entry for *prompt_id* or raise ``KeyError``."""
        try:
            return self._entries[prompt_id]
        except KeyError:
            raise KeyError(f"unknown prompt id {prompt_id!r}") from None

    def has(self, prompt_id: str) -> bool:
        """True if *prompt_id* is registered."""
        return prompt_id in self._entries

    def list_ids(self) -> list[str]:
        """Every registered prompt id, sorted."""
        return sorted(self._entries)

    # -- render --------------------------------------------------------------

    def render(
        self,
        prompt_id: str,
        *,
        model: str | None = None,
        scenario: str | None = None,
        **variables: object,
    ) -> str:
        """Render *prompt_id*: base → declarative override → modifiers.

        ``model`` selects a model-prefix override (longest match wins); ``scenario``
        selects an exact scenario override (scenario wins over model). Extra
        ``variables`` bind the Jinja2 template (ignored for a static entry).
        """
        entry = self.get(prompt_id)
        override = self._select_override(entry, model=model, scenario=scenario)
        if override is not None and override.mode == "replace":
            text = self._render_source(override.template, entry.is_templated, variables)
        else:
            text = self._render_source(self._source_of(entry), entry.is_templated, variables)
            if override is not None:
                extra = self._render_source(
                    override.template, entry.is_templated, variables
                )
                text = extra + text if override.mode == "prepend" else text + extra
        ctx = PromptContext(prompt_id=prompt_id, model=model, scenario=scenario)
        for modifier in self._modifiers:
            if modifier.matches(ctx):
                text = modifier.apply(ctx, text)
        return text

    def _render_source(
        self, source: str, templated: bool, variables: dict[str, object]
    ) -> str:
        if not templated:
            return source
        compiled = self._compiled.get(source)
        if compiled is None:
            compiled = self._env.from_string(source)
            self._compiled[source] = compiled
        return compiled.render(**variables)

    @staticmethod
    def _select_override(
        entry: PromptEntry, *, model: str | None, scenario: str | None
    ) -> PromptOverride | None:
        if scenario is not None:
            for override in entry.overrides:
                if override.kind == "scenario" and override.match == scenario:
                    return override
        best: PromptOverride | None = None
        if model is not None:
            for override in entry.overrides:
                if (
                    override.kind == "model"
                    and model.startswith(override.match)
                    and (best is None or len(override.match) > len(best.match))
                ):
                    best = override
        return best

    # -- template_file resolution -------------------------------------------

    def _source_of(self, entry: PromptEntry) -> str:
        if entry.template is not None:
            return entry.template
        return self._read_file(entry.id, entry.template_file or "")

    def _read_file(self, prompt_id: str, relpath: str) -> str:
        package, subdir = self._roots.get(prompt_id, ("mewbo_core", "prompts"))
        handle = _traverse(resources.files(package), subdir, *relpath.split("/"))
        return handle.read_text(encoding="utf-8")

    # -- validation (CI / definition-time gate) ------------------------------

    def validate_all(self) -> None:
        """Assert every entry is well-formed; raise on the first problem.

        For each templated entry (and templated override), the template must
        parse as Jinja2 and its undeclared variables must equal the declared
        ``variables`` set — so a typo'd or missing declaration fails the test
        suite, not a live render. ``template_file`` targets must exist.
        """
        for prompt_id, entry in self._entries.items():
            source = self._source_of(entry)  # raises if template_file is missing
            declared = set(entry.variables)
            if entry.is_templated:
                used = self._undeclared(prompt_id, source)
                if used != declared:
                    raise ValueError(
                        f"prompt {prompt_id!r}: declared variables {sorted(declared)} "
                        f"!= template variables {sorted(used)}"
                    )
            for override in entry.overrides:
                if entry.is_templated:
                    # An override shares the entry's variable namespace; it may
                    # use a subset (e.g. a terse per-model variant).
                    used = self._undeclared(prompt_id, override.template)
                    if not used <= declared:
                        raise ValueError(
                            f"prompt {prompt_id!r} override {override.match!r}: "
                            f"uses undeclared variables {sorted(used - declared)}"
                        )

    def _undeclared(self, prompt_id: str, source: str) -> set[str]:
        try:
            ast = self._env.parse(source)
        except Exception as exc:  # noqa: BLE001 — surface WHICH prompt failed
            raise ValueError(
                f"prompt {prompt_id!r}: template does not parse as Jinja2: {exc}"
            ) from exc
        return jinja_meta.find_undeclared_variables(ast)


# ---------------------------------------------------------------------------
# Singleton accessor (mirrors config access)
# ---------------------------------------------------------------------------


def get_prompt_registry() -> PromptRegistry:
    """Return the process-wide registry, built once from the package + roots."""
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = PromptRegistry.from_package()
    return _SINGLETON


__all__ = [
    "PromptContext",
    "PromptEntry",
    "PromptModifier",
    "PromptOverride",
    "PromptRegistry",
    "get_prompt_registry",
    "register_prompt_modifier",
    "register_prompt_root",
    "reset_prompt_modifiers",
]
