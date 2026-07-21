"""Domain contracts for Mewbo Apps — the single source of truth (spec §3).

Every other module in this sub-product (``store.py``, ``routes.py``,
``lifecycle.py``, the agent-side plugin) imports FROM here; nothing here
imports them. Two families, mirroring the house convention
(``agentic_search/schemas.py``, ``mewbo_core.triggers.spec``):

* **Manifest models** — :class:`AppSpec` (the durable, versioned app
  manifest) and its parts (:class:`WorkspaceRef`, :class:`CollectionSpec`,
  :class:`PipelineSpec`, :class:`AppPolicies`, :class:`AppFrontend`), plus
  :class:`AppVersion` (an append-only snapshot).
* **Runtime/data models** — :class:`PipelineRun` (the ingestion ledger),
  :class:`AppDataDoc` (one stored document), :class:`AppReadToken` (the
  short-lived render-scoped token), and the two wire event payloads
  (:class:`AppReadyEvent`, :class:`AppUpdatedEvent`).

Every model is ``ConfigDict(extra="forbid")`` and validated at definition.
Behavior intrinsic to the data lives ON the model (the strategy-on-model
rule): :meth:`AppSpec.transition`, the :class:`PipelineRun` ledger
(``open``/``record_write``/``close``/``freshness``), and
:meth:`AppReadToken.is_valid`. Per the trigger-domain precedent
(``TriggerSpec`` — clock/webhook/CI-payload always a method arg), **every
one of these takes the current time as an explicit ``now`` argument** —
models never read the wall clock to make a behavioral decision (a
``Field(default_factory=...)`` timestamp *stamp* at construction is the one
exception, mirroring ``TriggerSpec.created_at``: it is data, not a
decision). Tests inject a fixed ``NOW`` rather than patching a clock.

## The ``AppSpec.status`` graph

The spec (§3) fixes the six-member ``AppStatus`` vocabulary but doesn't spell
out which moves are legal — filled in here from the flows in §5 and the
honesty requirement in §6 ("app status honest: `broken` visible, never
silent"). ``archived`` is the one absorbing terminal (mirrors
``TriggerSpec``'s terminal statuses): once archived, nothing transitions out.
Every other status can archive directly.

```
draft    -> building, archived    (build starts / cancelled pre-build)
building -> live, broken, archived    (submit_app succeeds / build fails / cancelled)
live     -> paused, broken, archived    (user pauses / pipeline failure / archived)
paused   -> live, archived    (user resumes / archived)
broken   -> live, paused, archived    (repair succeeds / user pauses to investigate / archived)
archived -> (nothing — absorbing)
```

``draft -> live`` and ``draft -> broken``/``paused`` are deliberately NOT
direct edges: the flow in §5.1 always routes a fresh app through an active
``building`` window (the console's "live build progress" view) before it can
go live or be judged broken. If ``lifecycle.py`` finds this graph too strict
for a real flow, the fix is to extend ``_ALLOWED_TRANSITIONS`` here — never
to bypass ``transition()`` with a bare ``self.status = ...`` assignment
elsewhere.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

import jsonschema
from croniter import croniter  # type: ignore[import-untyped]  # no stubs published
from mewbo_core.triggers.spec import CronTrigger, TimeAtTrigger, TriggerSpec
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Literal vocabularies
# ---------------------------------------------------------------------------

AppStatus = Literal["draft", "building", "live", "paused", "broken", "archived"]
WorkspaceRefKind = Literal["own", "shared"]
PipelineFailurePolicy = Literal["repair", "pause", "notify"]
PipelineRunStatus = Literal["running", "succeeded", "failed"]
AppVersionAuthor = Literal["builder", "repair", "user"]
AppReadTokenScope = Literal["read", "write"]
# Per-pipeline submit-time verification verdict (see ``AppLifecycle.submit``'s
# verifier). ``pass`` — a code pipeline dry-ran cleanly; ``fail`` — it raised (a
# ``fail`` never actually PERSISTS: a failed dry run refuses the submit before the
# version row is written, so this member exists for honesty if that refusal ever
# softens); ``skipped`` — an agentic pipeline (no model call in a verifier), an
# unwired runner, or a code pipeline whose ``params_schema`` requires params a
# ``params={}`` smoke can't supply.
PipelineVerdict = Literal["pass", "fail", "skipped"]

# See the module docstring ("The AppSpec.status graph") for the rationale.
_ALLOWED_TRANSITIONS: dict[AppStatus, frozenset[AppStatus]] = {
    "draft": frozenset({"building", "archived"}),
    "building": frozenset({"live", "broken", "archived"}),
    "live": frozenset({"paused", "broken", "archived"}),
    "paused": frozenset({"live", "archived"}),
    "broken": frozenset({"live", "paused", "archived"}),
    "archived": frozenset(),
}

_ARCHIVED: AppStatus = "archived"

# Slug format shared by every user-authored identifier in this package
# (``CollectionSpec.name`` / ``PipelineSpec.name``): lowercase, 1-64 chars,
# starts/ends alphanumeric, `-`/`_` allowed in the middle.
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")

# The vetted binaries a ``mode="code"`` pipeline may declare in `allow_exec`.
# Extending this set is a deliberate, reviewed platform change, not a per-app
# choice — a pipeline cannot smuggle in an unvetted binary via any spelling.
#
# NOT a security boundary, and the distinction matters: the runner does not
# interpret subcommand semantics (no `push`-vs-`log` distinction, exactly like
# the agentic path's `aider_shell_tool`), and `git` in particular can be driven
# to run other programs through its own configuration. A declared binary
# therefore runs under the SAME trust envelope as the pipeline's own code. What
# this set bounds is ACCIDENT, and what it records is INTENT — declare only
# what a deterministic sync genuinely needs.
PIPELINE_ALLOWED_EXEC: frozenset[str] = frozenset({"git", "tea", "gh"})

# The git subcommands a pipeline may run, and the options it may not. Without
# this pair, declaring `git` is equivalent to granting a shell — `git -c
# alias.x='!sh -c …' x` executes, as do `core.pager`, `protocol.ext`/`ext::`,
# `--upload-pack` and `--exec-path`. That matters more here than the "same trust
# envelope as the pipeline's own code" argument admits, because the GET
# pipeline-invoke route is reachable by a browser holding a short-lived app READ
# token, whereas `aider_shell_tool` needs an agent session behind the master key.
#
# The SUBCOMMAND list is load-bearing on its own: banning the flags alone still
# leaves `git config alias.x '!sh'` followed by `git x` — two ordinary calls.
# Both halves are read-shaped, which is what the CLI-plumbed sync flows need
# (`git log`, `git diff`, `tea issues list`); a pipeline needing something else
# is refused until this list grows, which is the intended cost.
_GIT_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "log", "diff", "show", "status", "rev-parse", "ls-remote", "ls-files",
        "describe", "for-each-ref", "cat-file", "rev-list", "shortlog", "fetch",
    }
)
_GIT_BANNED_FLAGS: tuple[str, ...] = (
    "-c", "--config-env", "--exec-path", "--upload-pack", "--receive-pack",
    "--git-dir", "--work-tree",
)

# A bare hostname (no scheme, no path, no port) — what `allow_egress` declares.
# Rejecting a URL-shaped entry AT DEFINITION is what stops a declaration that
# would otherwise parse fine and then silently never match at runtime.
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$"
)

# git's scp-style remote, ``[user@]host:path`` — the user part is OPTIONAL, and
# that is the whole reason this regex exists rather than an ``@`` membership
# test: `git ls-remote example.com:repo.git` reaches a remote with no `@`
# anywhere in the token, so an `@`-only check leaves the egress gate wide open.
# Anchored, and the host class excludes `/` so a path (`origin/main:file`) or a
# flag (`--pretty=format:%H`) can never present as a host. The host must contain
# a DOT: without that, every ordinary colon-bearing refspec (`HEAD:refs/heads/x`,
# `main:main`, `HEAD:README.md`) reads as a host and is refused, whose only
# workaround is declaring `head` in `allow_egress` — which trains authors to list
# junk hosts, a worse habit than the narrow gap it closes. Accepted cost: a
# DOTLESS host (`localhost:repo`) is not gated by this branch.
_SCP_REMOTE_RE = re.compile(
    r"^(?:[^/@:]+@)?(?P<host>[A-Za-z0-9][A-Za-z0-9._-]*\.[A-Za-z0-9-]+):(?!//)"
)

# A host inside a ``scheme://`` URL, matched ANYWHERE in a token rather than at
# its start. Deliberately NOT ``urlparse``: `-c remote.x.url=https://host/r.git`
# has no legal scheme (``remote.x.url=https`` contains ``=``), so ``urlparse``
# returns hostname ``None`` and the token gets skipped — failing OPEN on a
# single-invocation reach to an arbitrary remote. ``finditer``, not one match:
# ``url.https://a/.insteadOf=https://b/`` carries two hosts.
_URL_HOST_RE = re.compile(r"://(?:[^/@\s]*@)?(?P<host>[A-Za-z0-9._-]+)")


def _utc_now() -> datetime:
    """Default-factory stamp for creation timestamps.

    A DATA default, not a behavioral clock read — see the module
    docstring's ``now`` rule.
    """
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Shared base — extra="forbid" + the slug validator every domain model reuses
# ---------------------------------------------------------------------------


class _AppsModel(BaseModel):
    """Shared base for every Mewbo Apps contract: strict, no unknown keys.

    Slug + aware-datetime validation live here as shared staticmethods
    (mirrors ``TriggerSpec._validate_owner_repo``/``_require_aware``) so
    every field across this module's models that needs the same format
    check reuses ONE implementation instead of drifting per-model.
    """

    model_config = ConfigDict(extra="forbid")

    @staticmethod
    def _validate_slug(value: str, *, field: str) -> str:
        if not _SLUG_RE.match(value):
            raise ValueError(
                f"{field} must be a lowercase slug (letters, digits, '-', '_'; "
                f"1-64 chars, alnum start/end), got {value!r}"
            )
        return value

    @staticmethod
    def _require_aware(value: datetime) -> datetime:
        """Reject a tz-naive datetime rather than silently assuming a zone.

        Same rationale as ``TriggerSpec._require_aware``: an agent- or
        client-authored ISO string with no UTC offset parses naive, and
        comparing it against an aware ``now`` later (``transition``,
        ``is_valid``, ``freshness``) raises ``TypeError`` deep in a
        request/trigger path instead of failing validation up front.
        """
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "datetime must include a timezone offset (e.g. Z or +05:30), "
                f"got {value.isoformat()!r}"
            )
        return value

    @staticmethod
    def _validate_relative_path(path: str) -> str:
        """Reject an absolute path, a Windows drive path, or any ``.``/``..`` segment.

        Shared by :class:`AppFrontend`'s file-map validator — the builder
        agent writes every frontend file under this manifest, so a
        traversal segment here is a sandbox escape onto the host
        filesystem, not merely a malformed path.
        """
        if not path or path.startswith("/") or path.startswith("\\"):
            raise ValueError(f"file path must be relative, got {path!r}")
        if ":" in path:
            raise ValueError(f"file path must not contain ':', got {path!r}")
        segments = path.replace("\\", "/").split("/")
        if any(seg in ("", ".", "..") for seg in segments):
            raise ValueError(
                f"file path must not contain '.', '..', or empty segments, got {path!r}"
            )
        return path


# ---------------------------------------------------------------------------
# Manifest parts
# ---------------------------------------------------------------------------


class WorkspaceRef(_AppsModel):
    """Which session-project/workspace primitive the app's agents anchor to.

    Points at the SAME workspace primitive sessions already use (spec §2.3)
    — this is a reference, never a new workspace-like entity. The frontend
    never inherits workspace capability; this powers the agent side only.
    """

    kind: WorkspaceRefKind
    key: str


class CollectionSpec(_AppsModel):
    """One named, schema-validated document collection in the app's data plane."""

    name: str
    json_schema: dict[str, Any]
    description: str = ""

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return cls._validate_slug(value, field="name")

    def validate_doc(self, doc: Mapping[str, Any]) -> None:
        """Validate *doc* against this collection's declared JSON Schema.

        Raises ``jsonschema.ValidationError`` on mismatch (the
        ``structured_response.py`` convention — callers catch that type
        specifically, not a bare ``ValueError``). The ``app_data``
        SessionTool (workstream B) is the intended caller: this keeps
        schema-validation behavior on the model that owns the schema
        instead of the tool re-deriving it.
        """
        jsonschema.validate(instance=dict(doc), schema=self.json_schema)


