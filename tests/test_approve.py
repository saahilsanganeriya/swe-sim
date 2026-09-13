"""Approval stage: code-owned gates in --auto mode, human freeze otherwise. Offline."""
import hashlib, json
from pathlib import Path

from swesim.models import ScenarioSpec, minimal_spec_dict
from swesim.paths import Workspace
from swesim.stages.approve import approve_scenario, auto_gates

AUTO_GATES = ["schema", "assets", "judge-accept", "on-request-fact-linked-to-f2p", "withholding-leak", "controls"]


def _draft(tmp_path, d=None, write_assets=True):
    ws = Workspace(tmp_path / "ws"); d = d or minimal_spec_dict()
    d.setdefault("difficulty", {})["judged"] = d.get("difficulty", {}).get("judged", {"keep": True, "gaps": ["x"], "verified_rubric_score": 3})
    sdir = ws.scenario_dir(d["id"]); (sdir / "assets").mkdir(parents=True)
    for a in d["assets"].values():
        if write_assets: (sdir / a["path"]).write_bytes(b"x")
        a["sha256"] = hashlib.sha256(b"x").hexdigest(); a["bytes"] = 1
    (sdir / "scenario.draft.json").write_text(json.dumps(d))
    return ws, sdir, d


def test_auto_mode_approves_and_records_validators(tmp_path):
    ws, sdir, d = _draft(tmp_path)
    out = approve_scenario(ws, d["id"], auto=True)
    spec = ScenarioSpec.model_validate(d)
    assert out == {"approved": True, "spec_hash": spec.spec_hash, "mode": "auto"}
    assert ScenarioSpec.model_validate_json((sdir / "scenario.json").read_text()).spec_hash == spec.spec_hash
    ap = json.loads((sdir / "approval.json").read_text())
    assert ap["approved_by"] == "auto-validators" and ap["review_needed"] is True and ap["gates"] == AUTO_GATES
    assert ap["spec_hash"] == spec.spec_hash and ap["version"] == 1 and ap["scenario_id"] == d["id"] and "approved_at" in ap


def test_judge_rejection_blocks_and_writes_nothing(tmp_path):
    d = minimal_spec_dict(); d["difficulty"] = {"judged": {"keep": False}}
    ws, sdir, d = _draft(tmp_path, d)
    out = approve_scenario(ws, d["id"], auto=True)
    assert out["approved"] is False and out["mode"] == "auto" and any("judge" in r for r in out["reasons"])
    assert not (sdir / "scenario.json").exists() and not (sdir / "approval.json").exists()


def test_judge_json_fallback_when_spec_has_no_judgement(tmp_path):
    d = minimal_spec_dict(); d["difficulty"] = {}
    ws, sdir, d = _draft(tmp_path, d)
    d["difficulty"] = {}; (sdir / "scenario.draft.json").write_text(json.dumps(d))
    assert approve_scenario(ws, d["id"], auto=True)["approved"] is False
    (sdir / "judge.json").write_text(json.dumps({"gaps": ["g"], "verified_rubric_score": 3, "keep": True}))
    assert approve_scenario(ws, d["id"], auto=True)["approved"] is True


def test_on_request_fact_must_link_f2p(tmp_path):
    d = minimal_spec_dict(); d["ledger"][0]["discovery"] = "brief"
    ws, sdir, d = _draft(tmp_path, d)
    out = approve_scenario(ws, d["id"], auto=True)
    assert out["approved"] is False and any("on-request" in r for r in out["reasons"]) and not (sdir / "scenario.json").exists()


def test_withholding_leak_gate_is_stricter_than_schema(tmp_path):
    d = minimal_spec_dict(); d["briefs"][0]["text"] = "Import fails\nERROR42 \nmore"
    d["briefs"].append({"profile": "withheld", "text": "Import fails\nsee ERROR42", "provenance": "withheld"})
    d["ledger"].append({"id": "withheld-001", "text": "ERROR42", "kind": "reproduction", "provenance": "original-issue", "source_excerpt": "ERROR42 ",
                        "source_url": "https://example.com/issue", "discovery": "on-request"})
    d["operations"] = [{"type": "withholding", "target": "withheld-001", "rationale": "error_message"}]
    spec = ScenarioSpec.model_validate(d)  # schema passes: the excerpt with trailing space is not literally present
    assert any("withheld brief still contains excerpt of withheld-001" in r for r in auto_gates(spec, tmp_path))
    ws, sdir, d = _draft(tmp_path, d)
    out = approve_scenario(ws, d["id"], auto=True)
    assert out["approved"] is False and any("excerpt" in r for r in out["reasons"]) and not (sdir / "scenario.json").exists()


def test_schema_failure_is_reported_not_raised(tmp_path):
    d = minimal_spec_dict(); d["briefs"][0]["text"] = "Import fails\nSECRET"
    d["briefs"].append({"profile": "withheld", "text": "Import fails SECRET", "provenance": "withheld"})
    d["ledger"].append({"id": "withheld-001", "text": "SECRET", "kind": "reproduction", "provenance": "original-issue", "source_excerpt": "SECRET",
                        "source_url": "https://example.com/issue", "discovery": "on-request"})
    d["operations"] = [{"type": "withholding", "target": "withheld-001", "rationale": "r"}]
    ws, sdir, d = _draft(tmp_path, d)
    out = approve_scenario(ws, d["id"], auto=True)
    assert out["approved"] is False and out["reasons"][0].startswith("schema:") and not (sdir / "scenario.json").exists()
    out = approve_scenario(ws, "no-such-scenario", auto=False)
    assert out["approved"] is False and "scenario.draft.json" in out["reasons"][0]


def test_asset_mismatch_blocks_both_modes(tmp_path):
    ws, sdir, d = _draft(tmp_path)
    (sdir / "assets" / "wrong.diff").write_text("changed")
    for auto in (True, False):
        out = approve_scenario(ws, d["id"], auto=auto)
        assert out["approved"] is False and out["reasons"] == ["asset mismatch assets/wrong.diff"]
    (sdir / "assets" / "wrong.diff").unlink()
    assert approve_scenario(ws, d["id"], auto=True)["reasons"] == ["asset path assets/wrong.diff"]
    assert not (sdir / "scenario.json").exists()


def test_human_mode_records_command_and_skips_auto_gates(tmp_path):
    d = minimal_spec_dict(); d["difficulty"] = {"judged": {"keep": False}}
    ws, sdir, d = _draft(tmp_path, d)
    out = approve_scenario(ws, d["id"], auto=False)
    assert out["approved"] is True and out["mode"] == "human"
    ap = json.loads((sdir / "approval.json").read_text())
    assert ap["approved_by"] == "human-command" and ap["review_needed"] is False and ap["gates"] == ["schema", "assets"]
