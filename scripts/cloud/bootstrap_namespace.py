"""Register swesim's six Keyword search attributes on the configured Temporal namespace, idempotently (handoff P2-14).

Every swesim workflow starts with `swesim_scenario_id swesim_model swesim_condition swesim_status swesim_version
swesim_purpose` (interfaces §5); a fresh namespace must have them before the first `swesim create` / `run-tasks`.

    SWESIM_ENV_FILE=<env file> uv run --frozen python scripts/cloud/bootstrap_namespace.py [--namespace <ns>]

Connection settings come from `Settings` (TEMPORAL_ADDRESS/NAMESPACE, TEMPORAL_API_KEY read on demand); only the auth
mode is printed, never the key. Exit codes:
    0  all six are present (already, or registered now through the SDK's OperatorService)
    2  the namespace refuses the OperatorService (Temporal Cloud: PERMISSION_DENIED / UNIMPLEMENTED) and a visibility
       probe (`count_workflows` on each attribute, read-only) finds some missing or cannot tell; the exact `tcld` command
       and the UI step for those attributes are printed — run one of them once, then re-run this script
    1  any other error (connection, authentication); the error class and gRPC status are printed, nothing else
Temporal Cloud refuses even ListSearchAttributes (measured 2026-09-13 on swesim.abcde), so presence there is verified by
the probe: an unregistered attribute makes the visibility query fail with INVALID_ARGUMENT, a registered one counts 0.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
from dataclasses import dataclass, field

from temporalio.service import RPCError, RPCStatusCode

from swesim.workflows import search_attributes as SA

MANUAL_STATUSES = (RPCStatusCode.PERMISSION_DENIED, RPCStatusCode.UNIMPLEMENTED)


@dataclass(frozen=True)
class Outcome:
    code: int
    summary: str
    attributes: tuple[str, ...] = field(default_factory=tuple)  # registered now (0) or still missing (2)


def tcld_command(namespace: str, names: tuple[str, ...]) -> str:
    flags = " ".join(f"--search-attribute {name}=Keyword" for name in names)
    return f"tcld namespace search-attributes add --namespace {namespace} {flags}"


def manual_steps(namespace: str, names: tuple[str, ...]) -> list[str]:
    return [
        f"Temporal Cloud does not let the SDK register search attributes on {namespace}; add these once as type Keyword:",
        "  " + ", ".join(names),
        "either with the Temporal Cloud CLI (tcld login first):",
        "  " + tcld_command(namespace, names),
        "or in the UI: cloud.temporal.io -> Namespaces -> " + namespace + " -> Edit -> Custom Search Attributes -> Add (type Keyword) -> Save,",
        "then re-run this script; it exits 0 once all six are present.",
    ]


async def probe(client, names: tuple[str, ...] = SA.KEYS) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(missing, unverifiable) by read-only visibility queries: INVALID_ARGUMENT means the attribute is not registered;
    any other refusal means we cannot tell (never reported as missing, never as present)."""
    missing: list[str] = []
    unknown: list[str] = []
    for name in names:
        try:
            await client.count_workflows(f"{name} = 'swesim-bootstrap-probe'")
        except RPCError as e:
            (missing if e.status == RPCStatusCode.INVALID_ARGUMENT else unknown).append(name)
    return tuple(missing), tuple(unknown)


async def bootstrap(client, namespace: str | None = None) -> Outcome:
    """Idempotent: `ensure_registered` adds only what is missing; a refusal becomes the operator step, never a crash."""
    ns = namespace or client.namespace
    try:
        added = tuple(await SA.ensure_registered(client, ns))
    except RPCError as e:
        if e.status not in MANUAL_STATUSES:
            return Outcome(1, f"{type(e).__name__} {e.status.name}: {e.message}")
        missing, unknown = await probe(client)
        if not missing and not unknown:
            return Outcome(0, f"search attributes: all {len(SA.KEYS)} present on {ns} (verified by visibility queries; "
                              f"the OperatorService answers {e.status.name} on Temporal Cloud)")
        detail = f"{len(missing)} missing" + (f", {len(unknown)} unverifiable" if unknown else "")
        return Outcome(2, f"search attributes: {detail} on {ns}; operator step required (OperatorService: {e.status.name})", missing + unknown)
    if added:
        return Outcome(0, f"search attributes: registered {', '.join(added)} on {ns} (Keyword)", added)
    return Outcome(0, f"search attributes: all {len(SA.KEYS)} present on {ns}")


def report(outcome: Outcome, namespace: str, out: Callable[[str], None] = print) -> int:
    out(outcome.summary)
    if outcome.code == 2:
        for line in manual_steps(namespace, outcome.attributes):
            out(line)
    return outcome.code


async def _run(namespace: str | None) -> int:
    from swesim.workflows.client import connect, describe_connection
    print(describe_connection())  # address, namespace and auth mode only
    client = await asyncio.wait_for(connect(), 30)
    ns = namespace or client.namespace
    return report(await bootstrap(client, ns), ns)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--namespace", default=None, help="override TEMPORAL_NAMESPACE (default: the configured one)")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args.namespace))
    except Exception as error:  # noqa: BLE001 - connection/auth failures may carry provider text; report only the class and status
        status = getattr(getattr(error, "status", None), "name", None)
        print(f"bootstrap_namespace: {type(error).__name__}{f' {status}' if status else ''}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
