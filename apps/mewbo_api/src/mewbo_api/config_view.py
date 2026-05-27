"""Cohesive, pure view over the ``AppConfig`` JSON schema.

``ConfigSchemaView`` is the single source of truth for how the ``/config``
endpoints treat sensitive fields. It classifies every field as either

* **protected** (``x-protected``) — never read, never written: stripped from
  the public schema and from value dumps, and rejected in PATCH payloads. Used
  for host paths and the API master token.
* **secret** (``x-secret``) — write-only: settable via PATCH but never read
  back. Kept in the public schema marked ``writeOnly: true``; its value is
  stripped from dumps; is-set status is reported separately.

A single traversal of the generated schema (walking ``$defs`` + ``$ref`` and
inline object properties) collects both sets of dot-paths up front; every
public method then operates on those precomputed sets. The class is pure (no
Flask/HTTP imports) and fully unit-testable.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

from mewbo_core.config import AppConfig
from pydantic import BaseModel

_PROTECTED_KEY = "x-protected"
_SECRET_KEY = "x-secret"


class ConfigSchemaView:
    """Cohesive view over the ``AppConfig`` JSON schema.

    Classifies fields as protected (never exposed) or secret (write-only), and
    serves the schema/value transforms the ``/config`` endpoints need. One
    traversal, dependency-injected with the generated schema.
    """

    def __init__(self, schema: dict) -> None:
        """Build the view from a generated JSON *schema* (one traversal)."""
        self._schema = schema
        self._defs: dict = schema.get("$defs", {})
        protected: set[str] = set()
        secret: set[str] = set()
        self._classify(schema, protected=protected, secret=secret, prefix="")
        self._protected = protected
        self._secret = secret

    @classmethod
    def from_model(cls, model: type[BaseModel] = AppConfig) -> ConfigSchemaView:
        """Build a view from a Pydantic model's generated JSON schema."""
        return cls(model.model_json_schema())

    # -- one traversal -----------------------------------------------------

    def _classify(
        self,
        schema: dict,
        *,
        protected: set[str],
        secret: set[str],
        prefix: str,
    ) -> None:
        """Walk *schema*'s properties, recording protected/secret dot-paths.

        Recurses through ``$ref`` (resolved against ``$defs``) and inline
        ``object`` definitions so nested sections are fully classified.
        """
        props = schema.get("properties", {})
        for name, prop in props.items():
            path = name if not prefix else f"{prefix}.{name}"
            if prop.get(_PROTECTED_KEY):
                protected.add(path)
            if prop.get(_SECRET_KEY):
                secret.add(path)
            ref = prop.get("$ref")
            if ref:
                ref_name = ref.rsplit("/", 1)[-1]
                target = self._defs.get(ref_name)
                if target is not None:
                    self._classify(target, protected=protected, secret=secret, prefix=path)
            if prop.get("type") == "object" and "properties" in prop:
                self._classify(prop, protected=protected, secret=secret, prefix=path)

    # -- classification accessors -----------------------------------------

    def protected_paths(self) -> set[str]:
        """Dot-paths of all ``x-protected`` fields."""
        return set(self._protected)

    def secret_paths(self) -> set[str]:
        """Dot-paths of all ``x-secret`` fields."""
        return set(self._secret)

    # -- schema transform --------------------------------------------------

    def public_schema(self) -> dict:
        """Return the schema with protected removed and secrets marked writeOnly.

        ``x-protected`` properties are REMOVED (and dropped from each def's
        ``required``); ``x-secret`` properties are KEPT but marked
        ``writeOnly: true``.
        """
        schema = deepcopy(self._schema)
        for def_schema in schema.get("$defs", {}).values():
            props = def_schema.get("properties")
            if not props:
                continue
            to_remove = [k for k, v in props.items() if v.get(_PROTECTED_KEY)]
            for key in to_remove:
                del props[key]
            for value in props.values():
                if value.get(_SECRET_KEY):
                    value["writeOnly"] = True
            req = def_schema.get("required")
            if req:
                def_schema["required"] = [r for r in req if r not in to_remove]
        return schema

    # -- value transforms --------------------------------------------------

    def strip_values(self, data: Mapping[str, object]) -> dict[str, object]:
        """Return a deep copy with BOTH protected and secret VALUES removed.

        Secrets are write-only — their values are never read back.
        """
        result = deepcopy(dict(data))
        self._strip(result, self._protected | self._secret, prefix="")
        return result

    def _strip(self, data: dict[str, object], paths: set[str], *, prefix: str) -> None:
        """Remove *paths* from *data* in place."""
        for key in list(data.keys()):
            path = key if not prefix else f"{prefix}.{key}"
            value = data[key]
            if path in paths:
                del data[key]
            elif isinstance(value, dict):
                self._strip(value, paths, prefix=path)

    def secret_status(self, cfg: Mapping[str, object]) -> dict[str, bool]:
        """Map each secret dot-path to whether *cfg* holds a non-empty value."""
        return {path: bool(self._lookup(cfg, path)) for path in self._secret}

    @staticmethod
    def _lookup(data: Mapping[str, object], path: str) -> object:
        """Return the value at dot-*path* in *data*, or ``None`` if absent."""
        node: object = data
        for part in path.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return None
            node = node[part]
        return node

    def reject_protected(self, patch: Mapping[str, object]) -> list[str]:
        """Return protected dot-paths present in a PATCH payload (caller 403s).

        ``x-secret`` paths are ALLOWED in patches.
        """
        violations: list[str] = []
        self._find(patch, self._protected, prefix="", out=violations)
        return violations

    def _find(
        self, patch: Mapping[str, object], paths: set[str], *, prefix: str, out: list[str]
    ) -> None:
        """Collect *paths* present in *patch* into *out*."""
        for key, value in patch.items():
            path = key if not prefix else f"{prefix}.{key}"
            if path in paths:
                out.append(path)
            elif isinstance(value, dict):
                self._find(value, paths, prefix=path, out=out)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        """Return a compact summary of the classification counts."""
        return (
            f"ConfigSchemaView(protected={len(self._protected)}, "
            f"secret={len(self._secret)})"
        )


__all__ = ["ConfigSchemaView"]
