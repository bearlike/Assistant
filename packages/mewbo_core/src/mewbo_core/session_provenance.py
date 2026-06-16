"""Session provenance — who/what spawned a session.

A session record carries no origin field: every session is created the same
way regardless of caller (see ``session_store``). Provenance is therefore
reconstructed from two durable signals written at creation time:

* the session's **tags** (e.g. ``wiki:job:<id>``, ``agentic_search:scg:<id>``,
  ``nextcloud-talk:room:<token>``) — the robust signal, present for every
  internally-spawned session and surviving even when the context event is
  empty (older wiki jobs stored no capabilities);
* the first ``context`` event's ``client_capabilities`` / ``source_platform``
  — the fallback when a tag is absent.

``SessionOrigin`` is the single place that maps those signals to a coarse
origin. The console badges and filters the landing page on this value, so the
enum members are stable wire strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SessionOrigin(str, Enum):
    """Coarse provenance of a session, derived from its tags + context."""

    USER = "user"
    WIKI = "wiki"
    SEARCH = "search"
    CHANNEL = "channel"
    STRUCTURED = "structured"
    DRAFT = "draft"

    @classmethod
    def classify(cls, tags: list[str], context: dict[str, object]) -> SessionOrigin:
        """Map a session's tags + merged context to an origin.

        Tags win over context because they are the more reliable signal.
        Prefixes are ordered most- to least-specific; channel tags are matched
        by the ``:room:`` / ``:thread:`` infix shared by every channel adapter.
        """
        tag_prefixes = (
            ("wiki:", cls.WIKI),
            ("agentic_search:", cls.SEARCH),
            ("structured:", cls.STRUCTURED),
            ("draft:", cls.DRAFT),
        )
        for tag in tags:
            for prefix, origin in tag_prefixes:
                if tag.startswith(prefix):
                    return origin
            if ":room:" in tag or ":thread:" in tag:
                return cls.CHANNEL
        if context.get("source_platform"):
            return cls.CHANNEL
        capabilities = context.get("client_capabilities")
        if isinstance(capabilities, list):
            if "wiki" in capabilities:
                return cls.WIKI
            if "scg" in capabilities:
                return cls.SEARCH
        return cls.USER


@dataclass(frozen=True)
class TraceProvenance:
    """Filterable Langfuse trace identity derived from a session's durable signals.

    Pure classifier (no I/O), sibling to :class:`SessionOrigin`. A session is
    untraceable to filter today because the observability seam only ever sees
    ``session_id`` + ``source_platform``. Yet a session already *carries* its
    identity in three durable signals the orchestrator can read at run start:

    * its **tags** — product / workspace identity, e.g. ``wiki:job:<id>``,
      ``wiki:qa:<id>``, ``agentic_search:run:<id>``, ``agentic_search:scg:<id>``,
      ``<platform>:room:<chan>`` / ``<platform>:thread:<chan>:<thread>``,
      ``vcs:<owner/repo>:<kind>:<n>``;
    * its merged **context** — ``project`` (``managed:<uuid>`` ⇒ a worktree),
      ``repo``, ``branch``, ``model``, ``structured_workspace``,
      ``client_capabilities``, ``source_platform``;
    * the originating client **surface** — stamped by the entry point (CLI,
      console, api, mcp, channel, github/gitea, home-assistant).

    ``derive`` folds those into the ``tags`` + ``metadata`` that make traces
    filterable by product, workspace, project, repo, branch, worktree, origin,
    and surface. Keeping it a pure transform lets the seam
    (``components.langfuse_session_context``) stay taxonomy-free — it propagates
    whatever it is handed and never learns these prefixes.

    ``tags`` are the low-cardinality ``key:value`` filter chips (the existing tag
    convention used across the codebase); ``metadata`` is the superset, adding
    the high-cardinality fields (ids, worktree, capabilities) for structured
    filtering without exploding the tag list.
    """

    origin: SessionOrigin
    product: str
    session_type: str
    surface: str
    tags: tuple[str, ...]
    metadata: dict[str, str]

    # Facets promoted to filter chips, in a stable order. Everything lands in
    # ``metadata``; only these low-cardinality dimensions also become tags.
    _TAG_FACETS = (
        "origin",
        "product",
        "session_type",
        "surface",
        "project",
        "repo",
        "branch",
        "workspace",
        "model",
    )

    # Coarse product when no concrete tag refines it. ``vcs`` has no coarse
    # origin (it isn't one of the console's four), so it only ever arrives via a
    # ``vcs:`` tag override below.
    _ORIGIN_PRODUCT = {
        SessionOrigin.USER: "agent",
        SessionOrigin.WIKI: "wiki",
        SessionOrigin.SEARCH: "search",
        SessionOrigin.CHANNEL: "channel",
        SessionOrigin.STRUCTURED: "structured",
        SessionOrigin.DRAFT: "draft",
    }

    @classmethod
    def derive(
        cls,
        *,
        tags: list[str],
        context: dict[str, object],
        surface: str | None = None,
    ) -> TraceProvenance:
        """Fold a session's durable signals into trace tags + metadata."""
        origin = SessionOrigin.classify(tags, context)
        facets: dict[str, str] = {"origin": origin.value}

        # Context first, then tags: the tag-derived product / workspace / ids are
        # the more reliable signal, so they win on any overlapping key.
        facets.update(cls._facets_from_context(context))
        facets.update(cls._facets_from_tags(tags))

        facets.setdefault("product", cls._ORIGIN_PRODUCT.get(origin, "agent"))
        facets.setdefault(
            "session_type",
            "structured" if context.get("structured_workspace") else "chat",
        )
        facets["surface"] = cls._resolve_surface(surface, context, tags)

        tag_list = [f"{key}:{facets[key]}" for key in cls._TAG_FACETS if facets.get(key)]
        return cls(
            origin=origin,
            product=facets["product"],
            session_type=facets["session_type"],
            surface=facets["surface"],
            tags=tuple(tag_list),
            metadata=facets,
        )

    # -- signal extractors (pure, atomic) ----------------------------------

    @staticmethod
    def _facets_from_tags(tags: list[str]) -> dict[str, str]:
        """Map the most-specific session tag to product / type / workspace / ids.

        Returns on the first recognised tag; an unrecognised label (e.g. a manual
        ``/tag``) is skipped, so a custom label never masks the real product.
        """
        for tag in tags:
            parts = tag.split(":")
            head = parts[0]
            if head == "wiki" and len(parts) >= 3:
                kind = "wiki_index" if parts[1] == "job" else "wiki_qa"
                return {"product": "wiki", "session_type": kind, "wiki_id": parts[2]}
            if head == "agentic_search" and len(parts) >= 3:
                kind = "search_run" if parts[1] == "run" else "scg_map"
                return {"product": "search", "session_type": kind, "search_id": parts[2]}
            if head == "scg" and parts[1:2] == ["map"] and len(parts) >= 3:
                # The SCG MAP-source (indexing) job session is tagged
                # ``scg:map:<job_id>`` — distinct from a search RUN
                # (``agentic_search:run:``). Both are the ``search`` product, but
                # the auditor must tell a map apart from a run (#77).
                return {"product": "search", "session_type": "scg_map", "search_id": parts[2]}
            if head == "structured" and len(parts) >= 2:
                # ``structured:run`` (agentic /v1/structured) and ``structured:fast``
                # (its no-loop ``mode:"synthesis"`` lane, #85) share the
                # ``structured`` product; the second segment is the session_type so
                # the two execution strategies stay distinguishable in a trace filter.
                return {"product": "structured", "session_type": f"structured_{parts[1]}"}
            if head == "draft" and len(parts) >= 2:
                # ``draft:stream`` — token-streaming /v1/draft/stream.
                return {"product": "draft", "session_type": f"draft_{parts[1]}"}
            if head == "vcs" and len(parts) >= 4:
                return {
                    "product": "vcs",
                    "session_type": "vcs_pickup",
                    "repo": parts[1],  # owner/repo
                    "vcs_kind": parts[2],  # issue | pull_request
                    "vcs_number": parts[3],
                }
            if parts[1:2] in (["room"], ["thread"]):
                out = {
                    "product": "channel",
                    "session_type": "channel_msg",
                    "platform": head,
                }
                if len(parts) >= 3:
                    out["channel_id"] = parts[2]
                if parts[1] == "thread" and len(parts) >= 4:
                    out["thread_id"] = parts[3]
                return out
        return {}

    @classmethod
    def _facets_from_context(cls, context: dict[str, object]) -> dict[str, str]:
        """Pull project / repo / branch / worktree / workspace / model / caps.

        A ``project`` of the form ``managed:<uuid>`` is an ephemeral worktree, not
        a named project — surface it as ``worktree`` (its ``repo`` / ``branch``
        arrive via sibling context) so the high-cardinality uuid never becomes a
        ``project`` filter chip.
        """
        out: dict[str, str] = {}
        project = cls._as_str(context.get("project"))
        if project.startswith("managed:"):
            out["worktree"] = project.split(":", 1)[1]
        elif project:
            out["project"] = project
        for key in ("repo", "branch", "model"):
            value = cls._as_str(context.get(key))
            if value:
                out[key] = value
        workspace = cls._as_str(context.get("structured_workspace")) or cls._as_str(
            context.get("workspace")
        )
        if workspace:
            out["workspace"] = workspace
        capabilities = context.get("client_capabilities")
        if isinstance(capabilities, list) and capabilities:
            out["capabilities"] = ",".join(str(cap) for cap in capabilities)
        return out

    @classmethod
    def _resolve_surface(
        cls, surface: str | None, context: dict[str, object], tags: list[str]
    ) -> str:
        """Pick the client surface, most-reliable signal first.

        Explicit param (stamped by the entry point) > context ``source_platform``
        (channels) > forge inferred from a ``vcs:`` tag > ``unknown`` — the latter
        keeps an un-stamped path *visible* as a filter rather than silently
        untagged.
        """
        explicit = (surface or "").strip()
        if explicit:
            return explicit
        platform = cls._as_str(context.get("source_platform")).strip()
        if platform:
            return platform
        if any(tag.startswith("vcs:") for tag in tags):
            return "vcs"
        return "unknown"

    @staticmethod
    def _as_str(value: object) -> str:
        """Coerce a context value to a non-empty string, or ``""``."""
        return value if isinstance(value, str) else ""
