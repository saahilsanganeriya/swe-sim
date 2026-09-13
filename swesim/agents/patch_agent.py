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


def build_probe_command(repo_dir: str, gold_lines: list[str], gold_sha: str = "") -> str:
    """Counts refs/reflog entries and lists files containing gold lines. Only real hits appear between the HITS markers."""
    q = shlex.quote
    gold_probe = f"git cat-file -e {q(gold_sha)} 2>/dev/null && echo GOLD_OBJECT_REACHABLE:{q(gold_sha)} || true" if gold_sha else "true"
    greps = " ; ".join(f"grep -rF --exclude-dir=.git -l -- {q(line)} . | head -5" for line in gold_lines) or "true"
    return (f"cd {q(repo_dir)} && echo REFS=$(git rev-list --all | wc -l) && echo REFLOG=$(git reflog | wc -l) "
            f"&& echo GOLD_SHA_CHECKED={q(gold_sha) if gold_sha else 'none'} && echo HITS_BEGIN && ({gold_probe}; {greps}) ; echo HITS_END")


def parse_probe_output(text: str) -> dict:
    """Markers are matched as whole lines so a file named like `HITS_END_fixture.py` cannot truncate the block;
    `hits_block_found` is False when the probe output is incomplete."""
    refs = re.search(r"^REFS=\s*(\d+)", text, re.M)
    reflog = re.search(r"^REFLOG=\s*(\d+)", text, re.M)
    block = re.search(r"^HITS_BEGIN$\n(.*?)^HITS_END$", text, re.M | re.S)
    hits = [line.strip() for line in block.group(1).splitlines() if line.strip()] if block else []
    return {"reflog_entries": int(reflog.group(1)) if reflog else None, "all_refs": int(refs.group(1)) if refs else None,
            "gold_line_hits": hits, "gold_object_reachable": any(h.startswith("GOLD_OBJECT_REACHABLE") for h in hits),
            "hits_block_found": block is not None}


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
            (Path(self.logs_dir) / "probe.json").write_text(json.dumps(parse_probe_output(stdout), indent=2))
            return
        if not self._patch: raise ValueError("patch kwarg required (host path to a diff) unless probe=true")
        await environment.upload_file(source_path=Path(self._patch), target_path=REMOTE_PATCH)
        r = await environment.exec(f"bash -c {shlex.quote(build_apply_command(repo_dir, REMOTE_PATCH))}", user="agent")
        (Path(self.logs_dir) / "apply.txt").write_text(f"rc={r.return_code}\n{r.stdout or ''}\n{r.stderr or ''}")
        if r.return_code != 0: raise RuntimeError(f"control patch failed to apply: {(r.stderr or '')[-500:]}")
