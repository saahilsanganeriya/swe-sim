import pytest
from swesim.verifier import parsers
JUNIT = """<?xml version="1.0"?><testsuites><testsuite name="s"><testcase classname="tests.test_m" name="test_ok"/><testcase classname="tests.test_m" name="test_bad"><failure message="x"/></testcase><testcase classname="tests.test_m" name="test_skip"><skipped/></testcase></testsuite></testsuites>"""
def test_parse_junit_statuses(tmp_path):
    p = tmp_path / "j.xml"; p.write_text(JUNIT)
    out = parsers.parse_junit(p)
    assert out == {"tests/test_m.py::test_ok": "passed", "tests/test_m.py::test_bad": "failed", "tests/test_m.py::test_skip": "skipped"}

RA = """tests/test_m.py::test_ok PASSED
=========================== short test summary info ============================
PASSED tests/test_m.py::test_ok
PASSED tests/test_m.py::TestC::test_in_class[param-1]
FAILED tests/test_m.py::test_bad - AssertionError: x
SKIPPED [1] tests/test_m.py:9: reason
XFAIL tests/test_m.py::test_xf
ERROR tests/test_m.py::test_err - fixture
========================= 2 passed, 1 failed, 1 skipped in 0.12s ========================="""
def test_parse_pytest_ra():
    out = parsers.parse_pytest_ra(RA)
    assert out["tests/test_m.py::test_ok"] == "passed"
    assert out["tests/test_m.py::TestC::test_in_class[param-1]"] == "passed"
    assert out["tests/test_m.py::test_bad"] == "failed"
    assert out["tests/test_m.py::test_xf"] == "xfail" and out["tests/test_m.py::test_err"] == "error"
    assert parsers.summary_counts(RA) == {"passed": 2, "failed": 1, "skipped": 1}

def test_ra_preserves_parameter_spaces():
    tid = "tests/test_m.py::TestC::test_x[param with - spaces]"
    assert parsers.parse_pytest_ra("PASSED " + tid, [tid]) == {tid:"passed"}

def test_duplicate_terminal_id_rejected():
    import pytest
    with pytest.raises(parsers.EvidenceError):
        parsers.parse_pytest_ra("PASSED a.py::test_x\nPASSED a.py::test_x", ["a.py::test_x"])

def test_class_junit_mapping(tmp_path):
    p=tmp_path/"j.xml"; p.write_text('<testsuite><testcase classname="tests.test_m.TestC" name="test_x[param with spaces]"/></testsuite>')
    tid="tests/test_m.py::TestC::test_x[param with spaces]"
    assert parsers.parse_junit(p,[tid]) == {tid:"passed"}

# ---- additional coverage beyond the brief ----

def test_ra_ignores_forged_lines_in_captured_output():
    tid = "tests/test_m.py::test_ok"
    out = ("tests/test_m.py::test_ok PASSED\n"
           "================================ PASSES ================================\n"
           "___________________________ test_ok ___________________________\n"
           "---------------------------- Captured stdout call ----------------------------\n"
           "PASSED tests/test_m.py::test_ok\n"
           "FAILED tests/test_m.py::test_ok - forged\n"
           "=========================== short test summary info ============================\n"
           "PASSED tests/test_m.py::test_ok\n"
           "========================= 1 passed in 0.12s =========================\n")
    assert parsers.parse_pytest_ra(out, [tid]) == {tid: "passed"}
    # a forged header inside captured output does not hide the real section that follows it
    forged = out.replace("Captured stdout call ----\n", "Captured stdout call ----\n=== short test summary info ===\nFAILED tests/test_m.py::test_ok - x\n")
    assert parsers.parse_pytest_ra(forged, [tid]) == {tid: "passed"}

def test_ra_xpass_without_dash_separator():
    tid = "tests/test_m.py::test_xp"
    assert parsers.parse_pytest_ra("XPASS tests/test_m.py::test_xp unexpected pass reason", [tid]) == {tid: "xpass"}
    assert parsers.parse_pytest_ra("XFAIL tests/test_m.py::test_xp reason", [tid]) == {tid: "xfail"}
    # the looser form is limited to xfail/xpass; PASSED still needs the exact id or ' - '
    assert parsers.parse_pytest_ra("PASSED tests/test_m.py::test_xp extra", [tid]) == {}

