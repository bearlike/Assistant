"""Configuration for the Mewbo MCP server.

All settings come from environment variables with sane defaults so the
server runs out-of-the-box against a local ``mewbo-api`` (default port
5124). No secrets are baked in — the caller's own Bearer token is what
authenticates every REST call (see ``rest.py``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar, TypeVar

# The REST API's own default bind port (see apps/mewbo_api/backend.py:main).
_DEFAULT_API_URL = "http://localhost:5124"

# Any exposure axis is an Enum whose members are the allowlist vocabulary.
_Axis = TypeVar("_Axis", bound=Enum)


class ToolGroup(str, Enum):
    """The tool families this facade can expose.

    Mirrors the atomic tool-group classes in :mod:`mewbo_mcp.tools`
    (``SessionTools`` / ``WikiTools`` / ``SearchTools`` / ``StructuredQueryTools``
    / ``IntegrationTools`` + ``ProjectTools`` / ``TriggerTools``) — the taxonomy
    already exists, so exposure keys off IT rather than inventing a second,
    drift-prone list of bare tool names.
    """

    SESSIONS = "sessions"
    WIKI = "wiki"
    SEARCH = "search"
    STRUCTURED = "structured"
    INTEGRATIONS = "integrations"
    TRIGGERS = "triggers"


class EffectTier(str, Enum):
    """What a tool DOES to the deployment — orthogonal to which subsystem it is in.

    The subsystem axis (:class:`ToolGroup`) alone cannot express "expose the
    wiki's READS but not its ASKS": reading a generated page and starting a Q&A
    run are the same *product*, and only the second one spends a model call.
    Cutting exposure along effect as well makes that policy sayable, and the two
    axes are conjunctive — a tool is exposed only if BOTH its group and its tier
    are allowed.

    Tiers are ordered by what they cost the deployment, cheapest first:

    - ``read`` — returns stored data. No model call, no run.
    - ``navigate`` — graph traversal over stored edges. No model call, no run.
    - ``ask`` — starts a model run (billable, and it can take minutes).
    - ``drive`` — creates, steers, or kills a session.
    """

    READ = "read"
    NAVIGATE = "navigate"
    ASK = "ask"
    DRIVE = "drive"


# The shipped exposure decision, one entry per group. EXHAUSTIVE BY CONSTRUCTION
# (see the check below): adding a `ToolGroup` without deciding its exposure is an
# import-time error, never a silent auto-expose. That is the whole point — the
# facade must not quietly widen as the project grows.
_DEFAULT_EXPOSURE: dict[ToolGroup, bool] = {
    # Driving a session is what an external agent comes here FOR — without this
    # group the facade is a read-only mirror of the REST API.
    ToolGroup.SESSIONS: True,
    ToolGroup.WIKI: True,
    # Agentic Search is a PRODUCT surface — the console and the REST API own it.
    # An external agent should not drive it through this facade.
    ToolGroup.SEARCH: False,
    # `structured_query` routes GRAPH-FIRST through a mapped Search workspace,
    # so exposing it re-opens the search surface by another door.
    ToolGroup.STRUCTURED: False,
    # Capability discovery describes THIS deployment's installed tools, plugins
    # and projects. A caller that needs it can be granted it; it is not part of
    # the minimum surface, so it is not shipped open.
    ToolGroup.INTEGRATIONS: False,
    # Triggers are armed in-session by an agent on ITSELF; the external
    # read/cancel counterpart is an operator surface, not an agent one.
    ToolGroup.TRIGGERS: False,
}

# The shipped decision on the EFFECT axis — same exhaustiveness contract. Every
# tier ships open: this axis exists so an operator CAN narrow the facade to,
# say, `read,navigate` (a wiki that answers no questions and starts no runs),
# not to narrow it by default. Widening a group is still the explicit act.
_DEFAULT_TIER_EXPOSURE: dict[EffectTier, bool] = {
    EffectTier.READ: True,
    EffectTier.NAVIGATE: True,
    EffectTier.ASK: True,
    EffectTier.DRIVE: True,
}

_EXPOSURE_AXES: tuple[tuple[dict[Any, bool], type[Enum]], ...] = (
    (_DEFAULT_EXPOSURE, ToolGroup),
    (_DEFAULT_TIER_EXPOSURE, EffectTier),
)

for _decided, _axis in _EXPOSURE_AXES:
    _undecided = sorted(m.value for m in _axis if m not in _decided)
    if _undecided:  # pragma: no cover — import-time guard
        raise RuntimeError(
            f"the default exposure map must decide every {_axis.__name__}; "
            f"missing: {', '.join(_undecided)}"
        )


@dataclass(frozen=True, slots=True)
class McpToolPolicy:
    """Which tools this MCP server exposes — the tool-exposure gate, on two axes.

    The policy is consulted AT REGISTRATION (``server._registrar``): a withheld
    group's tools are never handed to ``@mcp.tool()``, so FastMCP never builds
    their schema and they exist in neither ``list_tools`` nor dispatch. This is
    deliberately NOT a post-hoc prune — a tool that is never constructed cannot
    be forgotten about, and the gate sits at the line a future author actually
    edits (every registration must name its :class:`ToolGroup`, so a new tool
    cannot be added without an exposure decision).

    It is data + one predicate (:meth:`allows`); the registration itself is the
    server's job, because it touches FastMCP (a model never imports I/O).

    **A tool is exposed only if BOTH axes allow it** — its :class:`ToolGroup`
    (which subsystem) and its :class:`EffectTier` (what it does). Neither axis
    alone can express the policy this facade needs: the group axis cannot say
    "wiki reads but not wiki asks", and the tier axis cannot say "read the wiki
    but not the search product". Conjunction says both.

    **Search, structured query, integrations and triggers are withheld by
    DEFAULT** — see :data:`_DEFAULT_EXPOSURE`. Nothing here touches the engine:
    ``scg.enabled`` stays on and ``/api/agentic_search/*`` keeps serving the
    console, because MCP reaches search only over REST. Note the standing caveat
    from ``CLAUDE.md``: this facade is *curation, not a security boundary* — a
    key-holding caller can still reach the REST routes directly.

    Two env vars override, one per axis: ``MEWBO_MCP_EXPOSED_GROUPS`` and
    ``MEWBO_MCP_EXPOSED_TIERS``. Each is a comma-separated ALLOWLIST (fail-closed
    — naming a member is the only way to expose it), and an unknown name is a
    hard startup error, never a silent no-op.
    """

    exposed: frozenset[ToolGroup]
    tiers: frozenset[EffectTier]

    ENV_VAR: ClassVar[str] = "MEWBO_MCP_EXPOSED_GROUPS"
    TIER_ENV_VAR: ClassVar[str] = "MEWBO_MCP_EXPOSED_TIERS"

    @classmethod
    def default(cls) -> McpToolPolicy:
        """The shipped policy — the defaults decided on both axes."""
        return cls(
            exposed=frozenset(g for g, on in _DEFAULT_EXPOSURE.items() if on),
            tiers=frozenset(t for t, on in _DEFAULT_TIER_EXPOSURE.items() if on),
        )

    @classmethod
    def from_env(cls) -> McpToolPolicy:
        """Build the policy from the two allowlist env vars.

        Each axis resolves independently: unset ⇒ that axis's shipped default;
        set ⇒ EXACTLY the members named (empty ⇒ expose nothing on that axis), so
        widening the surface is always an explicit act.
        """
        shipped = cls.default()
        return cls(
            exposed=cls._allowlist(cls.ENV_VAR, ToolGroup, shipped.exposed),
            tiers=cls._allowlist(cls.TIER_ENV_VAR, EffectTier, shipped.tiers),
        )

    @classmethod
    def _allowlist(
        cls, env_var: str, axis: type[_Axis], default: frozenset[_Axis]
    ) -> frozenset[_Axis]:
        """Resolve one exposure axis from *env_var*, falling back to *default*."""
        raw = os.environ.get(env_var)
        if raw is None:
            return default
        names = [n.strip() for n in raw.split(",") if n.strip()]
        known = {m.value: m for m in axis}
        unknown = sorted(n for n in names if n not in known)
        if unknown:
            raise ValueError(
                f"{env_var} names unknown {axis.__name__} member(s): "
                f"{', '.join(unknown)} (known: {', '.join(sorted(known))})"
            )
        return frozenset(known[n] for n in names)

    def allows(self, group: ToolGroup, tier: EffectTier) -> bool:
        """Whether a tool in *group* with effect *tier* is exposed by this server.

        Both axes must allow it — see the class docstring for why conjunction is
        the operative rule rather than either axis on its own.
        """
        return group in self.exposed and tier in self.tiers


@dataclass(frozen=True, slots=True)
class McpConfig:
    """Resolved configuration for the MCP server process."""

    api_url: str
    host: str
    port: int
    tools: McpToolPolicy = field(default_factory=McpToolPolicy.default)

    @classmethod
    def from_env(cls) -> McpConfig:
        """Build the configuration from environment variables.

        - ``MEWBO_API_URL`` — base URL of the REST API (default
          ``http://localhost:5124``). A trailing slash is stripped so callers
          can join paths uniformly.
        - ``MEWBO_MCP_HOST`` — bind host for the MCP server (default
          ``127.0.0.1``).
        - ``MEWBO_MCP_PORT`` — bind port for the MCP server (default ``5127``).
          Deliberately not ``5125`` — that is the API's gunicorn port in Docker
          (``API_PORT=5125``), so sharing it would clash when both run together.
        - ``MEWBO_MCP_EXPOSED_GROUPS`` / ``MEWBO_MCP_EXPOSED_TIERS`` — the two
          axes of the tool-exposure gate; see :class:`McpToolPolicy`.
        """
        api_url = os.environ.get("MEWBO_API_URL", _DEFAULT_API_URL).rstrip("/")
        host = os.environ.get("MEWBO_MCP_HOST", "127.0.0.1")
        port = int(os.environ.get("MEWBO_MCP_PORT", "5127"))
        return cls(
            api_url=api_url, host=host, port=port, tools=McpToolPolicy.from_env()
        )


__all__ = ["EffectTier", "McpConfig", "McpToolPolicy", "ToolGroup"]
