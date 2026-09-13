"""SWE-bench annotated source: pool A (underspecified, valid tests) from the SWE-bench Verified ensembled annotations.

Pool A rule: underspecified >= 2, false_negative <= 1, other_major_issues == 0 (109 instances). Trial-eligible repos are
the pytest-run ones; django (custom runner, Trac) and sympy (custom runner) are excluded.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CSV_PATH = ROOT / "data" / "annotations" / "ensembled_annotations_public.csv"
REPO_DIR = "/testbed"
TEST_PYTHON = "/opt/miniconda3/envs/testbed/bin/python"
DATASET = "princeton-nlp/SWE-bench"
PYTEST_REPOS = {"sphinx-doc/sphinx", "pytest-dev/pytest", "matplotlib/matplotlib", "scikit-learn/scikit-learn", "pydata/xarray",
                "psf/requests", "mwaskom/seaborn", "astropy/astropy", "pylint-dev/pylint", "pallets/flask"}
ANNOTATION_KEYS = ("underspecified", "underspecified_notes", "false_negative", "false_negative_notes", "difficulty")


def pool_a(csv_path: str | Path = DEFAULT_CSV_PATH) -> list[str]:
    out = []
    with open(csv_path, newline="") as f:
        for r in csv.DictReader(f):
            if float(r["underspecified"]) >= 2 and float(r["false_negative"]) <= 1 and float(r["other_major_issues"]) == 0:
                out.append(r["instance_id"])
    return sorted(out)


def annotation_rows(csv_path: str | Path = DEFAULT_CSV_PATH) -> dict[str, dict]:
    with open(csv_path, newline="") as f:
        return {r["instance_id"]: r for r in csv.DictReader(f)}


def repo_of(instance_id: str) -> str:
    owner_repo = instance_id.rsplit("-", 1)[0]
    return owner_repo.replace("__", "/")


def filter_pytest_repos(ids: list[str]) -> list[str]:
    return [i for i in ids if repo_of(i) in PYTEST_REPOS]


def image_for(instance_id: str) -> str:
    return "swebench/sweb.eval.x86_64." + instance_id.replace("__", "_1776_") + ":latest"


def test_files_from_patch(test_patch: str) -> list[str]:
    return sorted(set(re.findall(r"^diff --git a/(\S+) b/", test_patch, re.M)))


def _load_dataset(name: str = DATASET, split: str = "test"):
    """Hugging Face dataset rows (network / cache). Tests monkeypatch this."""
    from datasets import load_dataset
    return load_dataset(name, split=split)


def candidates(instance_ids: list[str], out_path: Path, csv_path: Path = DEFAULT_CSV_PATH, pytest_repos_only: bool = True,
               fetch_threads: bool = True) -> int:
    """Write candidates.jsonl rows for `instance_ids`; returns the number written.

    Instances missing from the annotation CSV get blank annotation fields (the CSV covers 1,699 of the 2,294 test
    instances); instances missing from the dataset are an error.
    """
    from swesim.sources import threads

    out_path = Path(out_path)
    ids = filter_pytest_repos(list(instance_ids)) if pytest_repos_only else list(instance_ids)
    ds = {r["instance_id"]: r for r in _load_dataset()}
    missing = [i for i in ids if i not in ds]
    if missing:
        raise ValueError(f"instance ids not in SWE-bench test split: {missing[:5]}")
    ann = annotation_rows(csv_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(out_path, "w") as f:
        for iid in ids:
            r = ds[iid]
            a = ann.get(iid, {})
            repo = r["repo"]
            pr = int(iid.rsplit("-", 1)[1])
            row = {"instance_id": iid, "repo": repo, "base_commit": r["base_commit"], "version": r["version"],
                   "problem_statement": r["problem_statement"], "hints_text": r["hints_text"],
                   "fail_to_pass": json.loads(r["FAIL_TO_PASS"]), "pass_to_pass": json.loads(r["PASS_TO_PASS"]),
                   "patch": r["patch"], "test_patch": r["test_patch"], "image": image_for(iid), "repo_dir": REPO_DIR,
                   "annotation": {k: a.get(k, "") for k in ANNOTATION_KEYS},
                   "thread": threads.fetch_pr_thread(repo, pr) if fetch_threads else {}}
            f.write(json.dumps(row) + "\n")
            f.flush()
            n += 1
    return n
