"""Validated payloads for the durable wiki indexing event timeline.

Each event is persisted before it reaches SSE, so this module is the trust
boundary between an event writer and both readers. The concrete models own
payload validation; :meth:`WikiJobEvent.parse` is the one discriminated parse
seam used for a raw stored document.

``idx`` is store metadata rather than event payload. It is accepted so a reader
can parse an event returned by ``load_job_events`` but excluded when a writer
round-trips the payload back to a store.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

_CFG = ConfigDict(extra="forbid", populate_by_name=True)


class WikiJobEvent(BaseModel):
    """Shared durable fields for every wiki indexing event."""

    model_config = _CFG

    type: str
    idx: int | None = Field(default=None, exclude=True)

    _adapter: ClassVar[TypeAdapter | None] = None

    @classmethod
    def parse(cls, data: Mapping[str, object]) -> WikiJobEvent:
        """Parse a known writer's raw event into its concrete event model.

        The adapter is built lazily because writers normally create a plain
        payload and store it once, while readers may parse an entire history.
        """
        if cls._adapter is None:
            cls._adapter = TypeAdapter(WikiJobEventUnion)
        return cls._adapter.validate_python(data)

    @classmethod
    def parse_stored(cls, data: Mapping[str, object]) -> WikiJobEvent | None:
        """Parse a stored event, preserving an unknown future type as unreadable.

        Writers use :meth:`parse` and therefore cannot persist an unrecognised
        type. Readers return ``None`` for one instead, so an event emitted by a
        newer process does not make the rest of a job's historic timeline
        unreadable.
        """
        try:
            return cls.parse(data)
        except ValidationError as exc:
            if any(error["type"] == "union_tag_invalid" for error in exc.errors()):
                return None
            raise

    def stored_payload(self) -> dict[str, Any]:
        """Return the event payload without store-owned metadata.

        ``idx`` is the only excluded field: it belongs to the store's ordering,
        while ``type`` is the durable discriminator and must stay on the wire.
        ``exclude_unset`` preserves the shape written by older event producers.
        """
        return self.model_dump(mode="json", by_alias=True, exclude_unset=True)


class LogJobEvent(WikiJobEvent):
    """A timeline line, attributed to a declared step when one is open."""

    type: Literal["log"]
    level: Literal["info", "warn", "error"] = "info"
    text: str
    step: str | None = None

    @field_validator("level", mode="before")
    @classmethod
    def _normalize_legacy_warning(cls, value: object) -> object:
        """Keep the timeline's one warning spelling while reading older writers."""
        return "warn" if value == "warning" else value

    @field_validator("step")
    @classmethod
    def _step_is_addressable(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("log step must be non-empty when present")
        return value

    @property
    def unattributed(self) -> bool:
        """Whether this log records work outside a declared step."""
        return self.step is None


class PhaseJobEvent(WikiJobEvent):
    """A fine-grained indexing phase transition."""

    type: Literal["phase"]
    name: str

    @field_validator("name")
    @classmethod
    def _name_is_non_empty(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("phase name must be non-empty")
        return name


class ProgressJobEvent(WikiJobEvent):
    """A throttled progress-ledger projection or its legacy wrapper."""

    type: Literal["progress"]
    ledger: dict[str, Any] | None = None
    version: int | None = None
    fraction: float | None = None
    eta_seconds: float | None = Field(default=None, alias="etaSeconds")
    elapsed_seconds: float | None = Field(default=None, alias="elapsedSeconds")
    active_key: str | None = Field(default=None, alias="activeKey")
    groups: list[dict[str, Any]] | None = None
    steps: list[dict[str, Any]] | None = None

    @model_validator(mode="after")
    def _contains_a_projection(self) -> ProgressJobEvent:
        if self.ledger is None and self.version is None:
            raise ValueError("progress event needs a ledger projection")
        return self


class ProgressErrorJobEvent(WikiJobEvent):
    """A ledger mutation that was omitted without interrupting indexing."""

    type: Literal["progress_error"]
    operation: str
    error: str
    step: str | None = None


class ScopePreviewJobEvent(WikiJobEvent):
    """The flat scoped-refresh counts mirrored on the job snapshot."""

    type: Literal["scope_preview"]
    files_added: int = Field(alias="filesAdded")
    files_modified: int = Field(alias="filesModified")
    files_deleted: int = Field(alias="filesDeleted")
    early_cutoff_files: int = Field(alias="earlyCutoffFiles")
    affected_entities: int = Field(alias="affectedEntities")
    memory_kept: int = Field(alias="memoryKept")
    memory_invalidated: int = Field(alias="memoryInvalidated")
    memory_revalidated: int = Field(alias="memoryRevalidated")
    pages_keep: int = Field(alias="pagesKeep")
    pages_edit: int = Field(alias="pagesEdit")
    pages_regenerate: int = Field(alias="pagesRegenerate")
    new_pages: int = Field(alias="newPages")
    llm_calls: int = Field(alias="llmCalls")


class QueuedJobEvent(WikiJobEvent):
    """A checkout whose source facts are ready for indexing."""

    type: Literal["queued"]
    job_id: str | None = Field(default=None, alias="jobId")
    slug: str | None = None
    total_count: int | None = Field(default=None, alias="totalCount")


class ScanningJobEvent(WikiJobEvent):
    """A source file about to be inspected."""

    type: Literal["scanning"]
    file: str | None = None
    index: int | None = None
    total_count: int | None = Field(default=None, alias="totalCount")


class ScannedJobEvent(WikiJobEvent):
    """A source file whose inspection completed (same legacy tolerance)."""

    type: Literal["scanned"]
    file: str | None = None
    index: int | None = None
    total_count: int | None = Field(default=None, alias="totalCount")


class FinalizingJobEvent(WikiJobEvent):
    """The index has committed its page plan."""

    type: Literal["finalizing"]
    scanned_count: int = Field(alias="scannedCount")
    total_count: int = Field(alias="totalCount")


class PlanCommittedJobEvent(WikiJobEvent):
    """The committed plan's page denominator."""

    type: Literal["plan_committed"]
    total_pages: int = Field(alias="totalPages")


class PageCommittedJobEvent(WikiJobEvent):
    """One planned page has become durable."""

    type: Literal["page_committed"]
    page_id: str = Field(alias="pageId")
    index: int
    total_pages: int = Field(alias="totalPages")


class ErrorDetail(BaseModel):
    """The machine-readable reason an indexing job stopped."""

    model_config = _CFG

    code: str
    message: str


class ErrorJobEvent(WikiJobEvent):
    """A terminal or recoverable indexing failure."""

    type: Literal["error"]
    error: ErrorDetail


class CompleteJobEvent(WikiJobEvent):
    """A successfully finalized indexing job."""

    type: Literal["complete"]
    landing_page_id: str | None = Field(default=None, alias="landingPageId")
    page_count: int | None = Field(default=None, alias="pageCount")


class CancelledJobEvent(WikiJobEvent):
    """A job terminally cancelled by its caller."""

    type: Literal["cancelled"]


class DoneJobEvent(WikiJobEvent):
    """A legacy terminal marker retained for historical event logs."""

    type: Literal["done"]


WikiJobEventUnion = Annotated[
    LogJobEvent
    | PhaseJobEvent
    | ProgressJobEvent
    | ProgressErrorJobEvent
    | ScopePreviewJobEvent
    | QueuedJobEvent
    | ScanningJobEvent
    | ScannedJobEvent
    | FinalizingJobEvent
    | PlanCommittedJobEvent
    | PageCommittedJobEvent
    | ErrorJobEvent
    | CompleteJobEvent
    | CancelledJobEvent
    | DoneJobEvent,
    Field(discriminator="type"),
]

parse_job_event = WikiJobEvent.parse

__all__ = [
    "WikiJobEvent",
    "LogJobEvent",
    "PhaseJobEvent",
    "ProgressJobEvent",
    "ProgressErrorJobEvent",
    "ScopePreviewJobEvent",
    "QueuedJobEvent",
    "ScanningJobEvent",
    "ScannedJobEvent",
    "FinalizingJobEvent",
    "PlanCommittedJobEvent",
    "PageCommittedJobEvent",
    "ErrorDetail",
    "ErrorJobEvent",
    "CompleteJobEvent",
    "CancelledJobEvent",
    "DoneJobEvent",
    "WikiJobEventUnion",
    "parse_job_event",
]
