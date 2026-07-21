#!/usr/bin/env python3
"""Best-effort provenance hash chain for a session's agent tree.

A ``spawn``/``terminal`` pair of records brackets one child agent's lifetime;
every record's hash is computed over its own body (INCLUDING the previous
record's hash), so the sequence forms a tamper-evident chain the same way a
transparency log does. This module is ZERO I/O: no store, no clock source
other than ``utc_now_iso`` captured at build time, no session lookups — every
external fact (session id, agent identity, contract, token counts, the raw
summary text to fingerprint) arrives as a method ARGUMENT, mirroring the
``TriggerSpec`` house rule that a model never reaches out for its own inputs.

Privacy/exfil law (binding): a record carries NO task text and NO raw summary
text — only a sha256 fingerprint of the summary, plus the contract snapshot
and bounded scalars already surfaced elsewhere (``check_agents``). Persistence
and durability are the CALLER's problem: ``AttestationChain`` hands a built
record to an injected ``event_logger`` (the same ``Callable[[Event], None]``
every ``AgentContext`` carries) and is best-effort throughout — a failing or
absent sink degrades to a no-op, never a broken spawn.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from mewbo_core.common import get_logger, utc_now_iso
from mewbo_core.hypervisor import AgentStatus, DelegationContract, SummaryKind
from mewbo_core.types import Event

logging = get_logger(name="core.attestation")

GENESIS_HASH = "0" * 64


class ContractSnapshot(BaseModel):
    """Frozen mirror of ``DelegationContract.snapshot()`` — bounded scalars only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_steps: int = 0
    max_wall_s: float = 0.0
    max_tokens: int = 0
    autonomy: str = "open_ended"
    model_tier: str | None = None
    step_warn_headroom: int = 3

    @classmethod
    def from_contract(cls, contract: DelegationContract | None) -> ContractSnapshot:
        """Build from a live ``DelegationContract``, or the disabled default when absent."""
        if contract is None:
            return cls()
        return cls(**contract.snapshot())


class _AttBase(BaseModel):
    """Fields shared by every attestation phase — the hashed envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    agent_id: str
    parent_id: str | None
    depth: int
    ts: str  # Captured at build time — part of the hashed body.
    prev_hash: str
    record_hash: str


class SpawnAttestation(_AttBase):
    """Recorded the moment a child is registered.

    Brackets its lifetime with :class:`TerminalAttestation`.
    """

    phase: Literal["spawn"] = "spawn"
    agent_type: str | None = None
    model: str
    capability_mode: str
    contract: ContractSnapshot


class TerminalAttestation(_AttBase):
    """Recorded at the child's absorbing state. Never carries raw summary text."""

    phase: Literal["terminal"] = "terminal"
    terminal_state: AgentStatus
    spawn_hash: str
    attempts: int
    steps_completed: int
    input_tokens: int
    output_tokens: int
    summary_kind: SummaryKind
    summary_hash: str  # sha256 of the child's summary text — never the text itself.
    done_reason: str | None = None


# The ONE discriminated-union parse seam (mirrors ``TriggerUnion``) — no
# service-side ``if phase ==`` dispatch anywhere.
AttestationRecord = Annotated[SpawnAttestation | TerminalAttestation, Field(discriminator="phase")]

# Cached once, mirroring ``TriggerSpec._adapter`` — every raw payload dict a
# caller hands ``AttestationChain.verify`` parses through the SAME adapter.
_RECORD_ADAPTER: TypeAdapter[Any] = TypeAdapter(AttestationRecord)

# Cap on `done_reason` — mirrors the sub_agent event's own summary cap
# (`spawn_agent._SUB_AGENT_SUMMARY_CAP`); this field is a short exception
# blurb, not a transcript, so a far smaller cap is plenty.
_DONE_REASON_CAP = 200