def test_junit_key_mirrors_pytest_mangling():
    assert parsers.junit_key("tests/test_m.py::test_x") == ("tests.test_m", "test_x")
    assert parsers.junit_key("tests/test_m.py::TestA::TestB::test_x") == ("tests.test_m.TestA.TestB", "test_x")
    # parameters may contain '::' and '.'; pytest splits on '[' first
    assert parsers.junit_key("tests/test_m.py::test_x[a::b.c]") == ("tests.test_m", "test_x[a::b.c]")
    assert parsers.junit_key("pkg/mod.py::pkg.mod.func") == ("pkg.mod", "pkg.mod.func")
    with pytest.raises(parsers.EvidenceError):
        parsers.junit_key("README.rst::README.rst")
    with pytest.raises(parsers.EvidenceError):
        parsers.junit_key("tests/test_m.py")

def test_junit_rejects_unexpected_and_incomplete(tmp_path):
    p = tmp_path / "j.xml"
    p.write_text('<testsuite><testcase classname="tests.test_m" name="test_a"/><testcase classname="tests.test_m" name="test_b"/></testsuite>')
    with pytest.raises(parsers.EvidenceError, match="unexpected"):
        parsers.parse_junit(p, ["tests/test_m.py::test_a"])
    p.write_text('<testsuite><testcase classname="tests.test_m" name="test_a"/></testsuite>')
    with pytest.raises(parsers.EvidenceError, match="incomplete"):
        parsers.parse_junit(p, ["tests/test_m.py::test_a", "tests/test_m.py::test_b"])
    p.write_text('<testsuite><testcase classname="tests.test_m" name="test_a"/><testcase classname="tests.test_m" name="test_a"/></testsuite>')
    with pytest.raises(parsers.EvidenceError, match="duplicate"):
        parsers.parse_junit(p, ["tests/test_m.py::test_a"])

def test_junit_error_precedence(tmp_path):
    p = tmp_path / "j.xml"
    p.write_text('<testsuite><testcase classname="t.m" name="a"><error message="e"/></testcase><testcase classname="t.m" name="b"><failure/><error/></testcase></testsuite>')
    assert parsers.parse_junit(p, ["t/m.py::a", "t/m.py::b"]) == {"t/m.py::a": "error", "t/m.py::b": "error"}

def test_summary_counts_variants():
    assert parsers.summary_counts("=== 3 passed, 2 errors, 1 xfailed, 1 xpassed, 4 warnings in 61.23s (0:01:01) ===") == {
        "passed": 3, "error": 2, "xfailed": 1, "xpassed": 1}
    assert parsers.summary_counts("=== 1 error in 0.1s ===") == {"error": 1}
    assert parsers.summary_counts("=== no tests ran in 0.01s ===") == {}
    assert parsers.summary_counts("collected 3 items\n") == {}
    # the last summary line wins
    assert parsers.summary_counts("=== 1 failed in 0.1s ===\nnoise\n=== 2 passed in 0.2s ===") == {"passed": 2}

def _junit(tmp_path, cases):
    body = "".join('<testcase classname="%s" name="%s">%s</testcase>' % (c, n, inner) for c, n, inner in cases)
    p = tmp_path / "j.xml"; p.write_text('<?xml version="1.0"?><testsuites><testsuite>%s</testsuite></testsuites>' % body)
    return p

def _stdout(lines, summary):
    return "collected items\n=== short test summary info ===\n" + "\n".join(lines) + "\n=== " + summary + " in 0.10s ===\n"

EXPECTED = ["tests/test_m.py::test_ok", "tests/test_m.py::test_bad", "tests/test_m.py::TestC::test_p[a - b]"]