class _PipelineSchedule(_AppsModel):
    """Base for the declared-schedule discriminated union (strategy-on-model).

    A pipeline DECLARES how it is woken; the platform arms it (Phase 1).
    Each concrete kind owns its own field validator AND its
    :meth:`to_trigger_spec` strategy — there is deliberately no service-side
    ``if kind ==`` in the lifecycle, mirroring ``TriggerSpec`` itself (which is
    exactly what these build). Not itself a union member: the concrete kinds
    below declare the ``kind`` Literal a ``Field(discriminator="kind")`` parses on.
    """

    kind: str

    def to_trigger_spec(
        self, *, wake_prompt: str, session_id: str, now: datetime
    ) -> TriggerSpec:
        """Build the core :class:`TriggerSpec` the lifecycle arms on *session_id*.

        Returns a concrete trigger (``created_by="user"`` — the platform arms it
        on the maintainer, not the agent), stamped ``created_at=now`` so cron
        anchoring is deterministic. NOW arrives as an argument, never read from a
        wall clock, and the trigger STORE stays out of the model — the lifecycle
        owns persistence/expiry. Each concrete kind overrides this.
        """
        raise NotImplementedError  # pragma: no cover - concrete kinds override


class CronSchedule(_PipelineSchedule):
    """A repeating cron wake (5-field expression, evaluated in UTC)."""

    kind: Literal["time.cron"] = "time.cron"
    cron: str

    @field_validator("cron")
    @classmethod
    def _validate_cron(cls, value: str) -> str:
        # Validated at DEFINITION (like ``CronTrigger``), so a malformed schedule
        # fails the submit boundary with an agent-actionable error rather than
        # deep in the arm path.
        if not croniter.is_valid(value):
            raise ValueError(f"invalid cron expression: {value!r}")
        return value

    def to_trigger_spec(
        self, *, wake_prompt: str, session_id: str, now: datetime
    ) -> TriggerSpec:
        """Build the repeating :class:`CronTrigger` this schedule describes."""
        return CronTrigger(
            session_id=session_id,
            wake_prompt=wake_prompt,
            cron=self.cron,
            created_by="user",
            created_at=now,
        )


class AtSchedule(_PipelineSchedule):
    """A one-shot wake at a specific aware instant (fires once, then completes)."""

    kind: Literal["time.at"] = "time.at"
    at: datetime

    @field_validator("at")
    @classmethod
    def _validate_at(cls, value: datetime) -> datetime:
        return cls._require_aware(value)

    def to_trigger_spec(
        self, *, wake_prompt: str, session_id: str, now: datetime
    ) -> TriggerSpec:
        """Build the one-shot :class:`TimeAtTrigger` this schedule describes."""
        return TimeAtTrigger(
            session_id=session_id,
            wake_prompt=wake_prompt,
            at=self.at,
            created_by="user",
            created_at=now,
        )


# The declared-schedule union: parsed by ``kind`` exactly like ``TriggerUnion``.
# The builder's ``submit_app`` tool dumps its own (flat) schedule arg with
# ``exclude_none``, so the unused ``cron``/``at`` sibling is dropped and the
# right member validates cleanly here — the two shapes stay decoupled by design.
PipelineSchedule = Annotated[CronSchedule | AtSchedule, Field(discriminator="kind")]


