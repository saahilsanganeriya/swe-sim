"""Search attributes: the six Keyword keys every swesim workflow carries (interfaces §5, brief §2.8 item 5).

Contract
- `KEYS` are registered on the Temporal Cloud namespace; `ensure_registered(client)` registers them on any other
  namespace (a local dev server) through the OperatorService and is idempotent.
- `attributes(...)` builds the typed attributes for a `start_workflow` / `start_child_workflow`; unset values are omitted,
  set values are stringified (all six are Keyword attributes, including the integer version).
- `upsert_status(status)` updates `swesim_status` from inside a workflow at every status change; outside a workflow
  (direct tests run workflow bodies as plain coroutines) it is a no-op.
"""
from __future__ import annotations

from temporalio import workflow
from temporalio.common import SearchAttributeKey, SearchAttributePair, TypedSearchAttributes

SCENARIO_ID, MODEL, CONDITION, STATUS, VERSION, PURPOSE = (
    "swesim_scenario_id", "swesim_model", "swesim_condition", "swesim_status", "swesim_version", "swesim_purpose")
KEYS: tuple[str, ...] = (SCENARIO_ID, MODEL, CONDITION, STATUS, VERSION, PURPOSE)
_KEYS: dict[str, SearchAttributeKey[str]] = {name: SearchAttributeKey.for_keyword(name) for name in KEYS}


def key(name: str) -> SearchAttributeKey[str]:
    """The typed Keyword key for one of `KEYS`."""
    return _KEYS[name]


def attributes(*, scenario_id: str | None = None, model: str | None = None, condition: str | None = None, status: str | None = None,
               version: int | str | None = None, purpose: str | None = None) -> TypedSearchAttributes:
    values = ((SCENARIO_ID, scenario_id), (MODEL, model), (CONDITION, condition), (STATUS, status), (VERSION, version), (PURPOSE, purpose))
    return TypedSearchAttributes([SearchAttributePair(_KEYS[name], str(value)) for name, value in values if value is not None])


def upsert_status(status: str) -> None:
    """Inside a workflow: `swesim_status = status`. A plain coroutine call (direct tests) has nothing to upsert into."""
    if workflow.in_workflow():
        workflow.upsert_search_attributes([_KEYS[STATUS].value_set(status)])


async def ensure_registered(client, namespace: str | None = None) -> list[str]:
    """Register the six Keyword attributes on `client`'s namespace when they are missing; returns the names added.
    Temporal Cloud has them already (`swesim.abcde`); this is for the local dev server and the test server."""
    from temporalio.api.enums.v1 import IndexedValueType
    from temporalio.api.operatorservice.v1 import (
        AddSearchAttributesRequest,
        ListSearchAttributesRequest,
    )

    ns = namespace or client.namespace
    existing = await client.operator_service.list_search_attributes(ListSearchAttributesRequest(namespace=ns))
    missing = [name for name in KEYS if name not in existing.custom_attributes and name not in existing.system_attributes]
    if missing:
        await client.operator_service.add_search_attributes(AddSearchAttributesRequest(
            namespace=ns, search_attributes={name: IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD for name in missing}))
    return missing


def for_params(p: dict, status: str = 'running') -> TypedSearchAttributes:
    """Scalar values for a child, or the sole selected value for an operator workflow."""
    def one(singular, plural):
        values = p.get(plural) or []
        return p.get(singular) or (values[0] if len(values) == 1 else None)
    config = p.get('run_config') or {}
    return attributes(scenario_id=one('scenario_id', 'scenario_ids'), model=one('model', 'models'),
                      condition=one('condition', 'conditions'), status=status,
                      version=p.get('version'), purpose=p.get('purpose') or config.get('purpose'))
