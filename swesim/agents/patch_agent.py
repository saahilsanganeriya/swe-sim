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
    """Probe items are plain lines (legacy) or {"line", "removed": [...], "path"} from validate.gold_probes."""
    if isinstance(item, str): return {"line": item, "removed": [], "path": None}
    return {"line": item["line"], "removed": list(item.get("removed") or []), "path": item.get("path")}


def build_probe_command(repo_dir: str, gold_lines: list, gold_sha: str = "") -> str:
    """Counts refs/reflog entries, checks the gold object, and greps for each probe's added line.

    Between the HITS markers: `RAW <i>\\t<file>` for every file containing probe i's added line, and `HIT <i>\\t<file>`
    only when that file also lacks all of the hunk's removed lines (a base copy such as build/lib/... keeps the
    pre-fix lines; a leaked post-fix copy does not). Pure-addition probes emit HIT for every RAW; the host drops the
    patch's own base path for those. `.git/` is excluded; build/, dist/, *.egg-info and site-packages stay in scope."""
    q = shlex.quote
    gold_probe = f"git cat-file -e {q(gold_sha)} 2>/dev/null && echo GOLD_OBJECT_REACHABLE:{q(gold_sha)} || true" if gold_sha else "true"
    parts = []
    for i, spec in enumerate(_probe_spec(x) for x in gold_lines):
        raw = f"printf 'RAW {i}\\t%s\\n' \"$f\""
        hit = f"printf 'HIT {i}\\t%s\\n' \"$f\""
        if spec["removed"]:
            removed = " ".join(f"-e {q(r)}" for r in spec["removed"])
            body = f"{raw}; if ! grep -qF {removed} -- \"$f\"; then {hit}; fi"
        else:
            body = f"{raw}; {hit}"
        parts.append(f"grep -rF --exclude-dir=.git -l -- {q(spec['line'])} . 2>/dev/null | head -50 | while IFS= read -r f; do {body}; done")
    greps = " ; ".join(parts) or "true"
    return (f"cd {q(repo_dir)} && echo REFS=$(git rev-list --all | wc -l) && echo REFLOG=$(git reflog | wc -l) "
            f"&& echo GOLD_SHA_CHECKED={q(gold_sha) if gold_sha else 'none'} && echo HITS_BEGIN && ({gold_probe}; {greps}) ; echo HITS_END")


def _keep_hit(idx: int | None, path: str, specs: list[dict]) -> bool:
    norm = path[2:] if path.startswith("./") else path
    if norm == ".git" or norm.startswith(".git/") or "/.git/" in norm: return False
    if idx is not None and idx < len(specs):
        spec = specs[idx]
        if not spec["removed"] and spec["path"] and norm == spec["path"]: return False  # pure addition: the modified file's base copy is expected to hold it
    return True


def parse_probe_output(text: str, probes: list | None = None) -> dict:
    """Markers are matched as whole lines so a file named like `HITS_END_fixture.py` cannot truncate the block;
    `hits_block_found` is False when the probe output is incomplete. `raw_hits` are every file containing an added
    line; `filtered_hits` survive the removed-line, base-path and .git/ rules; `gold_line_hits` lists their files."""
    refs = re.search(r"^REFS=\s*(\d+)", text, re.M)
    reflog = re.search(r"^REFLOG=\s*(\d+)", text, re.M)
    block = re.search(r"^HITS_BEGIN$\n(.*?)^HITS_END$", text, re.M | re.S)
    specs = [_probe_spec(p) for p in (probes or [])]
    raw_hits: list[dict] = []; filtered: list[dict] = []; gold_object = False
    for line in (block.group(1).splitlines() if block else []):
        if not line.strip(): continue
        if line.startswith("GOLD_OBJECT_REACHABLE"): gold_object = True; continue
        m = re.match(r"^(RAW|HIT) (\d+)\t(.+)$", line)
        if m:
            kind, idx, path = m.group(1), int(m.group(2)), m.group(3)
            if kind == "RAW": raw_hits.append({"probe": idx, "file": path})
            elif _keep_hit(idx, path, specs): filtered.append({"probe": idx, "file": path})
        else:  # legacy probe.txt: bare file lines were both raw and hit
            path = line.strip(); raw_hits.append({"probe": None, "file": path})
            if _keep_hit(None, path, specs): filtered.append({"probe": None, "file": path})
    return {"reflog_entries": int(reflog.group(1)) if reflog else None, "all_refs": int(refs.group(1)) if refs else None,
            "gold_line_hits": sorted({h["file"] for h in filtered}), "gold_object_reachable": gold_object,
            "hits_block_found": block is not None, "raw_hits": raw_hits, "filtered_hits": filtered}


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