class PipelineSpec(_AppsModel):
    """One agent-authored data pipeline: a DECLARED wake + tool scope.

    A pipeline declares HOW it is woken — a ``schedule`` (the platform arms a
    trigger for it on the maintainer at submit) or ``on_demand`` (no armed wake;
    runs only from a repair or a manual re-invocation).

    ``trigger_ref`` is PLATFORM-OWNED OUTPUT (Phase 1): the builder never
    sets it. :meth:`AppLifecycle.submit` STAMPS it with the id of the trigger it
    arms from ``schedule``. The one carve-out is the LEGACY flow where a
    builder hand-armed its own trigger and passed the id here — the lifecycle
    still re-homes it (deprecated), which is why a builder-supplied ``trigger_ref``
    also satisfies the floor below.

    The floor invariant makes the silent-unscheduled state (no schedule, not
    on-demand, no legacy ref — a pipeline that would never run) UNREPRESENTABLE at
    the submit trust boundary.
    """

    name: str
    wake_prompt: str
    schedule: PipelineSchedule | None = None
    on_demand: bool = False
    trigger_ref: str | None = None
    tools_allowlist: list[str] = Field(default_factory=list)
    cursor: dict[str, Any] = Field(default_factory=dict)
    # -- Phase 2: materialized (code) pipelines --------------------------
    # ``agentic`` (default, backwards-compatible) is woken by re-engaging the
    # maintainer LLM session; ``code`` runs a deterministic ``entrypoint`` file
    # through ``AppPipelineRunner`` — NO LLM call — at fire time and on demand.
    mode: Literal["agentic", "code"] = "agentic"
    # A bundle-relative key into ``AppSpec.frontend.files`` naming the pipeline's
    # Python file (``def run(params, ctx) -> Any``). REQUIRED iff ``mode=="code"``
    # (the iff enforced below); the lifecycle additionally checks it resolves to a
    # real bundle file at submit (``AppLifecycle._validate_code_pipelines``).
    entrypoint: str | None = None
    # Optional JSON Schema the invocation ``params`` are validated against before
    # ``run`` executes (``None`` ⇒ the pipeline accepts no params). Validated as a
    # well-formed schema AT DEFINITION so a malformed one fails the submit boundary.
    params_schema: dict[str, Any] | None = None
    # Wall-clock ceiling (seconds) for ONE ``run(params, ctx)`` execution — the
    # bound the runner's watchdog joins the pipeline's worker thread on (declared,
    # never inferred). A deterministic transform finishes well under the ``10``
    # default; a pipeline that calls ``ctx.llm`` should raise it (e.g. ``120``-
    # ``300``) so the model round-trip(s) have headroom. Capped at 600s.
    timeout_seconds: int = Field(default=10, ge=1, le=600)
    # The declare-don't-infer v1 of the freshness contract: seconds a
    # successful result may be served from the runner's process-local cache instead
    # of re-executing. ``0`` (default) = never cache. Applies to ``cache_mode="ttl"``
    # only — ``"source"`` (below) is stat-driven and IGNORES this value.
    cache_ttl_seconds: int = Field(default=0, ge=0)
    # -- Wave 5: the LIVE cache tier -------------------------------------
    # ``"ttl"`` (default) serves a successful result for ``cache_ttl_seconds``.
    # ``"source"`` is the read-through liveness tier: the runner records every
    # file/glob the run read and serves the cached result only while their stat
    # fingerprint (path, mtime_ns, size + the glob result set) is unchanged,
    # recomputing on ANY source change — so an invoke reflects current source state
    # yet computes only on change. ``"source"`` IGNORES ``cache_ttl_seconds``
    # (precedence: liveness is stat-driven, not time-driven); the runner enforces
    # both. Backwards-compatible: a legacy pipeline dict parses as ``"ttl"``.
    cache_mode: Literal["ttl", "source"] = "ttl"
    # -- Wave 5: the bounded LLM step (``ctx.llm``) ----------------------
    # Per-RUN cap on the cumulative ``max_tokens`` a run's ``ctx.llm()`` calls may
    # request. ``0`` (default) FORBIDS ``ctx.llm`` entirely — a pipeline must DECLARE
    # a positive budget to reach the model (declared data over ambient permission).
    # The runner refuses any call that would push the cumulative request past this.
    #
    # HONEST v1 LIMITATION (documented, not silently pretended): this is a
    # REQUESTED-token budget, not a metered-spend budget. It UNDER-counts real
    # usage two ways — a schema-reask retries the model (a second round-trip the
    # budget never charged), and ``max_tokens`` is NOT threaded into the synthesis
    # round-trip (it only drives this accounting), so a call can emit more than it
    # "requested". The real spend bound in v1 is therefore ``timeout_seconds`` plus
    # the CALL COUNT this budget caps, not a token meter. A metered budget is a
    # phase-2 candidate; see apps CLAUDE.md → "ctx.llm".
    llm_budget_tokens: int = Field(default=0, ge=0)
    # Whether an end-user render token (not only the builder/maintainer) may invoke
    # this pipeline over the write-scoped REST surface. Consumed by ``routes.py``
    # (the token-auth stream); NO behavior on the model or runner side.
    user_writable: bool = False
    # -- Controlled, allowlisted CLI/network egress ----------------------
    # Opt-in, closed by default (``[]`` ⇒ ``ctx.exec`` refuses every call), the
    # same banned-by-omission posture ``PIPELINE_ALLOWED_MODULES`` takes for
    # imports. Every entry must be one of :data:`PIPELINE_ALLOWED_EXEC`, so a
    # pipeline declares WHICH of the vetted set it needs and can never expand
    # the set itself.
    allow_exec: list[str] = Field(default_factory=list)
    # Bare hostnames (``git.example.com``, ``github.com`` — no scheme, no path)
    # ``ctx.exec`` may reach. Checked against every remote-shaped argv token, so
    # a declared binary still cannot reach an undeclared host. Empty (default)
    # permits host-less invocations only (``git log`` in the workspace).
    allow_egress: list[str] = Field(default_factory=list)

    @field_validator("allow_exec")
    @classmethod
    def _validate_allow_exec(cls, value: list[str]) -> list[str]:
        """Every declared binary must be in the platform's vetted set (see module top)."""
        unknown = sorted(set(value) - PIPELINE_ALLOWED_EXEC)
        if unknown:
            raise ValueError(
                f"allow_exec contains unvetted binaries {unknown} — only "
                f"{sorted(PIPELINE_ALLOWED_EXEC)} may be declared"
            )
        return value

    @field_validator("allow_egress")
    @classmethod
    def _validate_allow_egress(cls, value: list[str]) -> list[str]:
        """Every declared entry must be a bare, lowercased hostname — never a URL."""
        out: list[str] = []
        for host in value:
            if not isinstance(host, str) or not _HOSTNAME_RE.match(host):
                raise ValueError(
                    f"allow_egress entries must be bare hostnames (no scheme/path/port), "
                    f"got {host!r}"
                )
            out.append(host.lower())
        return out

    @staticmethod
    def hosts_in_argv(argv: Sequence[str]) -> set[str]:
        """Every host *argv* can reach — a ``scheme://host/…`` URL or ``[user@]host:path``.

        A pure read of the tokens, so the egress rule stays testable without a
        process. Anything else (a flag, a subcommand, a local path) yields no
        host, which is why a plain ``git log`` needs no ``allow_egress`` entry.
        """
        hosts: set[str] = set()
        for token in argv:
            if "://" in token:
                hosts.update(m.group("host").lower() for m in _URL_HOST_RE.finditer(token))
                continue
            scp = _SCP_REMOTE_RE.match(token)
            if scp:
                hosts.add(scp.group("host").lower())
        return hosts

    def check_exec_allowed(self, argv: Sequence[str]) -> None:
        """Refuse *argv* unless this pipeline declared its binary AND every host it reaches.

        The ONE authorization rule for ``ctx.exec``, living on the model that
        DECLARES ``allow_exec``/``allow_egress`` rather than in the runner that
        spawns — argv arrives as a method ARG, so the rule never reaches for a
        process or a clock (the ``TriggerSpec`` discipline). Raises
        :class:`ValueError`; the runner maps it onto its own error envelope.

        No re-check against :data:`PIPELINE_ALLOWED_EXEC` here: the field
        validator already made an unvetted entry unrepresentable, and a second
        copy of that rule is one that can drift.
        """
        # A bare ``str`` IS a ``Sequence[str]``, so `exec("git log")` would otherwise
        # sail past the element check and read ``argv[0]`` as the letter "g".
        if isinstance(argv, (str, bytes)) or not argv:
            raise ValueError("exec(argv, ...) requires a non-empty list of strings")
        if not all(isinstance(token, str) for token in argv):
            raise ValueError("exec(argv, ...) requires a non-empty list of strings")
        binary = argv[0]
        if binary not in self.allow_exec:
            raise ValueError(
                f"pipeline {self.name!r} did not declare {binary!r} in `allow_exec` "
                f"(declared: {sorted(self.allow_exec) or 'none'})"
            )
        undeclared = sorted(self.hosts_in_argv(argv) - set(self.allow_egress))
        if undeclared:
            raise ValueError(
                f"pipeline {self.name!r} did not declare host(s) {undeclared} in "
                f"`allow_egress` (declared: {sorted(self.allow_egress) or 'none'})"
            )
        if binary == "git":
            self._check_git_shape(argv)

    @staticmethod
    def _check_git_shape(argv: Sequence[str]) -> None:
        """Keep a `git` call read-shaped — see :data:`_GIT_SUBCOMMANDS` for why.

        Checked in argv ORDER so the first offending token is the one reported:
        a banned option can appear before OR after the subcommand, and the
        subcommand is the first token that is not an option.
        """
        subcommand: str | None = None
        for token in argv[1:]:
            if token in _GIT_BANNED_FLAGS or any(
                token.startswith(f"{flag}=") for flag in _GIT_BANNED_FLAGS
            ):
                raise ValueError(
                    f"`git {token}` is not permitted from a pipeline — it can redirect git "
                    "to run another program"
                )
            if "::" in token:
                # `ext::sh -c …` / `protocol.ext` transport: a remote that IS a command.
                raise ValueError(f"git argument {token!r} names a transport that runs a command")
            if subcommand is None and not token.startswith("-"):
                subcommand = token
        if subcommand is None:
            raise ValueError("exec(['git', …]) needs a subcommand")
        if subcommand not in _GIT_SUBCOMMANDS:
            raise ValueError(
                f"git subcommand {subcommand!r} is not permitted from a pipeline "
                f"(permitted: {sorted(_GIT_SUBCOMMANDS)})"
            )

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return cls._validate_slug(value, field="name")

    @field_validator("entrypoint")
    @classmethod
    def _validate_entrypoint(cls, value: str | None) -> str | None:
        """A code entrypoint is a relative, traversal-free bundle path (or ``None``)."""
        return cls._validate_relative_path(value) if value is not None else value

    @field_validator("params_schema")
    @classmethod
    def _validate_params_schema(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        """Reject a malformed JSON Schema at definition (the trust boundary).

        A bad ``params_schema`` would otherwise surface as a ``SchemaError`` deep
        in the runner's param check on every invocation; validating it here means
        the submit fails with an agent-actionable reask instead. ``validator_for``
        honours an explicit ``$schema`` and falls back to the latest draft.
        """
        if value is None:
            return value
        from jsonschema.validators import validator_for  # noqa: PLC0415 - local, edge dep

        try:
            validator_for(value).check_schema(value)
        except jsonschema.exceptions.SchemaError as exc:
            raise ValueError(f"params_schema is not a valid JSON Schema: {exc.message}") from exc
        return value

    @model_validator(mode="after")
    def _validate_code_mode(self) -> PipelineSpec:
        """Enforce ``entrypoint`` present IFF ``mode=="code"`` (matches the builder tool).

        ``mode=="code"`` with no entrypoint has nothing to execute; a
        ``mode=="agentic"`` pipeline that sets one has a file that would never run.
        Both states are made unrepresentable at definition, mirroring the builder's
        ``submit_app`` ``SubmitPipelineArgs`` validator so the two shapes stay in
        lockstep (the spec is dumped from the tool through ``model_validate`` here).
        """
        if self.mode == "code" and not self.entrypoint:
            raise ValueError(
                f"pipeline {self.name!r} is mode='code' but declares no `entrypoint` — "
                "name the bundle-relative Python file exposing `def run(params, ctx)`"
            )
        if self.mode == "agentic" and self.entrypoint is not None:
            raise ValueError(
                f"pipeline {self.name!r} is mode='agentic' but sets `entrypoint` — "
                "`entrypoint` applies only to mode='code'"
            )
        return self

    @model_validator(mode="after")
    def _validate_user_writable_requires_code(self) -> PipelineSpec:
        """Reject ``user_writable`` on a non-``code`` pipeline — history-safe (see below).

        ``user_writable=True`` is what makes a write-scoped render token mintable so
        the served frontend can POST this pipeline's params — but only a
        ``mode="code"`` pipeline executes synchronously on that POST, so a
        ``user_writable`` agentic pipeline mints a write credential that can never
        invoke it (an unusable-token trap). ``SubmitPipelineArgs`` enforces the same
        floor at the tool boundary; this makes the invalid pair unrepresentable on
        the durable model too, so no path (chat-builder, rollback replay, a
        hand-built spec) can persist it.

        HISTORY-SAFE — why a parse-time ``model_validator`` is legal here where
        ``ensure_wakeable`` had to stay OFF the model: ``user_writable`` is NEW this
        wave with default ``False``, so NO stored ``AppVersion`` snapshot can hold
        ``user_writable=True``. Every historical pipeline dict parses
        ``user_writable=False``, which this admits unconditionally — the invariant
        never existed to be violated by old data, so validating it at parse cannot
        500 a detail read of any pre-wave app (the ``ensure_wakeable`` trap).
        """
        if self.user_writable and self.mode != "code":
            raise ValueError(
                f"pipeline {self.name!r} sets `user_writable` but is not mode='code' — "
                "only a code pipeline can accept user-submitted params"
            )
        return self

    @model_validator(mode="after")
    def _validate_exec_requires_code(self) -> PipelineSpec:
        """Reject ``allow_exec``/``allow_egress`` on a non-``code`` pipeline.

        Only a code pipeline is handed a ``ctx``, so these declarations are
        unreachable on an agentic one — a grant that reads as capability while
        granting nothing is the kind of quiet lie an audit has to re-derive.
        History-safe for the SAME reason as ``user_writable`` above: both fields
        are new with an EMPTY default, so no stored snapshot can hold a
        non-empty one and no historical parse can trip this.
        """
        if self.mode != "code" and (self.allow_exec or self.allow_egress):
            raise ValueError(
                f"pipeline {self.name!r} declares `allow_exec`/`allow_egress` but is not "
                "mode='code' — only a code pipeline can call `ctx.exec`"
            )
        return self

    def ensure_wakeable(self) -> None:
        """Refuse the silent-unscheduled state — at the SUBMIT boundary, not parse.

        A pipeline must declare a ``schedule``, be ``on_demand``, or carry a
        LEGACY builder-armed ``trigger_ref`` (itself a scheduling declaration);
        one with NONE of the three would never run. ``AppLifecycle.submit``
        calls this for every INCOMING pipeline, and the builder's
        ``SubmitPipelineArgs`` enforces the same floor at the tool boundary —
        so a new silently-unscheduled app stays unshippable.

        Deliberately NOT a ``model_validator``: the app store is append-only —
        every legacy ``AppVersion`` snapshot holds pipelines with none of the
        three fields, and HISTORY MUST PARSE FOREVER. A parse-time floor 500'd
        every detail read of any app with a pre-Phase-1 snapshot (live-verified
        on app-a2a5f299e0ad the hour it deployed). Validation of new data
        belongs at the trust boundary it crosses; stored history re-crosses the
        parse seam under the contract it was WRITTEN with.
        """
        if self.schedule is None and not self.on_demand and self.trigger_ref is None:
            raise ValueError(
                f"pipeline {self.name!r} declares no schedule and is not on_demand — "
                "declare a `schedule` (time.cron/time.at) or set `on_demand: true`"
            )


class AppPolicies(_AppsModel):
    """Declarative reactions wired onto existing seams — no new hook engine."""

    on_pipeline_failure: PipelineFailurePolicy = "notify"
    retention_days: int | None = Field(default=None, gt=0)
    max_docs_per_collection: int = Field(default=50_000, gt=0)


class AppFrontend(_AppsModel):
    """The multi-file stlite frontend bundle.

    ``files`` must be non-empty, every path must be relative with no
    traversal (see ``_AppsModel._validate_relative_path``), and
    ``entrypoint`` must be a key of ``files`` — a manifest with no runnable
    entrypoint is not meaningfully an app.
    """

    entrypoint: str = "app.py"
    files: dict[str, str]
    requirements: list[str] = Field(default_factory=list)

    @field_validator("files")
    @classmethod
    def _validate_files(cls, value: dict[str, str]) -> dict[str, str]:
        if not value:
            raise ValueError("files must not be empty")
        for path in value:
            cls._validate_relative_path(path)
        return value

    @model_validator(mode="after")
    def _entrypoint_present(self) -> AppFrontend:
        if self.entrypoint not in self.files:
            raise ValueError(f"entrypoint {self.entrypoint!r} not present in files")
        return self


# ---------------------------------------------------------------------------
# AppSpec — the durable, versioned manifest
# ---------------------------------------------------------------------------


class AppSpec(_AppsModel):
    """The durable, versioned app manifest, keyed by ``app_id``.

    ``version``/``status`` are the mutable runtime facets (see
    :meth:`transition`); everything else describes the current build.
    ``created_at``/``updated_at`` are aware timestamps — validated, never
    silently assumed UTC.
    """

    app_id: str
    title: str
    summary: str = ""
    icon: str = "🧩"
    owner_session_id: str
    workspace_ref: WorkspaceRef
    frontend: AppFrontend
    collections: list[CollectionSpec] = Field(default_factory=list)
    pipelines: list[PipelineSpec] = Field(default_factory=list)
    policies: AppPolicies = Field(default_factory=AppPolicies)
    maintainer_session_id: str | None = None
    version: int = Field(default=1, ge=1)
    status: AppStatus = "draft"
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    @field_validator("app_id")
    @classmethod
    def _validate_app_id(cls, value: str) -> str:
        """Reject a colon in ``app_id`` — it breaks render-token parsing.

        ``AppReadTokenSigner`` packs ``<app_id>:<exp>:<nonce>:<sig>`` and recovers
        ``app_id`` via ``split(":")``; a colon-bearing ``app_id`` makes EVERY token
        for the app unverifiable (``split`` yields >4 parts ⇒ a uniform 401), a
        self-DoS. The tokens docstring ASSERTS colon-free — this is what ENFORCES
        it, at the manifest boundary, so a bad ``app_id`` is refused up front rather
        than silently breaking token minting later.
        """
        if ":" in value:
            raise ValueError(
                f"app_id must not contain ':' (it breaks render-token parsing), got {value!r}"
            )
        return value

    @field_validator("created_at", "updated_at")
    @classmethod
    def _validate_timestamps(cls, value: datetime) -> datetime:
        return cls._require_aware(value)

    def transition(self, to: AppStatus, *, now: datetime) -> None:
        """Move to status *to*, guarding illegal transitions.

        Mirrors ``TriggerSpec.transition``: a no-op (``to == self.status``)
        is a silent success that leaves ``updated_at`` untouched (even from
        ``archived`` — the terminal absorbs, it doesn't reject its own
        status back at itself). ``archived`` is otherwise the one status no
        transition may leave. On a genuine move, ``updated_at`` is stamped
        from the caller's *now* — never the wall clock.
        """
        if to == self.status:
            return
        if self.status == _ARCHIVED:
            raise ValueError(
                f"app {self.app_id} cannot transition out of terminal status 'archived'"
            )
        if to not in _ALLOWED_TRANSITIONS[self.status]:
            raise ValueError(f"illegal app transition {self.status!r} -> {to!r}")
        self.status = to
        self.updated_at = now

    def served_frontend_files(self) -> dict[str, str]:
        """The ``frontend.files`` that may reach the browser — pipeline source excluded.

        Strategy-on-model (the render seam calls this, never re-deriving the
        predicate): a ``mode="code"`` pipeline's ``entrypoint`` — and anything under
        the conventional ``pipelines/`` dir (helper modules the entrypoint imports)
        — is the SERVER-side engine's input (executed via ``AppPipelineRunner``,
        never in stlite/pyodide), so it is withheld from the served bundle. The
        STORED manifest keeps every file (the runner reads them); this projection is
        what the API serves.

        Applied to the rendered top-level spec AND every ``versions[]`` snapshot on
        the wire — history must never leak the exact source the live strip withholds
        — while the durable store (and its version history) retains it. Excludes
        pipeline source only; SDK injection is a separate render-seam concern the
        controller layers on top of this.
        """
        code_entrypoints = frozenset(
            p.entrypoint for p in self.pipelines if p.mode == "code" and p.entrypoint
        )
        return {
            name: content
            for name, content in self.frontend.files.items()
            if name not in code_entrypoints and not name.startswith("pipelines/")
        }

    @property
    def has_user_writable_pipeline(self) -> bool:
        """Whether any pipeline declares ``user_writable`` (frontend form input).

        The structural least-privilege gate the token mint reads (a write token
        exists only to let a served frontend POST a form pipeline's params, so an
        app with none can never obtain one). On the model because it is intrinsic to
        the manifest — ``user_writable`` always parses (default ``False``), so this
        works against every historical snapshot too.
        """
        return any(p.user_writable for p in self.pipelines)

    def ensure_unique_pipeline_names(self) -> None:
        """Refuse duplicate pipeline names — at the SUBMIT boundary, not parse.

        Pipeline resolution is first-match everywhere (``_find_pipeline`` on the
        routes controller, ``_resolve_pipeline`` on the run tracker), so two
        pipelines sharing a name silently shadow one: the fire seam, the ledger, and
        every ``trigger_ref`` binding would all resolve to whichever appears first,
        and the other becomes unreachable dead weight.

        Deliberately NOT a parse-time ``model_validator`` — the same discipline
        :meth:`PipelineSpec.ensure_wakeable` follows: the app store is append-only,
        so a legacy snapshot that already carries a duplicate must keep PARSING for a
        detail read (a parse-time floor 500'd every detail read of a pre-existing
        app — the live-verified ``ensure_wakeable`` trap). Validation of NEW data
        belongs at the trust boundary it crosses; stored history re-crosses the parse
        seam under the contract it was written with. ``AppLifecycle.submit`` calls
        this for every incoming draft, so it surfaces through submit's existing
        validation reask.
        """
        seen: set[str] = set()
        dupes: list[str] = []
        for pipeline in self.pipelines:
            if pipeline.name in seen and pipeline.name not in dupes:
                dupes.append(pipeline.name)
            seen.add(pipeline.name)
        if dupes:
            raise ValueError(
                f"duplicate pipeline name(s): {', '.join(sorted(dupes))} — each "
                "pipeline needs a unique name"
            )


class AppVersionSummary(_AppsModel):
    """What one version changed vs the previous one — a pure, computed diff.

    Strategy-on-model (mirrors the trigger-domain precedent): the diff logic lives
    ON the model that owns the shape, computed with :meth:`compute` and rendered by
    :meth:`describe`, never a service-side switch. Both sides arrive as method args
    (:class:`AppSpec` in, no store/clock), so it imports no I/O. Stored on the
    :class:`AppVersion` row so the agent transcript, the user timeline, and the
    console all read the SAME verifiable fact instead of re-deriving it.
    """

    files_added: int = 0
    files_changed: int = 0
    files_removed: int = 0
    pipelines_added: list[str] = Field(default_factory=list)
    pipelines_removed: list[str] = Field(default_factory=list)
    pipelines_changed: list[str] = Field(default_factory=list)
    collections_added: list[str] = Field(default_factory=list)
    collections_removed: list[str] = Field(default_factory=list)

    @classmethod
    def compute(cls, prev: AppSpec | None, new: AppSpec) -> AppVersionSummary:
        """Diff *new* against *prev* (``None`` for v1 ⇒ everything is "added").

        Files diff by path (a changed file = present in both with different source);
        pipelines and collections diff by NAME. A pipeline "changed" on any of
        ``schedule``/``mode``/``entrypoint``/``tools_allowlist`` (the invocation-
        shaping facets) — NOT its platform-owned ``trigger_ref`` (which the lifecycle
        re-mints every submit, so it is never a builder-authored change). Collections
        report add/remove only (a schema edit keeps the same collection).
        """
        prev_files = prev.frontend.files if prev is not None else {}
        new_files = new.frontend.files
        files_added = sum(1 for name in new_files if name not in prev_files)
        files_removed = sum(1 for name in prev_files if name not in new_files)
        files_changed = sum(
            1 for name, src in new_files.items() if name in prev_files and prev_files[name] != src
        )

        prev_pipes = {p.name: p for p in (prev.pipelines if prev is not None else [])}
        new_pipes = {p.name: p for p in new.pipelines}
        pipelines_added = sorted(name for name in new_pipes if name not in prev_pipes)
        pipelines_removed = sorted(name for name in prev_pipes if name not in new_pipes)
        pipelines_changed = sorted(
            name
            for name in new_pipes
            if name in prev_pipes and cls._pipeline_changed(prev_pipes[name], new_pipes[name])
        )

        prev_cols = {c.name for c in (prev.collections if prev is not None else [])}
        new_cols = {c.name for c in new.collections}

        return cls(
            files_added=files_added,
            files_changed=files_changed,
            files_removed=files_removed,
            pipelines_added=pipelines_added,
            pipelines_removed=pipelines_removed,
            pipelines_changed=pipelines_changed,
            collections_added=sorted(new_cols - prev_cols),
            collections_removed=sorted(prev_cols - new_cols),
        )

    @staticmethod
    def _pipeline_changed(prev: PipelineSpec, new: PipelineSpec) -> bool:
        """Whether a same-named pipeline changed on a builder-authored facet."""
        return (
            prev.schedule != new.schedule
            or prev.mode != new.mode
            or prev.entrypoint != new.entrypoint
            or sorted(prev.tools_allowlist) != sorted(new.tools_allowlist)
        )

    def describe(self) -> str:
        """One compact human line naming what changed (``"no changes"`` if nothing did)."""
        parts: list[str] = []
        if self.files_added:
            parts.append(f"+{self._count(self.files_added, 'file')}")
        if self.files_changed:
            parts.append(f"{self._count(self.files_changed, 'file')} changed")
        if self.files_removed:
            parts.append(f"-{self._count(self.files_removed, 'file')}")
        if self.pipelines_added:
            parts.append(self._named("+", self.pipelines_added, "pipeline"))
        if self.pipelines_changed:
            parts.append(self._named("", self.pipelines_changed, "pipeline", suffix=" changed"))
        if self.pipelines_removed:
            parts.append(self._named("-", self.pipelines_removed, "pipeline"))
        if self.collections_added:
            parts.append(self._named("+", self.collections_added, "collection"))
        if self.collections_removed:
            parts.append(self._named("-", self.collections_removed, "collection"))
        return ", ".join(parts) if parts else "no changes"

    @staticmethod
    def _count(n: int, noun: str) -> str:
        return f"{n} {noun}" if n == 1 else f"{n} {noun}s"

    @classmethod
    def _named(cls, sign: str, names: list[str], noun: str, *, suffix: str = "") -> str:
        return f"{sign}{cls._count(len(names), noun)}{suffix} ({', '.join(names)})"


class AppVersion(_AppsModel):
    """An append-only snapshot in an app's version history (spec §2.9).

    Rollback = repoint the active version to an earlier snapshot; it is
    never a status transition and never mutates prior history.
    """

    app_id: str
    version: int = Field(ge=1)
    spec: AppSpec
    author: AppVersionAuthor
    note: str = ""
    created_at: datetime = Field(default_factory=_utc_now)
    # -- version transparency (additive; both default None) --------------
    # What this version changed vs the previous one (``AppVersionSummary.compute``),
    # computed at submit where the prior spec is at hand and stored here so every
    # surface reads the same verifiable fact. ``None`` on a pre-existing row (the
    # field is new) — clients must tolerate its absence.
    summary: AppVersionSummary | None = None
    # Per-pipeline submit-time verification verdict keyed by pipeline name (see
    # ``AppLifecycle.submit``'s verifier). ``None`` on a pre-existing row.
    verification: dict[str, PipelineVerdict] | None = None


# ---------------------------------------------------------------------------
# Runtime / data-plane models
# ---------------------------------------------------------------------------


class PipelineRun(_AppsModel):
    """The provenance ledger for one pipeline execution (spec §2.8).

    Opened at trigger fire, incremented by ``app_data`` writes, closed at
    maintainer run end. It is the provenance chain, the freshness signal
    (:meth:`freshness`), the system-namespace backing, and the repair-loop
    input.
    """

    run_key: str
    app_id: str
    pipeline_name: str
    trigger_id: str | None = None
    session_run_id: str | None = None
    started_at: datetime
    ended_at: datetime | None = None
    status: PipelineRunStatus = "running"
    docs_written: dict[str, int] = Field(default_factory=dict)
    cursor_before: dict[str, Any] = Field(default_factory=dict)
    cursor_after: dict[str, Any] | None = None
    error: str | None = None
    # -- Phase 2: code-pipeline attribution (additive; agentic runs default) --
    # How this run was invoked: a ``scheduled`` fire (a trigger, agentic or code) or
    # an ``on_request`` code-pipeline invocation (the run_pipeline tool / REST run
    # endpoint). Defaults to ``scheduled`` so every existing agentic open/close is
    # unchanged.
    kind: Literal["scheduled", "on_request"] = "scheduled"
    # sha256 of the canonical invocation params (``AppPipelineRunner.params_hash``)
    # — the cache key discriminator; ``None`` for an agentic run (no params).
    params_hash: str | None = None
    # Whether a code run's result was served from the runner's cache (``hit``) or
    # freshly computed (``miss``); ``None`` for an agentic run.
    cache: Literal["hit", "miss"] | None = None

    @field_validator("started_at", "ended_at")
    @classmethod
    def _validate_timestamps(cls, value: datetime | None) -> datetime | None:
        return cls._require_aware(value) if value is not None else value

    @classmethod
    def open(
        cls,
        *,
        run_key: str,
        app_id: str,
        pipeline_name: str,
        now: datetime,
        trigger_id: str | None = None,
        session_run_id: str | None = None,
        cursor_before: dict[str, Any] | None = None,
        kind: Literal["scheduled", "on_request"] = "scheduled",
        params_hash: str | None = None,
    ) -> PipelineRun:
        """Open a new ``running`` ledger entry — the trigger-fire write primitive.

        ``kind``/``params_hash`` (both additive, defaulting to the agentic-run
        shape) attribute a code-pipeline run; an agentic fire passes neither.
        """
        return cls(
            run_key=run_key,
            app_id=app_id,
            pipeline_name=pipeline_name,
            trigger_id=trigger_id,
            session_run_id=session_run_id,
            started_at=now,
            status="running",
            cursor_before=cursor_before or {},
            kind=kind,
            params_hash=params_hash,
        )

    def record_write(self, collection: str, count: int = 1) -> PipelineRun:
        """Increment ``docs_written[collection]`` by *count* — the ``app_data`` write hook."""
        if self.status != "running":
            raise ValueError(
                f"cannot record a write on pipeline run {self.run_key!r}: "
                f"already {self.status}"
            )
        if count < 1:
            raise ValueError(f"count must be >= 1, got {count}")
        self.docs_written[collection] = self.docs_written.get(collection, 0) + count
        return self

    def close(
        self,
        *,
        now: datetime,
        status: Literal["succeeded", "failed"],
        cursor_after: dict[str, Any] | None = None,
        error: str | None = None,
        cache: Literal["hit", "miss"] | None = None,
    ) -> PipelineRun:
        """Close a ``running`` ledger entry at maintainer run end.

        A ``failed`` close requires *error* — the honesty requirement (spec
        §6: app status honest, never silent) starts at the ledger: a failed
        run with no recorded reason is not an acceptable terminal state.
        ``cache`` (additive) records a code run's hit/miss; ``None`` for agentic.
        """
        if self.status != "running":
            raise ValueError(f"pipeline run {self.run_key!r} is already {self.status}")
        if status == "failed" and error is None:
            raise ValueError("a failed pipeline run requires an error message")
        self.ended_at = now
        self.status = status
        self.cursor_after = cursor_after
        self.error = error
        self.cache = cache
        return self

    @property
    def wrote_nothing(self) -> bool:
        """Whether this run ended ``succeeded`` with nothing recorded in the ledger.

        Not a new :data:`PipelineRunStatus` member — a run that raises no error but
        writes zero documents is a legitimate no-op, indistinguishable from a real
        success unless something says so. This is that derived signal, computed
        from data the run already carries rather than a status the maintainer
        would have to remember to set.
        """
        return self.status == "succeeded" and not self.docs_written

    def unwritten_collections(self, declared_collections: Sequence[str]) -> list[str]:
        """Declared collection names this SUCCEEDED run recorded zero writes to.

        The other half of the honesty gap :attr:`wrote_nothing` doesn't cover: a
        run can write real documents to ONE declared collection while silently
        missing another — status ``succeeded``, ``docs_written`` non-empty, yet a
        collection the app declares (and a served frontend page may read) never
        appears in the ledger. This is the exact shape of the incident that
        motivated both signals: a pipeline wrote to ``today_digest`` while the
        ``records`` collection every page actually read stayed empty, and
        ``wrote_nothing`` alone couldn't see it (``docs_written`` wasn't empty).
        A run has no notion of the app's declared collections — that's
        :class:`AppSpec` data — so the caller (``routes.py``, which already holds
        the spec) supplies them as an argument, the same discipline
        ``PipelineSchedule.to_trigger_spec`` uses for ``now``: the model takes
        context in, it never reaches out for it. Scoped to ``succeeded`` runs
        only, matching :attr:`wrote_nothing` — a failed or still-``running`` run
        reports none here; its own status already says enough.
        """
        if self.status != "succeeded":
            return []
        return sorted(name for name in declared_collections if not self.docs_written.get(name))

    def new_integrity_violations(
        self, declared_collections: Sequence[str], *, prior_runs: Sequence[PipelineRun]
    ) -> list[str]:
        """Collections this run REGRESSED, that the run before it did not already report.

        :meth:`unwritten_collections` is the honest LEVEL signal — "this run missed
        these declared collections" — and is right for ``/system`` and a log line.
        It is the wrong trigger for an automated reaction, for two reasons this
        method fixes:

        **1. It cannot tell "regressed" from "never populated".** An app whose
        upstream genuinely has no rows yet writes nothing forever, legitimately;
        a pipeline that USED to fill a collection and now writes zero is a real
        break. The discriminator is this pipeline's OWN ledger history: a
        collection only counts as watched once some earlier succeeded run of THIS
        pipeline actually wrote to it (*prior_runs* is that history). So a
        first-ever run has an empty baseline and reports nothing, and a
        never-written collection never enters the baseline at all.

        Deliberately NOT derived from the app's live data store, which would look
        like the more direct question ("does this collection hold documents?").
        Collections are declared APP-wide while runs are per-PIPELINE, so a store
        read cannot attribute a collection to the pipeline that feeds it: in a
        two-pipeline app every run of pipeline A would report pipeline B's
        collection as regressed, forever. The ledger baseline is
        pipeline-attributed by construction, needs no I/O, and answers the
        narrower question correctly.

        **2. A level signal re-fires every cycle.** Once a collection is broken it
        stays unwritten on every subsequent run, so reacting to the level alone
        repeats the reaction indefinitely — including when the reaction is a
        repair run that did not fix it. This is EDGE-triggered instead: the same
        violation reported by the immediately-preceding succeeded run is
        subtracted, so one break dispatches ONCE. Visibility stays level-triggered
        (``/system`` keeps showing it); only the ACTION is edged.

        Cache HITS are excluded on both sides — a served-from-cache run writes
        nothing BY DESIGN, so counting it would report every watched collection as
        regressed, and using it as the comparison point would mask a real one.
        Succeeded runs only, matching the two signals above. NOW is not needed:
        this is ordinal (which run preceded which), never elapsed time.
        """
        if self.status != "succeeded" or self.cache == "hit":
            return []
        history = [
            run
            for run in prior_runs
            if run.run_key != self.run_key and run.status == "succeeded" and run.cache != "hit"
        ]
        watched = {
            name
            for name in declared_collections
            if any(run.docs_written.get(name) for run in history)
        }
        regressed = {name for name in watched if not self.docs_written.get(name)}
        if not regressed:
            return []
        previous = max(history, key=lambda run: run.started_at, default=None)
        if previous is not None:
            regressed -= {name for name in watched if not previous.docs_written.get(name)}
        return sorted(regressed)

    @classmethod
    def freshness(cls, runs: Sequence[PipelineRun], *, now: datetime) -> timedelta | None:
        """Age of the most recent SUCCEEDED run's completion, or ``None`` if never succeeded.

        The freshness signal the system-namespace introspection endpoint
        (spec §2.6) surfaces per app/collection. Ignores ``failed`` and
        still-``running`` entries — only a completed success counts as
        fresh data.
        """
        ended_ats = [r.ended_at for r in runs if r.status == "succeeded" and r.ended_at is not None]
        if not ended_ats:
            return None
        return now - max(ended_ats)

    @classmethod
    def cooldown_remaining(
        cls, runs: Sequence[PipelineRun], *, now: datetime, cooldown_seconds: int
    ) -> int | None:
        """Whole seconds left before a fresh manual fire is allowed, or ``None`` if none.

        The anti-spam guard for an on-demand AGENTIC fire (a code fire is
        TTL-cache-protected, so it has no cooldown): if the most-recent run of this
        pipeline — ANY ``kind``/``status``, keyed on ``started_at`` — began within
        ``cooldown_seconds`` of *now*, the pipeline is cooling down and this returns
        the whole seconds remaining (rounded UP, so a caller's ``retry_after_seconds``
        never under-promises). Otherwise, or when ``cooldown_seconds <= 0`` or there
        are no runs, ``None``. Strategy-on-model beside :meth:`freshness`: NOW is a
        method argument, never a wall-clock read.
        """
        if cooldown_seconds <= 0 or not runs:
            return None
        latest = max(runs, key=lambda r: r.started_at)
        remaining = cooldown_seconds - (now - latest.started_at).total_seconds()
        if remaining <= 0:
            return None
        return int(math.ceil(remaining))


class PipelineIssue(_AppsModel):
    """Why an app needs attention — the ONE input to the ``on_pipeline_failure`` dispatch.

    ``AppLifecycle.handle_pipeline_failure`` used to take a bare ``error: str |
    None``, which was enough while the only reason to react was "the run raised".
    A second reason now reaches the same dispatch: a run that SUCCEEDED and still
    stopped filling a collection it used to fill. That is not a failure and must
    never be recorded as one — the run genuinely succeeded, and restating it as
    ``failed`` would corrupt ``stale``/``last_success_at``/freshness, the signals
    the health surface just made honest. So the run STATUS keeps telling the truth
    and this carries the orthogonal "needs attention" axis alongside it.

    Two shapes rather than a bare string because the reaction has to be actionable:
    a repair agent told only "something went wrong" hunts for an exception that
    never happened. Each member therefore owns its own prose (:meth:`describe`,
    :meth:`repair_brief`) instead of the lifecycle branching on a code — the
    strategy-on-model rule. It crosses a trust boundary (it is persisted into an
    ``app_issue`` transcript event), so it is a strict model, not a dataclass.
    """

    kind: Literal["run_failed", "unwritten_collections"]
    pipeline_name: str
    error: str | None = None
    collections: list[str] = Field(default_factory=list)

    @classmethod
    def run_failed(cls, pipeline_name: str, error: str | None) -> PipelineIssue:
        """The classic reaction cause: the run raised and closed ``failed``."""
        return cls(kind="run_failed", pipeline_name=pipeline_name, error=error)

    @classmethod
    def unwritten(cls, pipeline_name: str, collections: Sequence[str]) -> PipelineIssue:
        """A SUCCEEDED run that stopped writing collections it previously wrote.

        Built from :meth:`PipelineRun.new_integrity_violations`, which owns the
        regressed-vs-never-populated and edge-vs-level rules.
        """
        return cls(
            kind="unwritten_collections",
            pipeline_name=pipeline_name,
            collections=list(collections),
        )

    @property
    def needs_own_event(self) -> bool:
        """Whether this issue must emit ``app_issue`` regardless of the policy.

        A ``run_failed`` issue already leaves a user-visible artifact without any
        help: the ledger row closes ``failed`` and carries the error, so
        ``/system`` and the run list show it under every policy. An integrity
        violation leaves NO such trace by construction — the run is a green
        ``succeeded`` row — so under ``repair`` or ``pause`` the user would see
        the app act on something they were never told about. The event is this
        kind's only push signal, so it is emitted on top of whatever the policy
        does rather than instead of it.
        """
        return self.kind == "unwritten_collections"

    def describe(self) -> str:
        """One line naming what happened — the ``app_issue`` payload's ``error``."""
        if self.kind == "run_failed":
            return self.error or "the last run ended without success"
        names = ", ".join(self.collections)
        return (
            f"pipeline {self.pipeline_name!r} succeeded but wrote no documents to "
            f"declared collection(s) {names}, which earlier runs of it did populate"
        )

    def repair_brief(self) -> str:
        """The 'what happened' block of the repair prompt — FACTS only.

        Three-way split, deliberately: this owns the FACTS (which pipeline, which
        collections, whether anything raised), ``AppLifecycle._repair_prompt`` owns
        the SURFACES to reach for, and the ``app-repair`` AgentDef owns the
        durable HOW — the hypothesis set, the ordering, the verification bar.
        The hypothesis list lived here in an earlier cut and was moved down into
        the AgentDef: it is the same advice for a platform-dispatched repair and a
        user-reported "my dashboard is empty", so duplicating it in a prompt
        string would have been a second copy to drift.
        """
        if self.kind == "run_failed":
            return (
                f"Pipeline {self.pipeline_name!r} FAILED and needs repair.\n\n"
                f"Failure: {self.describe()}"
            )
        names = ", ".join(self.collections)
        return (
            f"Pipeline {self.pipeline_name!r} is silently writing no data and needs "
            "repair.\n\n"
            "The run SUCCEEDED — it raised no exception, so there is NO error in the "
            "ledger to find and no traceback to hunt for. What it did NOT do is write "
            f"any document to these declared collection(s): {names}. Earlier runs of "
            "this same pipeline DID write to them, so this is a regression, not an app "
            "that has never had data."
        )


class PipelineResult(_AppsModel):
    """The frozen outcome of ONE code-pipeline execution (Phase 2).

    Returned by :meth:`AppPipelineRunner.execute` — the single contract both the
    REST run endpoint (`routes.py` reads these four attributes structurally) and
    the ledger writer consume. ``output`` is the (already JSON-serializable,
    size-capped) value ``run(params, ctx)`` returned; ``cache`` says whether it
    was freshly computed (``miss``) or served from the runner's process-local
    cache (``hit``, in which case ``docs_written`` is empty — a hit does no
    writes); ``docs_written`` mirrors the per-collection counts the run's ``ctx``
    accumulated. Frozen because it is an immutable record of a completed run — a
    failure is RAISED (:class:`~mewbo_api.apps.pipeline_runner.PipelineExecutionError`),
    never encoded as a result, so this type only ever represents success.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    output: Any = None
    evaluated_at: datetime
    cache: Literal["hit", "miss"]
    docs_written: dict[str, int] = Field(default_factory=dict)

    @field_validator("evaluated_at")
    @classmethod
    def _validate_evaluated_at(cls, value: datetime) -> datetime:
        return cls._require_aware(value)


class AppDataDoc(_AppsModel):
    """One stored document in ``app_data``, keyed by ``(app_id, collection, key)``."""

    app_id: str
    collection: str
    key: str
    doc: dict[str, Any]
    updated_at: datetime = Field(default_factory=_utc_now)

    @field_validator("updated_at")
    @classmethod
    def _validate_updated_at(cls, value: datetime) -> datetime:
        return cls._require_aware(value)


class AppReadToken(_AppsModel):
    """A short-lived, render-scoped token minted when an app is opened.

    Carries ``app_id`` + a scope only (spec §2.7; ``write`` added in Phase 2
    for pipeline invocation) — never a general session credential. Default
    ``scope`` stays ``"read"``; minting ``"write"`` is gated master-key-only at
    the route (see ``routes.py:AppsRoutesController.mint_token``).
    """

    token_id: str
    app_id: str
    scope: AppReadTokenScope = "read"
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def _validate_expires_at(cls, value: datetime) -> datetime:
        return cls._require_aware(value)

    def is_valid(self, now: datetime) -> bool:
        """Whether this token is still usable at *now* (strictly before expiry)."""
        return now < self.expires_at


# ---------------------------------------------------------------------------
# Wire events (build-phase terminal + version-change notifications)
# ---------------------------------------------------------------------------


class AppReadyEvent(_AppsModel):
    """Wire payload for the ``app_ready`` build-phase terminal event."""

    app_id: str
    title: str
    summary: str
    version: int = Field(ge=1)


class AppUpdatedEvent(_AppsModel):
    """Wire payload for the ``app_updated`` event (repair/rollback/edit)."""

    app_id: str
    version: int = Field(ge=1)
    author: AppVersionAuthor


__all__ = [
    "AppStatus",
    "WorkspaceRefKind",
    "PipelineFailurePolicy",
    "PipelineRunStatus",
    "AppVersionAuthor",
    "AppReadTokenScope",
    "PipelineVerdict",
    "PIPELINE_ALLOWED_EXEC",
    "WorkspaceRef",
    "CollectionSpec",
    "CronSchedule",
    "AtSchedule",
    "PipelineSchedule",
    "PipelineSpec",
    "AppPolicies",
    "AppFrontend",
    "AppSpec",
    "AppVersionSummary",
    "AppVersion",
    "PipelineRun",
    "PipelineIssue",
    "PipelineResult",
    "AppDataDoc",
    "AppReadToken",
    "AppReadyEvent",
    "AppUpdatedEvent",
]
