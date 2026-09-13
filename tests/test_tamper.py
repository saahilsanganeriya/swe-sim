import shutil
import subprocess

import pytest

from swesim import tamper


def test_root_conftest_noop_is_a_valid_diff_adding_conftest():
    d = tamper.root_conftest_noop()
    assert d.startswith("diff --git a/conftest.py b/conftest.py") and "pytest_runtest_call" in d and "new file mode" in d


def test_sitecustomize_forge_targets_startup_hook():
    d = tamper.sitecustomize_junit_forge()
    assert "a/sitecustomize.py" in d


def test_skip_marker_rewrites_test_file():
    tp = "diff --git a/tests/test_a.py b/tests/test_a.py\n--- a/tests/test_a.py\n+++ b/tests/test_a.py\n@@ -1 +1 @@\n-x\n+def test_new():\n+    assert 1\n"
    d = tamper.skip_marker({"tests/test_a.py": "def test_old():\n    assert True\n"})
    assert "pytestmark=pytest.mark.skip" in d and "a/tests/test_a.py" in d


def test_root_conftest_noop_appends_to_existing_conftest():
    d = tamper.root_conftest_noop("import os\n")
    assert "new file mode" not in d and "--- a/conftest.py" in d and "+++ b/conftest.py" in d and "-import os" not in d


def test_file_diff_marks_missing_final_newline():
    d = tamper.file_diff("t.py", "x = 1", "x = 1\nimport pytest\n")
    assert "\\ No newline at end of file" in d and "-x = 1\n" in d and "+x = 1\n" in d


def test_skip_marker_ignores_files_missing_on_base():
    assert tamper.skip_marker({"tests/test_new.py": None}) == ""


def test_all_controls_drops_empty_skip_marker_when_no_base_test_file():
    controls = tamper.all_controls({"tests/test_new.py": None, "conftest.py": None, "sitecustomize.py": None})
    assert set(controls) == {"tamper-root-conftest", "tamper-sitecustomize"}
    assert all(controls.values())
    with_base = tamper.all_controls({"tests/test_a.py": "def test_a():\n    pass\n", "conftest.py": "import os\n"})
    assert set(with_base) == {"tamper-root-conftest", "tamper-sitecustomize", "tamper-skip-marker"}
    assert "a/tests/test_a.py" in with_base["tamper-skip-marker"] and "conftest" not in with_base["tamper-skip-marker"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_generated_diffs_apply_with_git(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tests").mkdir()
    (repo / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    (repo / "conftest.py").write_text("import os")  # no final newline on purpose
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    base_files = {"tests/test_a.py": (repo / "tests" / "test_a.py").read_text(), "conftest.py": (repo / "conftest.py").read_text(), "sitecustomize.py": None}
    for name, diff in tamper.all_controls(base_files).items():
        patch = tmp_path / f"{name}.diff"
        patch.write_text(diff)
        proc = subprocess.run(["git", "-C", str(repo), "apply", "--check", str(patch)], capture_output=True, text=True)
        assert proc.returncode == 0, (name, proc.stderr)
