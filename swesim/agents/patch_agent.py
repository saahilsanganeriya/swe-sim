"""PatchAgent: a Harbor BaseAgent that either applies a named control patch as the agent user (validate stage)
or runs the history/gold-line reachability probe. Invocation:

    harbor run -p <task> --agent swesim.agents.patch_agent:PatchAgent --ak patch=/abs/path.diff -e modal
    harbor run -p <task> --agent swesim.agents.patch_agent:PatchAgent --ak probe=true --ak gold_lines='[...]' -e modal
"""
from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

from harbor.agents.base import BaseAgent
from harbor.models.agent.context import AgentContext

REMOTE_PATCH = "/tmp/control.diff"


def build_apply_command(repo_dir: str, remote_patch: str) -> str:
    return f"cd {shlex.quote(repo_dir)} && git apply --whitespace=nowarn {shlex.quote(remote_patch)} && git status --porcelain | head -50"


def _probe_spec(item) -> dict:
    """Probe items are plain lines (legacy) or {"line", "removed": [...], "path", "new"} from validate.gold_probes."""
    if isinstance(item, str): return {"line": item, "removed": [], "path": None, "new": False}
    return {"line": item["line"], "removed": list(item.get("removed") or []), "path": item.get("path"), "new": bool(item.get("new", False))}


_STRIP_LINES = "sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'"


def build_probe_command(repo_dir: str, gold_lines: list, gold_sha: str = "") -> str:
    """Counts refs/reflog entries, checks the gold object, and greps for each probe's added line.

    Between the HITS markers: `RAW <i>\\t<file>` for every file containing probe i's added line, and `HIT <i>\\t<file>`
    only when that file also lacks all of the hunk's removed lines, matched as whole lines after stripping whitespace
    (a base copy such as build/lib/... keeps the pre-fix lines; a leaked post-fix copy does not; a removed line that
    is a substring of the added line therefore never masks a hit). Pure-addition probes emit HIT for every RAW; for an
    existing file the probe first prints `PREEXISTS <i>` when the added line is already present at the base path (the
    line pre-exists at base, so no copy is evidence) and the host skips that path. New files (`new`) skip nothing.
    `.git/` is excluded; build/, dist/, *.egg-info and site-packages stay in scope."""
    q = shlex.quote
    gold_probe = f"git cat-file -e {q(gold_sha)} 2>/dev/null && echo GOLD_OBJECT_REACHABLE:{q(gold_sha)} || true" if gold_sha else "true"
    parts = []
    for i, spec in enumerate(_probe_spec(x) for x in gold_lines):
        raw = f"printf 'RAW {i}\\t%s\\n' \"$f\""
        hit = f"printf 'HIT {i}\\t%s\\n' \"$f\""
        pre = ""
        if spec["removed"]:
            removed = " ".join(f"-e {q(r)}" for r in spec["removed"])
            body = f"{raw}; if ! {_STRIP_LINES} < \"$f\" | grep -qxF {removed}; then {hit}; fi"
        else:
            body = f"{raw}; {hit}"
            if spec["path"] and not spec["new"]:
                pre = f"if [ -f {q(spec['path'])} ] && grep -qF -- {q(spec['line'])} {q(spec['path'])}; then printf 'PREEXISTS {i}\\n'; fi; "
        parts.append(f"{pre}grep -rF --exclude-dir=.git -l -- {q(spec['line'])} . 2>/dev/null | head -50 | while IFS= read -r f; do {body}; done")
    greps = " ; ".join(parts) or "true"
    return (f"cd {q(repo_dir)} && echo REFS=$(git rev-list --all | wc -l) && echo REFLOG=$(git reflog | wc -l) "
            f"&& echo GOLD_SHA_CHECKED={q(gold_sha) if gold_sha else 'none'} && echo HITS_BEGIN && ({gold_probe}; {greps}) ; echo HITS_END")


def _keep_hit(idx: int | None, path: str, specs: list[dict], preexisting: set[int] = frozenset()) -> bool:
    norm = path[2:] if path.startswith("./") else path
    if norm == ".git" or norm.startswith(".git/") or "/.git/" in norm: return False
    if idx is not None:
        if idx in preexisting: return False  # the added line already exists at base in the modified file: copies are not evidence
        if idx < len(specs):
            spec = specs[idx]
            if not spec["removed"] and not spec["new"] and spec["path"] and norm == spec["path"]: return False  # pure addition: the modified file's own base copy
    return True


