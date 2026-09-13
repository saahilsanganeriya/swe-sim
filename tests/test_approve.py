"""Approval stage: code-owned gates in --auto mode, human freeze otherwise. Offline."""
import hashlib, json
import pytest

from swesim.models import ScenarioSpec, minimal_spec_dict
from swesim.paths import Workspace
from swesim.stages.approve import approve_scenario, auto_gates, fact_scope_failures
from test_audit_rubric import F2P_7283, P2P_7283, TEST_PATCH_7283, spec_dict_7283

AUTO_GATES = ["schema", "assets", "judge-accept", "on-request-fact-linked-to-f2p", "requirements-grounded", "fact-scope", "withholding-leak",
              "withheld-reconstructs", "controls"]


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


def test_schema_rejects_withholding_leaks_even_with_trailing_space(tmp_path):
    d = minimal_spec_dict(); d["briefs"][0]["text"] = "Import fails\nERROR42 \nmore"
    d["briefs"].append({"profile": "withheld", "text": "Import fails\nsee ERROR42", "provenance": "withheld"})
    d["ledger"].append({"id": "withheld-001", "text": "ERROR42", "kind": "reproduction", "provenance": "original-issue", "source_excerpt": "ERROR42 ",
                        "source_url": "https://example.com/issue", "discovery": "on-request"})
    d["operations"] = [{"type": "withholding", "target": "withheld-001", "rationale": "error_message"}]
    with pytest.raises(ValueError, match="reconstruct"):
        ScenarioSpec.model_validate(d)
    ws, sdir, d = _draft(tmp_path, d)
    out = approve_scenario(ws, d["id"], auto=True)
    assert out["approved"] is False and any("reconstruct" in r for r in out["reasons"]) and not (sdir / "scenario.json").exists()


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


def test_withheld_brief_must_be_the_natural_text_minus_exactly_the_recorded_excerpts(tmp_path):
    """B-P1-2: the gate recomputes the withheld brief from the recorded removals; an unrecorded deletion or an extra
    sentence fails approval even when every recorded excerpt is absent and the title survives."""
    natural = ("Numeric export drops values\n"
               "Expected values are numeric when the option is enabled.\n"
               "The exporter should keep the column order.\n"
               "Expected values are numeric and preserve all nulls.\n"
               "Seen on 1.2.")
    d = minimal_spec_dict(); d["briefs"][0]["text"] = natural
    d["ledger"].append({"id": "withheld-001", "text": "Reporter's original statement: Expected values are numeric when the option is enabled.", "kind": "behavior",
                        "provenance": "original-issue", "source_excerpt": "Expected values are numeric when the option is enabled.",
                        "source_url": "https://example.com/issue", "discovery": "on-request"})
    d["operations"] = [{"type": "withholding", "target": "withheld-001", "rationale": "Removed category: expected_output"}]
    # the second sentence vanished without a record (the old bug): rejected
    d["briefs"].append({"profile": "withheld", "text": "Numeric export drops values\nThe exporter should keep the column order.\nSeen on 1.2.", "provenance": "withheld"})
    with pytest.raises(ValueError, match="reconstruct"):
        ScenarioSpec.model_validate(d)
    # exactly the recorded excerpt removed: accepted
    d["briefs"][1]["text"] = "Numeric export drops values\nThe exporter should keep the column order.\nExpected values are numeric and preserve all nulls.\nSeen on 1.2."
    assert not any("recorded excerpts" in r for r in auto_gates(ScenarioSpec.model_validate(d), tmp_path))
    # both sentences recorded as their own removals: accepted with both gone
    d["ledger"].append({**d["ledger"][-1], "id": "withheld-002", "text": "Reporter's original statement: Expected values are numeric and preserve all nulls.",
                        "source_excerpt": "Expected values are numeric and preserve all nulls."})
    d["operations"].append({"type": "withholding", "target": "withheld-002", "rationale": "Removed category: expected_output"})
    d["briefs"][1]["text"] = "Numeric export drops values\nThe exporter should keep the column order.\nSeen on 1.2."
    assert not any("recorded excerpts" in r for r in auto_gates(ScenarioSpec.model_validate(d), tmp_path))
    ws, sdir, d2 = _draft(tmp_path, d)
    assert approve_scenario(ws, d2["id"], auto=True)["approved"] is True
    assert "withheld-reconstructs" in json.loads((sdir / "approval.json").read_text())["gates"]


