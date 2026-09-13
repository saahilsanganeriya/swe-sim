"""Task-sense rubric: the label models (interfaces §9.1) and the mechanical labels computed without an LLM.

Offline and deterministic. Transcript events mirror the stakeholder service's frozen transcript; trajectories mirror
Harbor's ATIF ``agent/trajectory.json`` (the same shapes ``swesim.stages.export`` reads).
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from swesim.audit import rubric as R
from swesim.stakeholder.session import REFUSAL

T0 = 1789293600.0  # 2026-09-13T10:00:00Z


def step(i: int, command: str, seconds: int, source: str = "agent", observation: str = "") -> dict:
    return {"step_id": i, "timestamp": f"2026-09-13T10:{seconds // 60:02d}:{seconds % 60:02d}Z", "source": source,
            "message": "...", "tool_calls": [{"tool_call_id": f"c{i}", "function_name": "bash", "arguments": {"command": command}}],
            "observation": {"results": [{"source_call_id": f"c{i}", "content": observation}]}}


def traj(*steps: dict) -> dict:
    return {"schema_version": "ATIF-v1.4", "session_id": "s", "agent": {"name": "mini-swe-agent"},
            "steps": [{"step_id": 0, "timestamp": "2026-09-13T09:59:00Z", "source": "user", "message": "Fix it."}, *steps]}


def ask(seq: int, question: str, reply: str, fact_ids: list[str], remaining: int, t: float, *, exchange: str = "ask",
        tier: str = "full", regenerated: bool | None = False, redacted: list[str] | None = None) -> list[dict]:
    user = {"kind": "message", "ts": t, "seq": seq, "role": "user", "actor": "coding-agent", "content": question,
            "request_id": f"r{seq}", "exchange": exchange}
    asst = {"kind": "message", "ts": t + 1, "seq": seq + 1, "role": "assistant", "actor": "stakeholder", "content": reply,
            "request_id": f"r{seq}", "exchange": exchange, "disclosed_fact_ids": fact_ids, "tier": tier,
            "patience_remaining": remaining, "tool_calls": [], "reviewed_diff_sha256": None, "delivery": "generated"}
    if regenerated is not None:  # stakeholder v2 annotates every reply; v1 replies carry no filter keys
        asst.update(regenerated=regenerated, redacted_terms=list(redacted or []), redaction_count=len(redacted or []),
                    withheld_sentences=[])
    return [user, asst]


def transcript(*events: list[dict], start: int = 6) -> dict:
    flat = [e for group in events for e in group]
    end = min((e["patience_remaining"] for e in flat if e["role"] == "assistant"), default=start)
    return {"trial_id": "t" * 24, "events": [{"kind": "session", "seq": 0, "ts": T0 - 10}, *flat],
            "patience_start": start, "patience_end": end, "frozen": True, "transcript_hash": "h" * 64}


# ---------------------------------------------------------------- rubric as data and the label models

def test_rubric_version_and_definitions_cover_every_label():
    assert R.RUBRIC_VERSION == "2026-09-13.2"
    for label in ("coherent", "fair_tests", "plausible_withholding", "facts_within_contract", "specific", "decision_oriented",
                  "before_implementation", "redundant", "fishing", "targeted", "volunteered", "test_or_identifier_disclosed", "behaviour_level"):
        assert label in R.RUBRIC and R.RUBRIC[label].strip()
    text = R.rubric_text()
    assert "coherent" in text and "behaviour_level" in text


def test_label_models_match_interfaces_and_forbid_extras():
    q = R.QuestionLabel(seq=1, specific=True, decision_oriented=True, before_implementation=True, redundant=False, fishing=False,
                        reason="asks which exception type to raise")
    assert set(q.model_dump()) >= {"seq", "specific", "decision_oriented", "before_implementation", "redundant", "fishing", "reason"}
    r = R.ReplyLabel(seq=2, targeted=True, volunteered=[], test_or_identifier_disclosed=False, behaviour_level=True, reason="ok")
    assert set(r.model_dump()) >= {"seq", "targeted", "volunteered", "test_or_identifier_disclosed", "behaviour_level", "reason"}
    with pytest.raises(ValidationError):
        R.QuestionLabel(seq=1, specific=True, decision_oriented=True, before_implementation=True, redundant=False, fishing=False,
                        reason="x", extra_field=1)
    s = R.ScenarioAudit(scenario_id="a-b", spec_hash="0" * 64, coherent=True, fair_tests=False, plausible_withholding=None,
                        reasons={"coherent": "c", "fair_tests": "f", "plausible_withholding": "no withheld brief"},
                        unfair_tests=["tests/t.py::test_x"], model="anthropic/claude-opus-5", audited_at="2026-09-13T10:00:00+00:00",
                        rubric_version=R.RUBRIC_VERSION, inputs_hash="1" * 64)
    assert set(s.model_dump()) >= {"coherent", "fair_tests", "plausible_withholding", "reasons", "model", "audited_at", "rubric_version"}
    # additive (interfaces §9.1): the fact-scope label defaults so older audit files still validate
    assert s.facts_within_contract is None and s.fact_scope == []
    scoped = s.model_copy(update={"facts_within_contract": False, "fact_scope": [R.FactScope(
        fact_id="f05", within_contract=False, reason="names a PASS_TO_PASS-only parametrisation", offending_tests=["t0"], markers=["@x"])]})
    dumped = scoped.model_dump()
    assert dumped["facts_within_contract"] is False and dumped["fact_scope"][0] == {
        "fact_id": "f05", "within_contract": False, "reason": "names a PASS_TO_PASS-only parametrisation", "offending_tests": ["t0"], "markers": ["@x"]}
    with pytest.raises(ValidationError):
        R.FactScope(fact_id="f05", within_contract="no", reason="x")
    t = R.TrialAudit(trial_id="t" * 24, scenario_id="a-b", spec_hash="0" * 64, stakeholder_version="v2", legacy=False,
                     questions=[q], replies=[r], asked_before_implementing=True, useful_question_rate=1.0,
                     patience=None, trajectory_source=None, model="anthropic/claude-opus-5",
                     audited_at="2026-09-13T10:00:00+00:00", rubric_version=R.RUBRIC_VERSION, inputs_hash="1" * 64)
    assert set(t.model_dump()) >= {"questions", "replies", "asked_before_implementing", "useful_question_rate", "model",
                                   "audited_at", "rubric_version"}
    with pytest.raises(ValidationError):  # labels are booleans or unknown, never strings
        R.ReplyLabel(seq=2, targeted="yes", volunteered=[], test_or_identifier_disclosed=False, behaviour_level=True, reason="x")


# ---------------------------------------------------------------- edit detection and asked-before-implementing

@pytest.mark.parametrize("command,edit", [
    ("cd /testbed && sed -n '340,365p' seaborn/_core/scales.py", False),
    ("cd /testbed && grep -rn 'def variable_type' seaborn/", False),
    ("cd /testbed && python -c 'import seaborn; print(1)' 2>&1 || echo no", False),
    ("cd /testbed && cat seaborn/_core/scales.py | head -330 > /tmp/scales_part1.py", False),           # scratch copy
    ("cd /testbed && cat > /tmp/test_fix.py << 'EOF'\nprint('probe')\nEOF\npython /tmp/test_fix.py", False),  # scratch script
    ("curl -sL -o /tmp/test.pdf https://example.com/x.pdf && ls -la /tmp/test.pdf", False),
    ("cd /app && git stash && python -m pytest tests -q && git stash pop", False),
    ("cd /app && git diff && git status", False),
    ("cd /testbed && sed -i '338d' seaborn/_core/scales.py", True),
    ("cd /testbed && sed -i.bak 's/KeyError/ValueError/' pkg/x.py", True),
    ("cd /testbed && cat > /tmp/fix.py << 'EOF'\nwith open('/testbed/seaborn/_core/scales.py', 'w') as f:\n    f.write(src)\nEOF\npython /tmp/fix.py", True),
    ("cd /app && python3 - <<'EOF'\nwith open('pypdf/_reader.py') as f:\n    c = f.read()\nwith open('pypdf/_reader.py', 'w') as f:\n    f.write(c.replace(a, b))\nEOF", True),
    ("cd /app && python3 -c \"from pathlib import Path; p=Path('pypdf/_reader.py'); p.write_text(p.read_text().replace('a','b'))\"", True),
    ("cd /testbed && cat > seaborn/_core/new_module.py << 'EOF'\nx = 1\nEOF", True),
    ("cd /testbed && echo 'x = 1' >> seaborn/_core/scales.py", True),
    ("cd /testbed && git apply /tmp/fix.patch", True),
    ("cd /testbed && patch -p1 < /tmp/fix.patch", True),
    ("cd /testbed && cp /tmp/scales.py seaborn/_core/scales.py", True),
    ("cd /testbed && cp seaborn/_core/scales.py /tmp/backup.py", False),
    ("cd /testbed && tee seaborn/_core/scales.py < /tmp/new.py", True),
    ("cd /testbed && perl -pi -e 's/a/b/' seaborn/_core/scales.py", True),
    # comparisons, type annotations and placeholders inside code are not redirections
    ("cd /testbed && python3 - <<'EOF'\nfor i, line in enumerate(lines):\n    if i > 320:\n        print(line)\nEOF", False),
    ("cd /testbed && cat > /tmp/fix.py <<'EOF'\ndef _setup(self, data) -> Scale:\n    return self\nEOF\npython /tmp/fix.py", False),
    ("ask-user \"Should a valid 'startxref <offset>' entry be required?\"", False),
    # PDF dictionaries inside a heredoc are not redirections
    ("cat > /tmp/test_issue.py << 'EOF'\npdf = b\"\"\"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n\"\"\"\nEOF\npython /tmp/test_issue.py", False),
    ("cd /app && python3 -c \"content = b'''<< /Size 3 /Root 1 0 R >>\nstartxref'''\nwith open('/tmp/broken.pdf','wb') as f:\n    f.write(content)\"", False),
])
def test_is_edit_command(command, edit):
    assert R.is_edit_command(command) is edit
    assert (R.edit_evidence(command) is not None) is edit


def test_first_edit_restricted_to_the_submission_paths_ignores_scratch_scripts():
    t = traj(step(1, "cat > repro.py << 'EOF'\nprint(1)\nEOF\npython repro.py", 1),            # a reproduction script in the tree
             step(2, "ask-user 'Which exception?'", 5),
             step(3, "sed -i s/KeyError/ValueError/ pkg/x.py", 20))
    assert R.first_edit_step(t) == 1                                        # without the submission's paths any write counts
    assert R.first_edit(t, ["pkg/x.py"]) == (3, "sed in-place")             # with them, only writes touching those files
    assert R.first_edit_step(t, ["other/y.py"]) is None
    tr = transcript(ask(1, "Which exception?", "ValueError.", ["f01"], 5, T0 + 5))
    assert R.asked_before_implementing(tr, t) is False and R.asked_before_implementing(tr, t, ["pkg/x.py"]) is True
    assert R.questions_before_implementation(tr, t, ["pkg/x.py"]) == {1: True}
    patching = traj(step(1, "git apply /tmp/fix.patch", 1), step(2, "ask-user 'Which exception?'", 5))
    assert R.first_edit(patching, ["pkg/x.py"]) == (1, "git apply")          # patch commands count whatever they touch


def test_non_bash_editor_tools_count_as_edits():
    s = {"step_id": 3, "timestamp": "2026-09-13T10:00:30Z", "source": "agent",
         "tool_calls": [{"tool_call_id": "c3", "function_name": "str_replace_editor",
                         "arguments": {"command": "str_replace", "path": "/testbed/pkg/x.py", "old_str": "a", "new_str": "b"}}]}
    assert R.first_edit_step(traj(s)) == 1
    view = {**s, "tool_calls": [{"tool_call_id": "c3", "function_name": "str_replace_editor",
                                 "arguments": {"command": "view", "path": "/testbed/pkg/x.py"}}]}
    assert R.first_edit_step(traj(view)) is None


def test_asked_before_implementing_uses_trajectory_order():
    tr = transcript(ask(1, "Which exception should be raised?", "ValueError.", ["f01"], 5, T0 + 5))
    before = traj(step(1, "cat pkg/x.py", 1), step(2, "ask-user 'Which exception should be raised?'", 5, observation="ValueError."),
                  step(3, "sed -i s/KeyError/ValueError/ pkg/x.py", 20))
    after = traj(step(1, "sed -i s/KeyError/ValueError/ pkg/x.py", 1), step(2, "ask-user 'Which exception should be raised?'", 5))
    assert R.asked_before_implementing(tr, before) is True
    assert R.asked_before_implementing(tr, after) is False
    assert R.first_ask_step(before) == 2 and R.first_edit_step(before) == 3


def test_asked_before_implementing_without_questions_is_false_and_without_trajectory_unknown():
    tr = transcript(ask(1, "Which exception?", "ValueError.", ["f01"], 5, T0 + 5))
    assert R.asked_before_implementing(transcript(), traj(step(1, "sed -i s/a/b/ x.py", 1))) is False
    assert R.asked_before_implementing(tr, None) is None
    assert R.asked_before_implementing(tr, {"steps": []}) is None


def test_asked_before_implementing_falls_back_to_timestamps_when_the_shim_call_is_not_in_the_trajectory():
    # The stakeholder recorded a question at T0+5 but the trajectory shows no ask-user invocation (a truncated trajectory);
    # the first edit is at T0+20, so the question came first.
    tr = transcript(ask(1, "Which exception?", "ValueError.", ["f01"], 5, T0 + 5))
    edits_later = traj(step(1, "cat pkg/x.py", 1), step(2, "sed -i s/a/b/ pkg/x.py", 20))
    edits_first = traj(step(1, "sed -i s/a/b/ pkg/x.py", 1))
    assert R.asked_before_implementing(tr, edits_later) is True
    assert R.asked_before_implementing(tr, edits_first) is False


def test_questions_before_implementation_are_labelled_per_question_by_text_match():
    tr = transcript(ask(1, "Which exception should be raised?", "ValueError.", ["f01"], 5, T0 + 5),
                    ask(3, "And the message text?", "Say 'bad key'.", ["f02"], 4, T0 + 40))
    t = traj(step(1, "ask-user 'Which exception should be raised?'", 5), step(2, "sed -i s/a/b/ pkg/x.py", 20),
             step(3, "ask-user 'And the message text?'", 40))
    assert R.questions_before_implementation(tr, t) == {1: True, 3: False}
    assert R.questions_before_implementation(tr, None) == {1: None, 3: None}


def test_confirmations_are_not_questions_but_edit_after_confirm_is_visible():
    tr = transcript(ask(1, "Booleans map to 0/1?", "Yes.", ["f02"], 3, T0 + 5, exchange="confirm"))
    t = traj(step(1, "confirm-behavior 'Booleans map to 0/1'", 5), step(2, "sed -i s/a/b/ pkg/x.py", 20))
    assert R.asked_before_implementing(tr, t) is False  # nothing was asked; a confirmation is not a question
    assert R.first_ask_step(t) is None


# ---------------------------------------------------------------- redundancy, disclosure targeting, identifier leaks

BRIEF = ("Color mapping fails with boolean data\n"
         "```python\nso.Plot([\"a\", \"b\"], [1, 2], color=[True, False]).add(so.Bar())\n```\n"
         "TypeError: numpy boolean subtract, the `-` operator, is not supported, use the bitwise_xor instead.\n"
         "I'm on seaborn 0.12.2 with numpy 1.24.")


def test_redundant_with_brief_detects_restated_sentences_only():
    assert R.redundant_with_brief("Does the TypeError say numpy boolean subtract, the - operator, is not supported?", BRIEF) is True
    assert R.redundant_with_brief("Which seaborn and numpy versions are you on, 0.12.2 and 1.24?", BRIEF) is True
    assert R.redundant_with_brief("Should boolean values be treated as categorical, or as numeric 0/1 on a continuous scale?", BRIEF) is False
    assert R.redundant_with_brief("Which version?", BRIEF) is False          # too little content to call redundant
    assert R.redundant_with_brief("", BRIEF) is False


FACTS = [{"id": "f02", "text": "Boolean data should be upcast to 0/1 floats in numeric contexts, consistent with numpy conventions.",
          "kind": "behavior", "discovery": "on-request"},
         {"id": "f06", "text": "When recovery succeeds a warning is logged noting that a previous revision's pointer was used.",
          "kind": "behavior", "discovery": "on-request"},
         {"id": "f09", "text": "SECRET author-only rationale about the oracle.", "kind": "context", "discovery": "author-only"}]


def test_disclosure_targeted_separates_targeted_volunteered_and_undeclared_facts():
    q = "Should boolean data be treated as numeric 0/1 or as categorical?"
    reply = "Booleans act like 0 and 1 in numeric contexts. Also, when recovery succeeds a warning should be logged."
    d = R.disclosure_targeted(reply, q, ["f02", "f06"], FACTS)
    assert d.targeted is False and d.volunteered == ["f06"] and d.undeclared == []
    d = R.disclosure_targeted(reply, q, ["f02"], FACTS)
    assert d.targeted is True and d.volunteered == [] and d.undeclared == ["f06"]   # conveyed without declaring it
    d = R.disclosure_targeted("Yes, like 0 and 1.", q, [], FACTS)
    assert d.targeted is None and d.volunteered == [] and d.undeclared == []
    d = R.disclosure_targeted("Yes.", q, ["f99"], FACTS)                            # unknown id: cannot be targeted
    assert d.targeted is False and d.volunteered == ["f99"]


def test_conveyed_requirements_finds_test_only_requirements_restated_by_a_reply():
    reqs = [{"id": "r01", "text": "Recovery must only happen in non-strict mode; strict mode must raise.", "fact_id": "f03"},
            {"id": "r03", "text": "Bound the backward recovery scan so crafted files without a recoverable pointer fail quickly "
                                  "rather than scanning indefinitely.", "fact_id": None}]
    reply = ("The remaining requirement is bounding the backward scan so a crafted file without a recoverable pointer fails "
             "quickly rather than scanning indefinitely.")
    assert R.conveyed_requirements(reply, reqs) == ["r03"]
    assert R.conveyed_requirements(reply, reqs, ungrounded_only=True) == ["r03"]
    assert R.conveyed_requirements("Only in non-strict mode; strict mode must still raise.", reqs, ungrounded_only=True) == []
    assert R.conveyed_requirements("Yes, that matches.", reqs) == []


def test_test_or_identifier_disclosed_uses_the_reply_filter_term_semantics():
    terms = ["test_interval_with_bools", "IntervalProperty", "test_", "regression test", "bounded"]
    assert R.test_or_identifier_disclosed("Use IntervalProperty for that.", terms) == ["IntervalProperty"]
    assert R.test_or_identifier_disclosed("Please add a regression test for it.", terms) == ["regression test"]
    assert R.test_or_identifier_disclosed("The scan is bounded.", terms) == ["bounded"]
    assert R.test_or_identifier_disclosed("The scan needs bounding.", terms) == []          # whole-token: a filter gap, not a hit
    assert R.test_or_identifier_disclosed("Then test_x passes.", terms) == ["test_"]
    assert R.test_or_identifier_disclosed("Booleans act like 0 and 1.", terms) == []
    assert R.test_or_identifier_disclosed("anything", []) == []
    # builtin names are never test-derived, whatever the deny-list says
    assert R.test_or_identifier_disclosed("Raise ValueError, not KeyError; all of them.", ["ValueError", "KeyError", "all", "bounded"]) == []
    # only code-like hits and the fixed test vocabulary are unambiguous evidence
    assert R.code_like_hits(["bounded", "IntervalProperty", "regression test", "raises", "test_x", "tests/", "scales.py", "_setup("]) == [
        "IntervalProperty", "regression test", "test_x", "tests/", "scales.py", "_setup("]


# ---------------------------------------------------------------- patience

def test_patience_spent_reconstructs_costs_refusals_and_duplicates():
    events = [*ask(1, "Which exception?", "ValueError.", ["f01"], 5, T0 + 5),
              *ask(3, "Does it match?", "Yes.", [], 3, T0 + 30, exchange="confirm"),
              *ask(5, "Which exception?", "ValueError.", ["f01"], 2, T0 + 60),                       # duplicate question
              *ask(7, "And the message?", REFUSAL, [], 2, T0 + 90, tier="refused")]
    p = R.patience_spent(transcript(*[events]))
    assert (p.start, p.end, p.spent) == (6, 2, 4)
    assert [(e["seq"], e["exchange"], e["cost"], e["remaining_after"]) for e in p.exchanges] == [
        (2, "ask", 1, 5), (4, "confirm", 2, 3), (6, "ask", 1, 2), (8, "ask", 0, 2)]
    assert p.refusals == 1 and p.duplicates == 1 and p.sensible is False


def test_patience_spent_is_sensible_when_nothing_is_wasted_and_handles_empty_transcripts():
    p = R.patience_spent(transcript(ask(1, "Which exception?", "ValueError.", ["f01"], 5, T0 + 5)))
    assert p.sensible is True and p.spent == 1 and p.refusals == 0 and p.duplicates == 0
    p = R.patience_spent(transcript())
    assert (p.start, p.end, p.spent, p.exchanges, p.sensible) == (6, 6, 0, [], True)


# ---------------------------------------------------------------- useful-question rate

def q(seq, **labels):
    base = dict(specific=True, decision_oriented=True, before_implementation=True, redundant=False, fishing=False, reason="r")
    return R.QuestionLabel(seq=seq, **{**base, **labels})


def test_useful_question_rate_counts_specific_decision_oriented_non_fishing_non_redundant_questions():
    assert R.useful_question_rate([q(1), q(3, fishing=True), q(5, redundant=True), q(7, specific=False)]) == 0.25
    assert R.useful_question_rate([]) is None
    assert R.useful_question_rate([q(1, specific=None)]) is None          # unknown labels never count as useful or useless
    assert R.useful_question_rate([q(1), q(3, decision_oriented=None)]) == 1.0


# ---------------------------------------------------------------- fact scope (pytest-dev--pytest-7283, measured 2026-09-13)

# The hosted scenario's contract: one FAIL_TO_PASS parametrisation; the sibling parametrisation and the rest of the file pass on base.
F2P_7283 = ["testing/test_unittest.py::test_pdb_teardown_skipped[@unittest.skip]"]
P2P_7283 = ["testing/test_unittest.py::test_pdb_teardown_skipped[@pytest.mark.skip]",
            "testing/test_unittest.py::test_simple_unittest",
            "testing/test_unittest.py::test_pdb_teardown_called",
            "testing/test_unittest.py::test_setUpModule",
            "testing/test_unittest.py::test_class_method_containing_test_issue1558",
            "testing/test_unittest.py::test_trace[True]"]
TEST_PATCH_7283 = '''diff --git a/testing/test_unittest.py b/testing/test_unittest.py
--- a/testing/test_unittest.py
+++ b/testing/test_unittest.py
@@ -1193,3 +1193,32 @@ def test_pdb_teardown_called(testdir, monkeypatch):
     assert tracked == ["setUp:test_1", "tearDown:test_1", "setUp:test_2", "tearDown:test_2"]
+
+
+@pytest.mark.parametrize("mark", ["@unittest.skip", "@pytest.mark.skip"])
+def test_pdb_teardown_skipped(testdir, monkeypatch, mark: str) -> None:
+    """With --pdb, setUp and tearDown should not be called for skipped tests."""
+    tracked = []  # type: List[str]
+    monkeypatch.setattr(pytest, "test_pdb_teardown_skipped", tracked, raising=False)
+
+    testdir.makepyfile(
+        """
+        import unittest
+        import pytest
+
+        class MyTestCase(unittest.TestCase):
+
+            def setUp(self):
+                pytest.test_pdb_teardown_skipped.append("setUp:" + self.id())
+
+            def tearDown(self):
+                pytest.test_pdb_teardown_skipped.append("tearDown:" + self.id())
+
+            {mark}("skipped for reasons")
+            def test_1(self):
+                pass
+
+        """.format(
+            mark=mark
+        )
+    )
+    result = testdir.runpytest_inprocess("--pdb")
+    result.stdout.fnmatch_lines("* 1 skipped in *")
+    assert tracked == []
'''
F01_7283 = "With --pdb, setUp and tearDown are not called for a unittest.TestCase test that is skipped with unittest.skip"
F05_7283 = ("The same skipped lifecycle behavior applies to unittest.TestCase methods skipped with unittest.skip and with "
            "pytest's skip marker")
FACTS_7283 = [{"id": "f01", "text": F01_7283, "discovery": "on-request"},
              {"id": "f05", "text": F05_7283, "discovery": "on-request"}]


def spec_dict_7283() -> dict:
    """A ScenarioSpec dict with the pytest-7283 contract, f01 (linked to the F2P test) and the out-of-contract f05; the
    author-only f09 restates f05 and must stay exempt."""
    from swesim.models import minimal_spec_dict
    d = minimal_spec_dict()
    d["id"] = "pytest-dev--pytest-7283"
    d["briefs"][0]["text"] = "unittest.TestCase.tearDown executed on skipped tests when running --pdb\nWith --pdb, tearDown runs for skipped tests."
    d["ledger"] = [
        {"id": "f01", "text": F01_7283, "kind": "behavior", "provenance": "pr-thread", "source_excerpt": "tearDown should not run",
         "source_url": "https://example.com/pr#c1", "discovery": "on-request", "tests": F2P_7283},
        {"id": "f05", "text": F05_7283, "kind": "behavior", "provenance": "authored", "authored_rationale": "judge gap resolved by tests",
         "discovery": "on-request", "tests": []},
        {"id": "f09", "text": F05_7283, "kind": "context", "provenance": "authored", "authored_rationale": "kept for the record",
         "discovery": "author-only"},
    ]
    d["requirements"] = [{"id": "r01", "text": F01_7283, "fact_id": "f01", "tests": F2P_7283}]
    d["contract"]["fail_to_pass"] = list(F2P_7283)
    d["contract"]["pass_to_pass"] = list(P2P_7283)
    d["groups"] = [{"id": "g01", "weight": 1.0, "tests": list(F2P_7283)}]
    return d


def test_parse_test_id_splits_path_names_and_parametrisation():
    ref = R.parse_test_id("testing/test_unittest.py::TestX::test_pdb_teardown_skipped[@pytest.mark.skip]")
    assert (ref.path, ref.classes, ref.name, ref.param) == ("testing/test_unittest.py", ["TestX"], "test_pdb_teardown_skipped", "@pytest.mark.skip")
    plain = R.parse_test_id("tests/test_x.py::test_simple")
    assert (plain.name, plain.param, plain.classes) == ("test_simple", None, [])
    assert R.parse_test_id("t1").name == "t1" and R.parse_test_id("a::b[x-[y]]").param == "x-[y]"


def test_test_bodies_split_a_test_patch_into_functions_with_their_decorators():
    bodies = R.test_bodies({"test.patch": TEST_PATCH_7283})
    assert set(bodies) == {"test_pdb_teardown_skipped"}
    body = bodies["test_pdb_teardown_skipped"]
    assert body.startswith('@pytest.mark.parametrize("mark", ["@unittest.skip", "@pytest.mark.skip"])\ndef test_pdb_teardown_skipped(')
    assert "assert tracked == []" in body and "diff --git" not in body and "\n+" not in body
    plain = R.test_bodies({"tests/test_x.py": "import x\n\ndef helper():\n    return 1\n\n\nclass TestA:\n    def test_one(self):\n        assert helper() == 1\n\n    def test_two(self):\n        assert legacy_offset == 2\n\ndef test_three():\n    pass\n"})
    assert set(plain) == {"test_one", "test_two", "test_three"}
    assert "legacy_offset" in plain["test_two"] and "legacy_offset" not in plain["test_one"]


def test_fact_scope_flags_f05_by_the_pass_to_pass_only_parametrisation_and_keeps_f01():
    """f05 names `pytest's skip marker`: the `@pytest.mark.skip` parametrisation, which only the PASS_TO_PASS sibling carries.
    f01 names the F2P parametrisation and the shared body only."""
    out = R.fact_scope_mechanical(FACTS_7283, F2P_7283, P2P_7283, {"test.patch": TEST_PATCH_7283})
    by_id = {fs.fact_id: fs for fs in out}
    assert set(by_id) == {"f01", "f05"}
    f05 = by_id["f05"]
    assert f05.within_contract is False and f05.offending_tests == [P2P_7283[0]] and f05.markers == ["@pytest.mark.skip"]
    assert P2P_7283[0] in f05.reason and "@pytest.mark.skip" in f05.reason and "FAIL_TO_PASS" in f05.reason
    f01 = by_id["f01"]
    assert f01.within_contract is None and f01.offending_tests == [] and f01.markers == []
    assert R.facts_within_contract(out) is False
    assert R.facts_within_contract([f01]) is None
    assert R.facts_within_contract([f01.model_copy(update={"within_contract": True})]) is True
    assert R.facts_within_contract([]) is True


def test_fact_scope_does_not_fire_on_generic_or_f2p_parametrisations_or_shared_bodies():
    # [True] is a generic parametrisation; "pytest" and "mark" alone do not cover the whole `@pytest.mark.skip` id
    facts = [{"id": "a", "text": "When the option is true the teardown must not run", "discovery": "on-request"},
             {"id": "b", "text": "pytest should not call tearDown for skipped unittest tests", "discovery": "on-request"},
             {"id": "c", "text": "The skip applies to unittest.skip on TestCase methods", "discovery": "brief"}]
    out = R.fact_scope_mechanical(facts, F2P_7283, P2P_7283, {"test.patch": TEST_PATCH_7283})
    assert all(fs.within_contract is None and not fs.offending_tests for fs in out)
    # without any test text the parametrisation check still works from the ids alone
    out = R.fact_scope_mechanical(FACTS_7283, F2P_7283, P2P_7283, {})
    assert {fs.fact_id: fs.within_contract for fs in out} == {"f01": None, "f05": False}
    # author-only facts are never scoped: they are not stakeholder-visible
    out = R.fact_scope_mechanical(FACTS_7283 + [{"id": "f09", "text": F05_7283, "discovery": "author-only"}], F2P_7283, P2P_7283, {})
    assert [fs.fact_id for fs in out] == ["f01", "f05"]


def test_fact_scope_flags_identifiers_that_exist_only_in_a_pass_to_pass_body():
    patch = ("diff --git a/tests/test_x.py b/tests/test_x.py\n--- a/tests/test_x.py\n+++ b/tests/test_x.py\n@@ -1,4 +1,12 @@\n"
             "+def test_reads_legacy_header(loader):\n+    assert loader.read(legacy_header_offset=12).ok\n+\n"
             "+def test_rejects_missing_key(loader):\n+    with pytest.raises(ValueError):\n+        loader.load(strict_mode=True)\n")
    f2p, p2p = ["tests/test_x.py::test_rejects_missing_key"], ["tests/test_x.py::test_reads_legacy_header", "tests/test_x.py::test_other"]
    facts = [{"id": "f1", "text": "Loading with strict_mode raises ValueError for a missing key", "discovery": "on-request"},
             {"id": "f2", "text": "The legacy_header_offset of 12 bytes must still be honoured", "discovery": "on-request"},
             {"id": "f3", "text": "Reading the legacy header offset should keep working", "discovery": "on-request"}]
    out = {fs.fact_id: fs for fs in R.fact_scope_mechanical(facts, f2p, p2p, {"test.patch": patch})}
    assert out["f1"].within_contract is None and out["f1"].markers == []
    assert out["f2"].within_contract is False and out["f2"].markers == ["legacy_header_offset"]
    assert out["f2"].offending_tests == ["tests/test_x.py::test_reads_legacy_header"] and "legacy_header_offset" in out["f2"].reason
    # plain words shared with a PASS_TO_PASS-only body are evidence for the reading pass, never a mechanical verdict
    assert out["f3"].within_contract is None and "test_reads_legacy_header" in out["f3"].reason and out["f3"].offending_tests == []


def test_fact_scope_one_word_parametrisation_marker_needs_the_exact_word():
    """pypdf-3826 v3 (2026-09-13): f09 said the recovery "emits the warning ..." and the prefix rule read the plain word `warning`
    as naming `warning_msgs0`, a parametrisation of the unrelated PASS_TO_PASS test test_get_images_raw. A marker made of one
    non-generic word is named only by that exact word; multi-word markers keep the prefix rule (f05 above)."""
    f2p = ["tests/test_reader.py::test_startxref_corrupt_trailing_pointer"]
    p2p = ["tests/test_reader.py::test_get_images_raw[True-False--1-False-warning_msgs0]"]
    facts = [{"id": "f09", "text": "Successful non-strict recovery emits the warning `found startxref pointing to a previous revision`.",
              "discovery": "on-request"},
             {"id": "f10", "text": "The warning_msgs0 case must keep passing.", "discovery": "on-request"}]
    out = {fs.fact_id: fs for fs in R.fact_scope_mechanical(facts, f2p, p2p, {})}
    assert out["f09"].within_contract is None and out["f09"].offending_tests == []
    assert out["f10"].within_contract is False and out["f10"].markers == ["True-False--1-False-warning_msgs0"]