def parse_probe_output(text: str, probes: list | None = None) -> dict:
    """Markers are matched as whole lines so a file named like `HITS_END_fixture.py` cannot truncate the block;
    `hits_block_found` is False when the probe output is incomplete. `raw_hits` are every file containing an added
    line; `filtered_hits` survive the removed-line, PREEXISTS, base-path and .git/ rules; `gold_line_hits` lists their
    files; `preexisting_probes` are the probes whose added line already existed at the base path."""
    refs = re.search(r"^REFS=\s*(\d+)", text, re.M)
    reflog = re.search(r"^REFLOG=\s*(\d+)", text, re.M)
    block = re.search(r"^HITS_BEGIN$\n(.*?)^HITS_END$", text, re.M | re.S)
    specs = [_probe_spec(p) for p in (probes or [])]
    raw_hits: list[dict] = []; candidates: list[tuple[int | None, str]] = []; preexisting: set[int] = set(); gold_object = False
    for line in (block.group(1).splitlines() if block else []):
        if not line.strip(): continue
        if line.startswith("GOLD_OBJECT_REACHABLE"): gold_object = True; continue
        pre = re.match(r"^PREEXISTS (\d+)$", line)
        if pre: preexisting.add(int(pre.group(1))); continue
        m = re.match(r"^(RAW|HIT) (\d+)\t(.+)$", line)
        if m:
            kind, idx, path = m.group(1), int(m.group(2)), m.group(3)
            if kind == "RAW": raw_hits.append({"probe": idx, "file": path})
            else: candidates.append((idx, path))
        else:  # legacy probe.txt: bare file lines were both raw and hit
            path = line.strip(); raw_hits.append({"probe": None, "file": path}); candidates.append((None, path))
    filtered = [{"probe": idx, "file": path} for idx, path in candidates if _keep_hit(idx, path, specs, preexisting)]
    return {"reflog_entries": int(reflog.group(1)) if reflog else None, "all_refs": int(refs.group(1)) if refs else None,
            "gold_line_hits": sorted({h["file"] for h in filtered}), "gold_object_reachable": gold_object,
            "hits_block_found": block is not None, "raw_hits": raw_hits, "filtered_hits": filtered, "preexisting_probes": sorted(preexisting)}


class PatchAgent(BaseAgent):
    """Applies a named control patch (validate stage) or runs the history/gold-line reachability probe."""

    def __init__(self, logs_dir: Path, model_name: str | None = None, patch: str | None = None, probe: bool | str = False,
                 gold_lines: str | list[str] = "[]", gold_sha: str = "", **kw):
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kw)
        self._patch = patch
        self._probe = str(probe).strip().lower() in ("true", "1", "yes")
        self._gold_lines = json.loads(gold_lines) if isinstance(gold_lines, str) else list(gold_lines)
        self._gold_sha = gold_sha or ""

    @staticmethod
    def name() -> str:
        return "patch-agent"

    def version(self) -> str:
        return "1.0.0"

    async def setup(self, environment) -> None:
        return None

    async def run(self, instruction: str, environment, context: AgentContext) -> None:
        r = await environment.exec('printf "%s" "$SWESIM_REPO_DIR"', user="agent")
        repo_dir = (r.stdout or "").strip()
        if not repo_dir.startswith("/"): raise ValueError("missing repo_dir: SWESIM_REPO_DIR is not set in the task environment")
        Path(self.logs_dir).mkdir(parents=True, exist_ok=True)
        if self._probe:
            r = await environment.exec(f"bash -c {shlex.quote(build_probe_command(repo_dir, self._gold_lines, self._gold_sha))}", user="agent")
            stdout = r.stdout or ""
            (Path(self.logs_dir) / "probe.txt").write_text(stdout + ("\n" + r.stderr if r.stderr else ""))
            (Path(self.logs_dir) / "probe.json").write_text(json.dumps(parse_probe_output(stdout, self._gold_lines), indent=2))
            return
        if not self._patch: raise ValueError("patch kwarg required (host path to a diff) unless probe=true")
        await environment.upload_file(source_path=Path(self._patch), target_path=REMOTE_PATCH)
        r = await environment.exec(f"bash -c {shlex.quote(build_apply_command(repo_dir, REMOTE_PATCH))}", user="agent")
        (Path(self.logs_dir) / "apply.txt").write_text(f"rc={r.return_code}\n{r.stdout or ''}\n{r.stderr or ''}")
        if r.return_code != 0: raise RuntimeError(f"control patch failed to apply: {(r.stderr or '')[-500:]}")
