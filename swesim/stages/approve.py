"""Approval freezes a draft into scenario.json. Two modes:
human: a person edited the draft and runs `swesim approve <id>`.
auto (unattended runs): code-owned gates decide; the record says so and flags the scenario for post-hoc human review.
Never hand-edit a draft to satisfy a gate; re-draft once or take the next candidate."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from swesim.models import Discovery, OperationType, ScenarioSpec
from swesim.paths import Workspace, atomic_json

AUTO_GATES = ["schema", "assets", "judge-accept", "on-request-fact-linked-to-f2p", "withholding-leak", "controls"]
HUMAN_GATES = ["schema", "assets"]


def _judgement(spec: ScenarioSpec, sdir: Path) -> dict:
    """The drafter records the judge verdict in difficulty.judged; fall back to the judge stage's judge.json."""
    if spec.difficulty.judged: return spec.difficulty.judged
    p = Path(sdir) / "judge.json"
    if p.is_file():
        try: return json.loads(p.read_text())
        except ValueError: return {}
    return {}


def auto_gates(spec: ScenarioSpec, sdir) -> list[str]:
    """Return failure reasons; empty means the validators approve."""
    fails: list[str] = []
    if _judgement(spec, sdir).get("keep") is not True: fails.append("judge did not accept (no non-derivable, consequential, thread-resolved gap)")
    f2p = set(spec.contract.fail_to_pass)
    if not any(f.discovery == Discovery.ON_REQUEST and set(f.tests) & f2p for f in spec.ledger):
        fails.append("no on-request fact is linked to a FAIL_TO_PASS test; interaction would not be load-bearing")
    withheld = next((b for b in spec.briefs if b.profile == "withheld"), None)
    if withheld is not None:
        targets = {o.target for o in spec.operations if o.type == OperationType.WITHHOLDING}
        for f in spec.ledger:
            if f.id in targets and f.source_excerpt and f.source_excerpt.strip().casefold() in withheld.text.casefold():
                fails.append(f"withheld brief still contains excerpt of {f.id}")
        title = next(b for b in spec.briefs if b.profile == "natural").text.strip().splitlines()[0][:60].casefold()
        if title and title not in withheld.text.casefold(): fails.append("withheld brief lost the natural title line")
    if not spec.controls.plausible_wrong or not any(c.cls == "incorrect-exploit" for c in spec.controls.tamper): fails.append("controls incomplete")
    return fails


def check_assets(sdir: Path, spec: ScenarioSpec) -> list[str]:
    reasons: list[str] = []; root = sdir.resolve()
    for asset in spec.assets.values():
        p = (sdir / asset.path).resolve()
        if not p.is_relative_to(root) or not p.is_file(): reasons.append(f"asset path {asset.path}"); continue
        data = p.read_bytes()
        if len(data) != asset.bytes or hashlib.sha256(data).hexdigest() != asset.sha256: reasons.append(f"asset mismatch {asset.path}")
    return reasons


def approve_scenario(ws: Workspace, scenario_id: str, auto: bool) -> dict:
    mode = "auto" if auto else "human"
    sdir = ws.scenario_dir(scenario_id); draft = sdir / "scenario.draft.json"
    if not draft.is_file(): return {"approved": False, "reasons": [f"missing {draft}"], "mode": mode}
    try:
        spec = ScenarioSpec.model_validate_json(draft.read_text())
    except (ValidationError, ValueError) as e:
        return {"approved": False, "reasons": [f"schema: {e}"], "mode": mode}
    reasons = check_assets(sdir, spec)
    if reasons: return {"approved": False, "reasons": reasons, "mode": mode}
    reasons = auto_gates(spec, sdir) if auto else []
    if reasons: return {"approved": False, "reasons": reasons, "mode": mode}
    atomic_json(sdir / "scenario.json", spec.model_dump(mode="json"))
    atomic_json(sdir / "approval.json", {"scenario_id": scenario_id, "spec_hash": spec.spec_hash, "version": spec.version,
                                         "approved_by": "auto-validators" if auto else "human-command", "review_needed": bool(auto),
                                         "gates": AUTO_GATES if auto else HUMAN_GATES,
                                         "approved_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    return {"approved": True, "spec_hash": spec.spec_hash, "mode": mode}
