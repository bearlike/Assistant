"""Reverse-invocation trigger subsystem — domain model + stores + agent surface.

Public surface: the ``TriggerSpec`` family (data-owned behavior, no
service-side ``if kind ==`` dispatch — see ``spec.py``), the
``TriggerStoreBase``/``JsonTriggerStore``/``MongoTriggerStore`` backends
(see ``store.py``/``store_mongo.py``), the ``TriggerPolicy`` admission gate
(``policy.py``), and the agent-facing ``schedule_trigger`` SessionTool
(``session_tool.py``). ``MongoTriggerStore`` is imported lazily via
:func:`create_trigger_store` (mirrors ``create_session_store``), not
re-exported here, so importing this package never requires a reachable
MongoDB.
"""

from mewbo_core.triggers.policy import TriggerPolicy
from mewbo_core.triggers.session_tool import ScheduleTriggerArgs, ScheduleTriggerTool
from mewbo_core.triggers.spec import (
    CiWorkflowTrigger,
    CronTrigger,
    ForgePrTrigger,
    TimeAtTrigger,
    TriggerAuthority,
    TriggerProvenance,
    TriggerSpec,
    TriggerStatus,
    TriggerUnion,
    WebhookTrigger,
    parse_trigger,
)
from mewbo_core.triggers.store import (
    JsonTriggerStore,
    TriggerStoreBase,
    create_trigger_store,
)

__all__ = [
    "TriggerStatus",
    "TriggerProvenance",
    "TriggerAuthority",
    "TriggerSpec",
    "TimeAtTrigger",
    "CronTrigger",
    "CiWorkflowTrigger",
    "ForgePrTrigger",
    "WebhookTrigger",
    "TriggerUnion",
    "parse_trigger",
    "TriggerStoreBase",
    "JsonTriggerStore",
    "create_trigger_store",
    "TriggerPolicy",
    "ScheduleTriggerArgs",
    "ScheduleTriggerTool",
]