def test_reconstruction_gate_reports_a_duplicate_removal_instead_of_crashing():
    from swesim.stages.approve import withheld_reconstruction_failures
    d = minimal_spec_dict()
    d["briefs"][0]["text"] = "Title\nRemove this sentence.\nKeep this sentence."
    d["briefs"].append({"profile": "withheld", "text": "Title\nKeep this sentence.", "provenance": "withheld"})
    d["ledger"].append({"id": "withheld-001", "text": "Remove this sentence.", "kind": "reproduction", "provenance": "original-issue",
                        "source_excerpt": "Remove this sentence.", "source_url": "https://example.com/issue", "discovery": "on-request"})
    d["operations"] = [{"type": "withholding", "target": "withheld-001", "rationale": "error_message"}]
    spec = ScenarioSpec.model_validate(d)
    assert withheld_reconstruction_failures(spec) == []
    invalid = spec.model_copy(update={"operations": spec.operations * 2})
    assert "no remaining occurrence" in withheld_reconstruction_failures(invalid)[0]


def test_requirements_must_be_grounded_in_a_visible_fact_or_the_thread(tmp_path):
    """A-P1-2 / B-P1-2: a requirement with no fact_id is accepted only when its text is a verbatim thread or issue excerpt."""
    d = minimal_spec_dict()
    d["thread"] = {"pr_url": "https://example.com/pr/1", "reviews": [{"body": "Please raise a  ValueError\nwhen the dim is missing."}], "pr_comments": [], "review_comments": [],
                   "linked_issues": [{"title": "t", "body": "Weighted mean silently ignores missing dims", "comments": [{"body": "Expected: raise ValueError."}]}]}
    d["requirements"] += [{"id": "r02", "text": "Bound the recovery scan so crafted files fail quickly", "fact_id": None, "tests": ["t1"]}]
    spec = ScenarioSpec.model_validate(d)
    fails = auto_gates(spec, tmp_path)
    assert any(r.startswith("requirement r02 is ungrounded") for r in fails) and not any("r01" in r for r in fails)
    d["requirements"][1]["text"] = "raise a ValueError when the dim is missing"          # verbatim review text (whitespace/case-insensitive)
    d["requirements"].append({"id": "r03", "text": "expected: raise valueerror.", "fact_id": None, "tests": []})   # verbatim issue-comment text
    d["requirements"].append({"id": "r04", "text": "Weighted mean silently ignores missing dims", "fact_id": None, "tests": []})   # verbatim issue body
    d["requirements"].append({"id": "r05", "text": "it breaks", "fact_id": None, "tests": []})                    # too short to ground anything
    assert any(r.startswith("requirement r05 is ungrounded") for r in auto_gates(ScenarioSpec.model_validate(d), tmp_path))
    d["requirements"].pop()
    assert not any("ungrounded" in r for r in auto_gates(ScenarioSpec.model_validate(d), tmp_path))
    ws, sdir, d2 = _draft(tmp_path, d)
    assert approve_scenario(ws, d2["id"], auto=True)["approved"] is True
    assert "requirements-grounded" in json.loads((sdir / "approval.json").read_text())["gates"]


