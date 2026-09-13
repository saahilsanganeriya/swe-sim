"""PatchAgent: Harbor BaseAgent adapter for control patches and the reachability probe. Offline."""
import asyncio, json
from pathlib import Path

import pytest
from harbor.environments.base import ExecResult

from swesim.agents.patch_agent import PatchAgent, build_apply_command, build_probe_command, parse_probe_output


def test_apply_command_targets_repo_dir():
    cmd = build_apply_command("/testbed", "/tmp/control.diff")
    assert cmd.startswith("cd /testbed && git apply --whitespace=nowarn /tmp/control.diff")


def test_probe_command_greps_gold_lines():
    cmd = build_probe_command("/testbed", ["def new_helper(", "raise ValueError(\"dim\""])
    assert "git rev-list --all" in cmd and "grep -rF" in cmd and "new_helper" in cmd
    assert "HITS_BEGIN" in cmd and "HITS_END" in cmd and "git cat-file" not in cmd
    with_sha = build_probe_command("/testbed", [], gold_sha="a" * 40)
    assert "git cat-file -e " + "a" * 40 in with_sha and "GOLD_OBJECT_REACHABLE" in with_sha


def test_parse_probe_output_separates_counts_from_hits():
    out = "REFS=1\nREFLOG=1\nGOLD_SHA_CHECKED=none\nHITS_BEGIN\nHITS_END\n"
    assert parse_probe_output(out) == {"reflog_entries": 1, "all_refs": 1, "gold_line_hits": [], "gold_object_reachable": False, "hits_block_found": True}
    out = "REFS=3\nREFLOG=7\nGOLD_SHA_CHECKED=abc\nHITS_BEGIN\nGOLD_OBJECT_REACHABLE:abc\n./pkg/mod.py\nHITS_END\n"
    p = parse_probe_output(out)
    assert p["all_refs"] == 3 and p["reflog_entries"] == 7 and p["gold_object_reachable"] and p["gold_line_hits"] == ["GOLD_OBJECT_REACHABLE:abc", "./pkg/mod.py"]


def test_parse_probe_output_markers_are_line_anchored():
    out = "REFS=1\nREFLOG=0\nGOLD_SHA_CHECKED=none\nHITS_BEGIN\n./tests/HITS_END_fixture.py\n./HITS_BEGIN.txt\nHITS_END\n"
    p = parse_probe_output(out)
    assert p["gold_line_hits"] == ["./tests/HITS_END_fixture.py", "./HITS_BEGIN.txt"] and p["hits_block_found"] is True
    truncated = parse_probe_output("REFS=1\nREFLOG=0\nHITS_BEGIN\n./x.py\n")
    assert truncated["hits_block_found"] is False and truncated["gold_line_hits"] == []


def test_agent_identity_and_kwargs(tmp_path):
    a = PatchAgent(tmp_path)
    assert a.version() == "1.0.0" and PatchAgent.name() == "patch-agent"
    b = PatchAgent(tmp_path, probe="true", gold_lines='["x"]', gold_sha="")
    assert b._probe is True and b._gold_lines == ["x"]
    c = PatchAgent(tmp_path, probe=True, gold_lines=["y"])
    assert c._probe is True and c._gold_lines == ["y"]
    assert PatchAgent(tmp_path, probe="false")._probe is False


class FakeEnv:
    def __init__(self, repo="/app", apply_rc=0, probe_out="REFS=1\nREFLOG=1\nGOLD_SHA_CHECKED=none\nHITS_BEGIN\nHITS_END\n"):
        self.repo, self.apply_rc, self.probe_out, self.calls, self.uploads = repo, apply_rc, probe_out, [], []

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        self.calls.append((command, user))
        if "SWESIM_REPO_DIR" in command: return ExecResult(stdout=self.repo, stderr="", return_code=0)
        if "HITS_BEGIN" in command: return ExecResult(stdout=self.probe_out, stderr="", return_code=0)
        return ExecResult(stdout="M pkg/mod.py", stderr="" if self.apply_rc == 0 else "error: patch failed", return_code=self.apply_rc)

    async def upload_file(self, source_path, target_path):
        self.uploads.append((Path(source_path), target_path))


def test_run_applies_patch_as_agent_user(tmp_path):
    patch = tmp_path / "c.diff"; patch.write_text("diff")
    logs = tmp_path / "logs"; logs.mkdir(); env = FakeEnv()
    asyncio.run(PatchAgent(logs, patch=str(patch)).run("instruction", env, None))
    assert env.uploads == [(patch, "/tmp/control.diff")]
    apply_calls = [c for c in env.calls if "git apply" in c[0]]
    assert len(apply_calls) == 1 and apply_calls[0][1] == "agent" and "cd /app && git apply --whitespace=nowarn /tmp/control.diff" in apply_calls[0][0]
    assert (logs / "apply.txt").read_text().startswith("rc=0")
    env2 = FakeEnv(apply_rc=1)
    with pytest.raises(RuntimeError, match="failed to apply"):
        asyncio.run(PatchAgent(logs, patch=str(patch)).run("instruction", env2, None))
    assert "rc=1" in (logs / "apply.txt").read_text()


def test_run_probe_writes_txt_and_json(tmp_path):
    logs = tmp_path / "logs"; logs.mkdir()
    env = FakeEnv(probe_out="REFS=1\nREFLOG=1\nGOLD_SHA_CHECKED=none\nHITS_BEGIN\n./pkg/mod.py\nHITS_END\n")
    asyncio.run(PatchAgent(logs, probe="true", gold_lines=json.dumps(["def new_helper("])).run("i", env, None))
    probe_calls = [c for c in env.calls if "HITS_BEGIN" in c[0]]
    assert len(probe_calls) == 1 and probe_calls[0][1] == "agent" and "new_helper" in probe_calls[0][0]
    assert "HITS_BEGIN" in (logs / "probe.txt").read_text()
    j = json.loads((logs / "probe.json").read_text())
    assert j == {"reflog_entries": 1, "all_refs": 1, "gold_line_hits": ["./pkg/mod.py"], "gold_object_reachable": False, "hits_block_found": True}
    assert not env.uploads


def test_run_requires_repo_dir_and_patch(tmp_path):
    logs = tmp_path / "logs"; logs.mkdir()
    with pytest.raises(ValueError, match="repo_dir"):
        asyncio.run(PatchAgent(logs, patch="/x.diff").run("i", FakeEnv(repo=""), None))
    with pytest.raises(ValueError, match="patch"):
        asyncio.run(PatchAgent(logs).run("i", FakeEnv(), None))