def _hash_fields(fields: Mapping[str, Any]) -> str:
    """Canonical-JSON sha256 over *fields* — the ONE hashing seam every record uses.

    ``sort_keys=True`` + fixed separators make the digest independent of
    dict-construction order; ``ensure_ascii=False`` keeps the digest stable
    across encodings (a caller must still feed pure JSON-safe values).
    """
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class VerifyResult(BaseModel):
    """Outcome of :meth:`AttestationChain.verify` — pure integrity report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    broken_index: int | None = None
    reason: str | None = None


@dataclass
class AttestationChain:
    """One session's provenance chain — atomic state: ``session_id`` + live ``head``.

    Injected into ``AgentHypervisor`` (never constructed by it) and read by
    ``SpawnAgentTool`` at every spawn/terminal seam. ``head`` advances ONLY on
    a successful append, so a failed write never desyncs the in-memory chain
    from what actually landed in the transcript.
    """

    session_id: str
    head: str = GENESIS_HASH

    # -- record construction (pure — no I/O, no event_logger) ---------------

    def _build_spawn_record(
        self,
        *,
        agent_id: str,
        parent_id: str | None,
        depth: int,
        agent_type: str | None,
        model: str,
        capability_mode: str,
        contract: DelegationContract | None,
    ) -> SpawnAttestation:
        contract_snapshot = ContractSnapshot.from_contract(contract)
        fields: dict[str, Any] = {
            "session_id": self.session_id,
            "agent_id": agent_id,
            "parent_id": parent_id,
            "depth": depth,
            "ts": utc_now_iso(),
            "prev_hash": self.head,
            "phase": "spawn",
            "agent_type": agent_type,
            "model": model,
            "capability_mode": capability_mode,
            "contract": contract_snapshot.model_dump(mode="json"),
        }
        return SpawnAttestation(**fields, record_hash=_hash_fields(fields))

    def _build_terminal_record(
        self,
        *,
        agent_id: str,
        parent_id: str | None,
        depth: int,
        terminal_state: AgentStatus,
        spawn_hash: str,
        attempts: int,
        steps_completed: int,
        input_tokens: int,
        output_tokens: int,
        summary_kind: SummaryKind,
        summary_text: str,
        done_reason: str | None,
    ) -> TerminalAttestation:
        summary_hash = hashlib.sha256((summary_text or "").encode("utf-8")).hexdigest()
        capped_reason = done_reason[:_DONE_REASON_CAP] if done_reason else done_reason
        fields: dict[str, Any] = {
            "session_id": self.session_id,
            "agent_id": agent_id,
            "parent_id": parent_id,
            "depth": depth,
            "ts": utc_now_iso(),
            "prev_hash": self.head,
            "phase": "terminal",
            "terminal_state": terminal_state,
            "spawn_hash": spawn_hash,
            "attempts": attempts,
            "steps_completed": steps_completed,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "summary_kind": summary_kind,
            "summary_hash": summary_hash,
            "done_reason": capped_reason,
        }
        return TerminalAttestation(**fields, record_hash=_hash_fields(fields))

    # -- recording (best-effort I/O via the injected event_logger) -----------

    def record_spawn(
        self,
        event_logger: Callable[[Event], None] | None,
        *,
        agent_id: str,
        parent_id: str | None,
        depth: int,
        agent_type: str | None,
        model: str,
        capability_mode: str,
        contract: DelegationContract | None,
    ) -> str:
        """Append a spawn record and advance ``head`` on success.

        Best-effort: no bound ``event_logger``, a raising one, or any build
        error resolves to ``""`` and leaves ``head`` untouched — a broken
        provenance sink must never break a spawn. One try/except, one log
        line per failed call.
        """
        try:
            if event_logger is None:
                raise RuntimeError("no event_logger bound to this session")
            record = self._build_spawn_record(
                agent_id=agent_id,
                parent_id=parent_id,
                depth=depth,
                agent_type=agent_type,
                model=model,
                capability_mode=capability_mode,
                contract=contract,
            )
            event_logger({"type": "attestation", "payload": record.model_dump(mode="json")})
        except Exception:
            logging.warning(
                "AttestationChain.record_spawn failed for agent {}; continuing without provenance.",
                agent_id,
                exc_info=True,
            )
            return ""
        self.head = record.record_hash
        return record.record_hash

    def record_terminal(
        self,
        event_logger: Callable[[Event], None] | None,
        *,
        agent_id: str,
        parent_id: str | None,
        depth: int,
        terminal_state: AgentStatus,
        spawn_hash: str,
        attempts: int,
        steps_completed: int,
        input_tokens: int,
        output_tokens: int,
        summary_kind: SummaryKind,
        summary_text: str,
        done_reason: str | None,
    ) -> str:
        """Append a terminal record and advance ``head`` on success.

        ``summary_text`` is hashed internally (sha256) and NEVER stored or
        forwarded — only :attr:`TerminalAttestation.summary_hash` rides the
        record. Same best-effort contract as :meth:`record_spawn`.
        """
        try:
            if event_logger is None:
                raise RuntimeError("no event_logger bound to this session")
            record = self._build_terminal_record(
                agent_id=agent_id,
                parent_id=parent_id,
                depth=depth,
                terminal_state=terminal_state,
                spawn_hash=spawn_hash,
                attempts=attempts,
                steps_completed=steps_completed,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                summary_kind=summary_kind,
                summary_text=summary_text,
                done_reason=done_reason,
            )
            event_logger({"type": "attestation", "payload": record.model_dump(mode="json")})
        except Exception:
            logging.warning(
                "AttestationChain.record_terminal failed for agent {}; "
                "continuing without provenance.",
                agent_id,
                exc_info=True,
            )
            return ""
        self.head = record.record_hash
        return record.record_hash

    # -- verification (pure — no I/O) ----------------------------------------

    @staticmethod
    def verify(records: Sequence[Mapping[str, Any]]) -> VerifyResult:
        """Recompute + link-check a sequence of persisted attestation payloads.

        Pure: takes the raw ``payload`` dicts a caller scraped off ``type ==
        "attestation"`` transcript events (or captured straight from a fake
        event_logger in a test) and re-derives every hash from its own body —
        never trusts a stored ``record_hash`` without recomputing it — then
        checks each ``prev_hash`` against the PRECEDING record's (already
        verified) hash. Reports the first broken index and a short reason;
        ``ok`` iff the whole prefix checks out.
        """
        prev = GENESIS_HASH
        for idx, raw in enumerate(records):
            try:
                record = _RECORD_ADAPTER.validate_python(raw)
            except ValidationError:
                return VerifyResult(ok=False, broken_index=idx, reason="invalid_record")
            recomputed = _hash_fields(record.model_dump(mode="json", exclude={"record_hash"}))
            if recomputed != record.record_hash:
                return VerifyResult(ok=False, broken_index=idx, reason="hash_mismatch")
            if record.prev_hash != prev:
                return VerifyResult(ok=False, broken_index=idx, reason="link_break")
            prev = record.record_hash
        return VerifyResult(ok=True)


__all__ = [
    "GENESIS_HASH",
    "AttestationChain",
    "AttestationRecord",
    "ContractSnapshot",
    "SpawnAttestation",
    "TerminalAttestation",
    "VerifyResult",
]
