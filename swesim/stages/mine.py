"""Mine stage: write candidate rows for a source into `workspace/candidates/`."""
from __future__ import annotations

from pathlib import Path

from swesim.paths import Workspace


def mine_swebench(ws: Workspace, pool: str = "A", instance_ids: list[str] | None = None, pytest_only: bool = True, fetch_threads: bool = True) -> Path:
    from swesim.sources import swebench_annotated as sb

    if pool.upper() != "A":
        raise ValueError("only pool A (underspecified >= 2, false_negative <= 1, no other major issues) is defined")
    csv_path = sb.DEFAULT_CSV_PATH
    ids = list(instance_ids) if instance_ids else sb.pool_a(csv_path)
    if pytest_only:
        ids = sb.filter_pytest_repos(ids)
    out = ws.candidates_dir / "swebench-annotated.jsonl"
    sb.candidates(ids, out, csv_path, pytest_repos_only=pytest_only, fetch_threads=fetch_threads)
    return out


def mine_github_repo(ws: Workspace, repo: str, pr: int | None = None, since: str = "2026-06-01", max_candidates: int = 20, max_builds: int = 3,
                     max_prs: int = 400, kind: str = "feature") -> Path:
    import json
    from swesim.sources import github_repo as gr

    repo = gr.normalize_repo(repo)
    cands, rejects = gr.discover(repo, since, pr=pr, max_prs=max_prs, kind=kind)
    out = ws.candidates_dir / f"github-repo--{repo.replace('/', '__')}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    built = 0
    with open(out, "w") as f:
        for c in cands[:max_candidates]:
            row = gr.candidate_to_row(c)
            row.update(gr.build_recipe(repo, c["base_commit"]))
            if built < max_builds:
                built += 1
                try:
                    row.update(gr.probe_tests(row, ws))
                except Exception as e:  # keep the evidence, reject the candidate, continue
                    rejects["build-or-probe-failed"] = rejects.get("build-or-probe-failed", 0) + 1
                    (out.parent / (row["instance_id"] + ".error.json")).write_text(json.dumps({"error": str(e)}))
                    continue
            else:
                rejects["build-budget-exhausted"] = rejects.get("build-budget-exhausted", 0) + 1
                continue
            if not row["fail_to_pass"]:
                rejects["no-f2p-after-probe"] = rejects.get("no-f2p-after-probe", 0) + 1
                continue
            f.write(json.dumps(row) + "\n")
    out.with_suffix(".rejects.json").write_text(json.dumps(rejects, indent=2))
    if out.stat().st_size == 0:
        out.with_suffix(".status").write_text("no-valid-candidate")
    return out


def probe_candidates(ws: Workspace, judged_path: Path | str, instance_ids: list[str] | None = None, max_builds: int = 3) -> Path:
    """Measure the contract offline before drafting. Probes the accepted rows of a judged file (or exactly `instance_ids`)
    with `github_repo.probe_tests`, writes `<judged>.probed.jsonl` with the measured fail_to_pass / pass_to_pass replacing the
    source's lists (the originals kept under swebench_fail_to_pass / swebench_pass_to_pass), `probe` metadata attached, and
    `judge.keep = False, reject_reason = "not-reproducible-offline"` when nothing fails offline and passes with gold. Bounded
    by `max_builds`; a summary JSON sits next to the output."""
    import json
    from collections import Counter
    from swesim.sources import github_repo as gr

    judged_path = Path(judged_path)
    # Re-running on a probed file writes it back in place (rows are fully read first); the swebench_* originals set by the
    # first probe are kept via setdefault, so the SWE-bench lists are never overwritten by an earlier measurement.
    out = judged_path if judged_path.name.endswith(".probed.jsonl") else judged_path.with_suffix(".probed.jsonl")
    rows = [json.loads(line) for line in judged_path.read_text().splitlines() if line.strip()]
    wanted = set(instance_ids) if instance_ids else None
    summary: Counter = Counter()
    built = 0

    def apply(row: dict, res: dict) -> None:
        row.setdefault("swebench_fail_to_pass", list(row.get("fail_to_pass") or []))
        row.setdefault("swebench_pass_to_pass", list(row.get("pass_to_pass") or []))
        row["fail_to_pass"], row["pass_to_pass"] = res["fail_to_pass"], res["pass_to_pass"]
        row["test_python"] = res["test_python"]
        row["probe"] = res["probe"]
        row["test_order"] = list(res["probe"].get("order") or [])  # collected definition order; the runner lists ids in it
        if not row["fail_to_pass"]:
            row["reject_reason"] = "not-reproducible-offline"
            row.setdefault("judge", {})["keep"] = False
            summary["not-reproducible-offline"] += 1
        else:
            summary["reproducible"] += 1

    with open(out, "w") as f:
        for row in rows:
            if wanted is not None:
                if row.get("instance_id") not in wanted:
                    continue
            elif (row.get("judge") or {}).get("keep") is not True:
                summary["skipped-not-accepted"] += 1
                continue
            existing = row.get("probe") or {}
            if existing.get("statuses") and existing.get("binding") == gr.probe_binding(row) and (existing.get("runner") or {}).get("exact") == "exact":
                summary["already-probed"] += 1  # a grading-faithful measurement bound to this row: no second Modal run
                f.write(json.dumps(row) + "\n")
                continue
            if built >= max_builds:
                reconstructed = gr.reconstruct_probe(row)
                if reconstructed is not None:  # no budget left: rebuild whole-file-only evidence from the job dir, marked as such in probe.runner.exact
                    summary["reconstructed-whole-file-only"] += 1
                    apply(row, reconstructed)
                    f.write(json.dumps(row) + "\n")
                    continue
                summary["build-budget-exhausted"] += 1
                continue
            built += 1
            row.setdefault("swebench_fail_to_pass", list(row.get("fail_to_pass") or []))
            row.setdefault("swebench_pass_to_pass", list(row.get("pass_to_pass") or []))
            try:
                res = gr.probe_tests(row, ws)
            except Exception as e:  # keep the evidence, reject the candidate, continue
                row["probe"] = {"error": f"{type(e).__name__}: {e}"}
                row["reject_reason"] = "probe-failed"
                row.setdefault("judge", {})["keep"] = False
                summary["probe-failed"] += 1
                f.write(json.dumps(row) + "\n")
                continue
            apply(row, res)
            f.write(json.dumps(row) + "\n")
    out.with_suffix(".summary.json").write_text(json.dumps({"rows": len(rows), "probed": built, **summary}, indent=2, sort_keys=True))
    return out
