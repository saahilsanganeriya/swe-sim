"""Stakeholder-visible view and the code-owned deny-list (A-P1-2): the reporter's permitted vocabulary is the natural issue only."""
from swesim.models import ScenarioSpec, minimal_spec_dict
from swesim.stakeholder.view import FIXED_FORBIDDEN, allowed_identifiers, forbidden_terms, visible_view

HIDDEN_TEST_PATCH = """diff --git a/tests/test_scales.py b/tests/test_scales.py
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/tests/test_scales.py
@@ -0,0 +1,8 @@
+import pytest
+from pkg.scales import Continuous, IntervalProperty
+
+
+def test_bool_series_maps_to_interval(bool_series):
+    out = Continuous()._setup(bool_series, IntervalProperty())
+    assert out == [1, 0, 0]  # a ValueError must never be raised here
"""


def _spec(tmp_path, brief="It breaks.", requirements=None, facts=()):
    d = minimal_spec_dict()
    d["briefs"][0]["text"] = brief
    if requirements is not None:
        d["requirements"] = requirements
    d["ledger"] += list(facts)
    (tmp_path / "assets").mkdir(exist_ok=True); (tmp_path / "assets" / "test.patch").write_text(HIDDEN_TEST_PATCH)
    return ScenarioSpec.model_validate(d)


def test_identifier_present_only_in_a_requirement_is_still_forbidden(tmp_path):
    """A requirement repeating a hidden-test identifier puts it in the stakeholder's context; it must not also exempt it."""
    spec = _spec(tmp_path, requirements=[{"id": "r01", "text": "Continuous scale setup must accept IntervalProperty input", "fact_id": "f01", "tests": ["t1"]}])
    terms = forbidden_terms(spec, tmp_path)
    assert "Continuous" in terms and "IntervalProperty" in terms
    assert "ValueError" in terms                     # said in requirement r01 / fact f01 of the minimal spec, never by the reporter


def test_identifier_present_only_in_a_visible_fact_is_still_forbidden(tmp_path):
    facts = [{"id": "f02", "text": "The mapping should use IntervalProperty semantics", "kind": "behavior", "provenance": "authored", "authored_rationale": "x", "discovery": "on-request"},
             {"id": "f03", "text": "Continuous is the class to patch", "kind": "interface", "provenance": "authored", "authored_rationale": "x", "discovery": "repo-derivable"}]
    terms = forbidden_terms(_spec(tmp_path, facts=facts), tmp_path)
    assert "IntervalProperty" in terms and "Continuous" in terms


def test_identifiers_the_reporter_wrote_are_allowed(tmp_path):
    brief = "Color mapping fails with boolean data\nCalling Continuous()._setup(...) raises ValueError on a bool Series."
    spec = _spec(tmp_path, brief=brief)
    terms = forbidden_terms(spec, tmp_path)
    assert "Continuous" not in terms and "ValueError" not in terms and "_setup" not in terms
    assert "IntervalProperty" in terms and "bool_series" in terms and "test_bool_series_maps_to_interval" in terms
    assert allowed_identifiers(spec) >= {"continuous", "_setup", "valueerror", "series"}
    assert "intervalproperty" not in allowed_identifiers(spec)
    assert all(t in terms for t in FIXED_FORBIDDEN) and terms == sorted(set(terms))
    for t in ("import", "from", "def", "diff", "git", "index", "mode", "dev", "null", "file"):
        assert t not in terms, t


def test_allowed_identifiers_ignore_facts_and_requirements_entirely(tmp_path):
    spec = _spec(tmp_path, requirements=[{"id": "r01", "text": "Use IntervalProperty", "fact_id": "f01", "tests": ["t1"]}])
    assert allowed_identifiers(spec) == {"breaks"}


def test_visible_view_still_carries_requirement_text(tmp_path):
    spec = _spec(tmp_path)
    assert visible_view(spec)["requirements"] == [{"id": "r01", "text": "Raise ValueError"}]
