"""The episode page shows the grader's verifier/submission.diff and falls back, labelled, to the legacy patch.diff."""
import json

from swesim.dashboard import data
from test_dashboard import site as site  # noqa: F401 - fixture

SUBMISSION = "diff --git a/pkg/new.py b/pkg/new.py\n--- a/pkg/new.py\n+++ b/pkg/new.py\n@@ -1 +1 @@\n-old\n+SUBMISSION_MARKER\n"
LEGACY = "diff --git a/pkg/old.py b/pkg/old.py\n--- a/pkg/old.py\n+++ b/pkg/old.py\n@@ -1 +1 @@\n-old\n+LEGACY_MARKER\n"


def point_trial_dir_inside_workspace(site, tid):
    ws = site["ws"]
    record_path = ws.root / "trials" / tid / "record.json"
    record = json.loads(record_path.read_text())
    trial_dir = ws.root / "jobs" / "runs" / tid / "task__abc"
    (trial_dir / "verifier").mkdir(parents=True)
    record["trial_dir"] = str(trial_dir)
    record_path.write_text(json.dumps(record))
    return trial_dir / "verifier"


def test_submission_diff_is_preferred_over_the_legacy_patch(site):
    tid = site["done"]
    verifier = point_trial_dir_inside_workspace(site, tid)
    (verifier / "submission.diff").write_text(SUBMISSION)
    (verifier / "patch.diff").write_text(LEGACY)
    html = site["client"].get(f"/trial/{tid}").text
    assert "SUBMISSION_MARKER" in html and "LEGACY_MARKER" not in html
    assert "verifier/submission.diff" in html and "legacy patch.diff" not in html and "submitted patch (1 file(s))" in html
    assert f"/trial/{tid}/file/submission.diff" in html and f"/trial/{tid}/file/patch.diff" in html  # both artifacts stay downloadable
    assert site["client"].get(f"/trial/{tid}/file/submission.diff").text == SUBMISSION  # public: no evaluator identity needed
    payload = site["client"].get(f"/api/trial/{tid}/timeline").json()
    assert payload["patch"] == SUBMISSION and payload["patch_file"] == "verifier/submission.diff"


def test_legacy_patch_is_shown_only_without_a_submission_diff_and_is_labelled(site):
    tid = site["done"]
    verifier = point_trial_dir_inside_workspace(site, tid)
    (verifier / "patch.diff").write_text(LEGACY)
    html = site["client"].get(f"/trial/{tid}").text
    assert "LEGACY_MARKER" in html and "legacy patch.diff" in html and "verifier/patch.diff" in html
    assert site["client"].get(f"/api/trial/{tid}/timeline").json()["patch_file"] == "verifier/patch.diff"


def test_no_patch_artifact_is_said_plainly(site):
    tid = site["done"]
    point_trial_dir_inside_workspace(site, tid)
    html = site["client"].get(f"/trial/{tid}").text
    assert "No patch artifact collected for this trial" in html and "verifier/submission.diff" in html
    assert site["client"].get(f"/api/trial/{tid}/timeline").json()["patch_file"] is None
    assert data.PATCH_FILES == ("submission.diff", "patch.diff") and "submission.diff" not in data.EVALUATOR_TRIAL_FILES
