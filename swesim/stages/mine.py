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
