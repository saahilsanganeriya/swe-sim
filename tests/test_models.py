import json, pytest
from pydantic import ValidationError
from swesim.models import (ScenarioSpec, RunConfig, canonical_hash, trial_id, minimal_spec_dict)

def test_roundtrip_hash():
    spec = ScenarioSpec.model_validate(minimal_spec_dict())
    assert ScenarioSpec.model_validate_json(spec.model_dump_json()).spec_hash == spec.spec_hash

def test_invalid_weights_and_overlap():
    for weight in (float("nan"), -1.0, 0.0):
        d = minimal_spec_dict(); d["groups"][0]["weight"] = weight
        with pytest.raises(ValidationError): ScenarioSpec.model_validate(d)
    d = minimal_spec_dict(); d["contract"]["pass_to_pass"] = ["t1"]
    with pytest.raises(ValidationError): ScenarioSpec.model_validate(d)

def test_minimal_spec_validates():
    spec = ScenarioSpec.model_validate(minimal_spec_dict())
    assert spec.spec_hash == canonical_hash(spec)

def test_duplicate_fact_ids_rejected():
    d = minimal_spec_dict(); d["ledger"].append(dict(d["ledger"][0]))
    with pytest.raises(ValidationError): ScenarioSpec.model_validate(d)

def test_author_only_fact_cannot_link_tests():
    d = minimal_spec_dict(); d["ledger"][0]["discovery"] = "author-only"; d["ledger"][0]["tests"] = ["t1"]
    with pytest.raises(ValidationError): ScenarioSpec.model_validate(d)

def test_fact_needs_excerpt_or_authored_rationale():
    d = minimal_spec_dict(); d["ledger"][0].pop("source_excerpt"); d["ledger"][0].pop("source_url")
    with pytest.raises(ValidationError): ScenarioSpec.model_validate(d)

def test_withheld_brief_provenance_and_leak():
    d=minimal_spec_dict();d['briefs'][0]['text']='Import fails\nTraceback: SECRET_ERROR'
    d['briefs'].append({'profile':'withheld','text':'Import fails','provenance':'withheld'})
    d['ledger'].append({'id':'withheld-001','text':'SECRET_ERROR','kind':'reproduction','provenance':'original-issue',
        'source_excerpt':'SECRET_ERROR','source_url':'https://example.com/issue','discovery':'on-request','tests':[]})
    d['operations']=[{'type':'withholding','target':'withheld-001','rationale':'Removed error_message'}]
    ScenarioSpec.model_validate(d)
    d['briefs'][1]['text']='Import fails SECRET_ERROR'
    with pytest.raises(ValidationError):ScenarioSpec.model_validate(d)

def test_empty_f2p_rejected():
    d = minimal_spec_dict(); d["contract"]["fail_to_pass"] = []
    with pytest.raises(ValidationError): ScenarioSpec.model_validate(d)

def test_groups_must_cover_f2p_exactly_once():
    d = minimal_spec_dict(); d["groups"][0]["tests"] = []
    with pytest.raises(ValidationError): ScenarioSpec.model_validate(d)

def test_extra_fields_forbidden():
    d = minimal_spec_dict(); d["budgets"] = {}
    with pytest.raises(ValidationError): ScenarioSpec.model_validate(d)

def test_asset_refs_must_exist():
    d = minimal_spec_dict(); d["controls"]["oracle"] = "missing.diff"
    with pytest.raises(ValidationError): ScenarioSpec.model_validate(d)

def test_trial_id_is_stable_and_distinct():
    a = trial_id("s", "interactive", "r", 0); b = trial_id("s", "interactive", "r", 1)
    assert a == trial_id("s", "interactive", "r", 0) and a != b and len(a) == 24

def test_runconfig_hash_changes_with_patience():
    r1 = RunConfig.default("interactive"); r2 = RunConfig.model_validate({**r1.model_dump(),"stakeholder":{**r1.stakeholder.model_dump(),"patience_profile":{**r1.stakeholder.patience_profile.model_dump(),"start":3}}})
    assert r1.run_config_hash != r2.run_config_hash
