"""Register stage: append or update ``registry.json`` and the workspace dataset manifest.

A scenario registers only when its ``validation.json`` says ``valid`` for exactly this spec hash and for
the build recorded in ``compile.json``. The registry is keyed by scenario id; changing content under the
same version is refused (increment ``version``). Runs are listed per purpose from the frozen trial records
so calibration, comparison and demonstration trials are never confused.
"""
from __future__ import annotations

import json

from swesim.models import ScenarioSpec
from swesim.paths import Workspace
from swesim.settings import Settings
from swesim.storage.registry import locked_registry
from swesim.stages.score import PURPOSES, load_trial_records

CONDITIONS = ("full", "hidden", "interactive")


def _load(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise ValueError(f"cannot register unvalidated or changed scenario: unreadable {path.name}") from e


def register_scenario(ws: Workspace, spec: ScenarioSpec, *, settings: Settings | None = None) -> dict:
    sdir = ws.scenario_dir(spec.id)
    val = _load(sdir / "validation.json")
    comp = _load(sdir / "compile.json")
    if val.get("status") != "valid" or val.get("spec_hash") != spec.spec_hash \
            or val.get("compiled_hash") != comp.get("compiled_hash") or comp.get("spec_hash", spec.spec_hash) != spec.spec_hash:
        raise ValueError("cannot register unvalidated or changed scenario")
    conditions = [c for c in CONDITIONS if c in (comp.get("task_dirs") or {})] or list(CONDITIONS)
    with locked_registry(ws, settings) as storage:
        reg = storage.read()
        reg.setdefault("scenarios", {})
        previous = reg["scenarios"].get(spec.id)
        if previous and previous.get("spec_hash") != spec.spec_hash and int(previous.get("version", 0)) >= spec.version:
            raise ValueError(f"increment scenario version: {spec.id} v{previous.get('version')} already registered with other content")
        entry = {"id": spec.id, "version": spec.version, "spec_hash": spec.spec_hash, "source": spec.source.value,
                 "source_ref": spec.source_ref, "validation_status": "valid",
                 "validation_path": str(sdir / "validation.json"), "conditions": conditions,
                 "compiled_hash": comp.get("compiled_hash"), "image_digest": comp.get("image_digest"),
                 "calibration_runs": [], "comparison_runs": [], "demonstration_runs": [], "unresolved_runs": []}
        for r in load_trial_records(ws):
            if r.get("spec_hash") != spec.spec_hash or not r.get("trial_id"):
                continue
            if r.get("unresolved"):  # attempt without a record: listed, never counted as a run of any purpose
                entry["unresolved_runs"].append(r["trial_id"])
                continue
            purpose = r.get("purpose") if r.get("purpose") in PURPOSES else "other"
            entry.setdefault(f"{purpose}_runs", []).append(r["trial_id"])
        reg["scenarios"][spec.id] = entry
        # swesim manifest, not claimed to be Harbor's undocumented dataset.toml format. Paths are relative to
        # the workspace root because the workspace moves between the laptop and the VM.
        tasks = [{"path": ws.task_dir(e["id"], c).relative_to(ws.root).as_posix(), "scenario_id": e["id"],
                  "condition": c, "spec_hash": e["spec_hash"]}
                 for e in reg["scenarios"].values() for c in e.get("conditions", CONDITIONS)]
        storage.write(reg, {"tasks": tasks})
    return entry