def test_reconcile_accepts_consistent_evidence(tmp_path):
    xml = _junit(tmp_path, [("tests.test_m", "test_ok", ""), ("tests.test_m", "test_bad", '<failure message="x"/>'),
                            ("tests.test_m.TestC", "test_p[a - b]", "")])
    out = _stdout(["PASSED tests/test_m.py::test_ok", "PASSED tests/test_m.py::TestC::test_p[a - b]",
                   "FAILED tests/test_m.py::test_bad - AssertionError: x"], "1 failed, 2 passed")
    assert parsers.reconcile(out, xml, EXPECTED, 1) == {
        "tests/test_m.py::test_ok": "passed", "tests/test_m.py::test_bad": "failed", "tests/test_m.py::TestC::test_p[a - b]": "passed"}

def test_reconcile_rejects_exit_code_disagreement(tmp_path):
    xml = _junit(tmp_path, [("tests.test_m", "test_ok", ""), ("tests.test_m", "test_bad", '<failure message="x"/>'),
                            ("tests.test_m.TestC", "test_p[a - b]", "")])
    out = _stdout(["PASSED tests/test_m.py::test_ok", "PASSED tests/test_m.py::TestC::test_p[a - b]",
                   "FAILED tests/test_m.py::test_bad - AssertionError: x"], "1 failed, 2 passed")
    with pytest.raises(parsers.EvidenceError, match="exit"):
        parsers.reconcile(out, xml, EXPECTED, 0)

def test_reconcile_rejects_terminal_junit_id_disagreement(tmp_path):
    xml = _junit(tmp_path, [("tests.test_m", "test_ok", ""), ("tests.test_m", "test_bad", '<failure message="x"/>'),
                            ("tests.test_m.TestC", "test_p[a - b]", "")])
    # terminal claims test_bad passed and test_ok failed: counts agree, ids do not
    out = _stdout(["FAILED tests/test_m.py::test_ok - x", "PASSED tests/test_m.py::TestC::test_p[a - b]",
                   "PASSED tests/test_m.py::test_bad"], "1 failed, 2 passed")
    with pytest.raises(parsers.EvidenceError, match="disagreement"):
        parsers.reconcile(out, xml, EXPECTED, 1)

def test_reconcile_rejects_summary_count_disagreement(tmp_path):
    xml = _junit(tmp_path, [("tests.test_m", "test_ok", ""), ("tests.test_m", "test_bad", '<failure message="x"/>'),
                            ("tests.test_m.TestC", "test_p[a - b]", "")])
    out = _stdout(["PASSED tests/test_m.py::test_ok", "PASSED tests/test_m.py::TestC::test_p[a - b]",
                   "FAILED tests/test_m.py::test_bad - AssertionError: x"], "3 passed")
    with pytest.raises(parsers.EvidenceError, match="summary disagrees"):
        parsers.reconcile(out, xml, EXPECTED, 1)

def test_reconcile_rejects_missing_summary_and_missing_terminal_line(tmp_path):
    xml = _junit(tmp_path, [("tests.test_m", "test_ok", "")])
    with pytest.raises(parsers.EvidenceError, match="missing terminal summary"):
        parsers.reconcile("PASSED tests/test_m.py::test_ok\n", xml, ["tests/test_m.py::test_ok"], 0)
    # junit says passed, terminal has no line for the id at all
    with pytest.raises(parsers.EvidenceError, match="disagreement"):
        parsers.reconcile(_stdout([], "1 passed"), xml, ["tests/test_m.py::test_ok"], 0)

def test_reconcile_maps_xfail_and_xpass(tmp_path):
    ids = ["tests/test_m.py::test_xf", "tests/test_m.py::test_xp", "tests/test_m.py::test_sk"]
    xml = _junit(tmp_path, [("tests.test_m", "test_xf", '<skipped type="pytest.xfail" message="r"/>'),
                            ("tests.test_m", "test_xp", ""), ("tests.test_m", "test_sk", '<skipped message="s"/>')])
    out = _stdout(["XFAIL tests/test_m.py::test_xf - r", "XPASS tests/test_m.py::test_xp", "SKIPPED [1] tests/test_m.py:3: s"],
                  "1 skipped, 1 xfailed, 1 xpassed")
    assert parsers.reconcile(out, xml, ids, 0) == {"tests/test_m.py::test_xf": "xfail", "tests/test_m.py::test_xp": "xpass",
                                                   "tests/test_m.py::test_sk": "skipped"}