def _draft_7283(tmp_path, d=None):
    """The pytest-7283 draft with its real hidden test patch as the test asset."""
    ws, sdir, d = _draft(tmp_path, d or spec_dict_7283())
    patch = TEST_PATCH_7283.encode()
    (sdir / "assets" / "test.patch").write_bytes(patch)
    d["assets"]["test.patch"].update(sha256=hashlib.sha256(patch).hexdigest(), bytes=len(patch))
    (sdir / "scenario.draft.json").write_text(json.dumps(d))
    return ws, sdir, d


def test_fact_scope_gate_rejects_a_visible_fact_naming_a_pass_to_pass_only_parametrisation(tmp_path):
    """pytest-7283 (2026-09-13): f05 names `pytest's skip marker`, the `@pytest.mark.skip` parametrisation only a PASS_TO_PASS
    test carries; acting on it regressed a pass-to-pass test in the full condition. The gate rejects with the fact id and the
    offending test id; f01 (the FAIL_TO_PASS parametrisation) and the author-only f09 pass. Nothing is written."""
    ws, sdir, d = _draft_7283(tmp_path)
    out = approve_scenario(ws, d["id"], auto=True)
    assert out["approved"] is False and out["mode"] == "auto"
    scope = [r for r in out["reasons"] if r.startswith("fact f05 is outside the contract")]
    assert len(scope) == 1 and P2P_7283[0] in scope[0] and "@pytest.mark.skip" in scope[0] and "FAIL_TO_PASS" in scope[0]
    assert not any("f01" in r or "f09" in r for r in out["reasons"]) and not any("on-request" in r for r in out["reasons"])
    assert not (sdir / "scenario.json").exists() and not (sdir / "approval.json").exists()
    # the gate reads the ids alone when the asset is not on disk yet (pre-check), and stays silent for the F2P-linked fact
    spec = ScenarioSpec.model_validate(d)
    fails = fact_scope_failures(spec, tmp_path / "nowhere")
    assert len(fails) == 1 and fails[0].startswith("fact f05") and P2P_7283[0] in fails[0]
    assert any(r.startswith("fact f05") for r in auto_gates(spec, sdir))


def test_fact_scope_gate_passes_a_draft_whose_facts_stay_within_the_contract(tmp_path):
    d = spec_dict_7283()
    d["ledger"] = [f for f in d["ledger"] if f["id"] != "f05"]           # f09 (author-only, same text) stays and is exempt
    ws, sdir, d = _draft_7283(tmp_path, d)
    assert fact_scope_failures(ScenarioSpec.model_validate(d), sdir) == []
    out = approve_scenario(ws, d["id"], auto=True)
    assert out["approved"] is True
    assert json.loads((sdir / "approval.json").read_text())["gates"] == AUTO_GATES and "fact-scope" in AUTO_GATES


def test_fact_scope_gate_rejects_an_identifier_found_only_in_a_pass_to_pass_body(tmp_path):
    d = spec_dict_7283()
    patch = TEST_PATCH_7283 + ("\n+\n+def test_pdb_teardown_called(testdir, monkeypatch):\n+    tracked = teardown_call_log(testdir)\n"
                               "+    assert tracked == [\"setUp:test_1\", \"tearDown:test_1\"]\n")
    d["ledger"][1] = {"id": "f05", "text": "The teardown_call_log must keep recording setUp and tearDown for tests that run", "kind": "behavior",
                      "provenance": "authored", "authored_rationale": "x", "discovery": "on-request", "tests": []}
    ws, sdir, d = _draft(tmp_path, d)
    (sdir / "assets" / "test.patch").write_bytes(patch.encode())
    d["assets"]["test.patch"].update(sha256=hashlib.sha256(patch.encode()).hexdigest(), bytes=len(patch.encode()))
    (sdir / "scenario.draft.json").write_text(json.dumps(d))
    out = approve_scenario(ws, d["id"], auto=True)
    assert out["approved"] is False
    reason = next(r for r in out["reasons"] if r.startswith("fact f05"))
    assert "teardown_call_log" in reason and "testing/test_unittest.py::test_pdb_teardown_called" in reason
