"""Temporal activities. Payloads carry ids, paths, hashes and model ids; never secrets.

Stage modules owned by other streams are imported lazily inside activity bodies so this module
imports without them (tests inject fakes into sys.modules).

Trial lifecycle (the part that must be exactly-once in effect, not merely by workflow id):
  launch_once   -> read evidence (trial dir + unit state), decide, and only on "launch" write
                   request.json, a launch intent, run `systemd-run --user --unit swesim-trial-<id>`
                   and record the acknowledgement. Any prior launch evidence means: never relaunch.
  await_trial   -> heartbeating poll of record.json / runner-error.json / unit state; returns a
                   terminal status dict (completed, runner-error, launch-failed, unit-failed,
                   unresolved). Retryable and idempotent: it only reads.
Run as a program (`python -m swesim.workflows.activities run-trial <request.json>`) this module is
the per-trial process inside the unit; it survives Temporal worker restarts because systemd, not
the worker, owns it.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from temporalio import activity
from temporalio.exceptions import ApplicationError

from swesim.models import ID_RE, RunConfig, ScenarioSpec, trial_id
from swesim.paths import Workspace, atomic_json

WS = Workspace(os.environ.get("SWESIM_WORKSPACE", "workspace"))
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CSV = REPO_ROOT / "data" / "annotations" / "ensembled_annotations_public.csv"
# "systemd": production (transient user unit per trial). "subprocess": detached child process for
# macOS/local smoke only; it is NOT production and loses the unit-state evidence across worker restarts.
LAUNCHER = os.environ.get("SWESIM_TRIAL_LAUNCHER", "systemd")
HEARTBEAT_SECONDS = 30.0
POLL_SECONDS = float(os.environ.get("SWESIM_TRIAL_POLL_SEC", "10"))
AWAIT_DEADLINE_SEC = 3 * 3600
LAUNCH_GRACE_POLLS = 6  # polls without any launch evidence before await reports launch-failed
# Per-role fallback chains (CLAUDE.md, unattended-run rules): the configured id is probed first, then the chain in order.
# Judge and draft must resolve to different ids (draft_spec.draft refuses correlated errors); the solver is never substituted.
FALLBACK_CHAINS = {
    "judge_model": ["openai/gpt-5.5", "anthropic/claude-opus-5", "anthropic/claude-sonnet-5"],
    "draft_model": ["anthropic/claude-sonnet-5", "anthropic/claude-haiku-4-5-20251001"],
    "stakeholder_model": ["openai/gpt-5.5", "anthropic/claude-sonnet-5", "anthropic/claude-haiku-4-5-20251001"],
}
RUNNING_STATES = {"active", "activating", "reloading", "deactivating"}
SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD")
LAUNCH_SLOTS = asyncio.Semaphore(1)  # pilot trials launch serially per worker
_CHILDREN: dict[int, subprocess.Popen] = {}


class TrialBindingConflict(ValueError):
    """request.json for this trial id already binds a different request."""


class LaunchFailed(RuntimeError):
    """The launcher did not create the unit. Never retried automatically; reconcile first."""


class UnitStateUnavailable(RuntimeError):
    """systemctl could not report the unit (no bus, missing XDG_RUNTIME_DIR, killed). Retryable; never read as 'unit gone'."""


def RUN(argv: list[str]) -> subprocess.CompletedProcess:  # injectable process runner (tests substitute a fake)
    return subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)


# ------------------------------------------------------------------ helpers

def _heartbeat(*details) -> None:
    if activity.in_activity():
        activity.heartbeat(*details)


async def blocking(fn, every: float | None = None):
    """Run a blocking stage function in a thread while heartbeating every `every` seconds."""
    task = asyncio.ensure_future(asyncio.to_thread(fn))
    while True:
        done, _ = await asyncio.wait({task}, timeout=every or HEARTBEAT_SECONDS)
        _heartbeat()
        if done:
            return task.result()


def _spec(sid: str) -> ScenarioSpec:
    return ScenarioSpec.model_validate_json((WS.scenario_dir(sid) / "scenario.json").read_text())


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _redact(text: str) -> str:
    """Replace the values of secret-looking environment variables inside error text before it is stored."""
    for k, v in os.environ.items():
        if len(v) >= 8 and any(m in k.upper() for m in SECRET_MARKERS) and v in text:
            text = text.replace(v, f"[redacted:{k}]")
    return text


def _accepted_kwargs(fn, candidates: dict) -> dict:
    """Keep only keyword arguments `fn` accepts (all of them if it takes **kwargs)."""
    params = inspect.signature(fn).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(candidates)
    return {k: v for k, v in candidates.items() if k in params}


def scenario_id_for(instance_id: str) -> str:
    """Same rule as swesim.stages.draft_spec.draft: lower-case, '__' -> '--'."""
    sid = instance_id.lower().replace("__", "--")
    if not ID_RE.fullmatch(sid):
        raise ApplicationError(f"instance id {instance_id!r} does not map to a valid scenario id", non_retryable=True)
    return sid


# ------------------------------------------------------------------ pipeline activities

def probe_models(p: dict) -> dict:
    """One tiny request per model id. Judge, draft and stakeholder resolve along FALLBACK_CHAINS (configured id first);
    judge and draft must differ (advance the draft chain, then the judge chain, and record why); the solver is never
    substituted. Evidence: workspace/evidence/model-probe.json with configured/resolved ids and the reasoning."""
    from swesim import llm

    results: dict[str, dict] = {}

    def probe(model_id: str) -> bool:
        if model_id not in results:
            try:
                llm.chat_json(model_id, "You are a connectivity probe.", [{"role": "user", "content": 'Reply with {"ok": true}.'}],
                              '{"ok": true}', max_tokens=20)
                results[model_id] = {"ok": True, "error": None}
            except Exception as e:  # provider/auth/network failures are the point of the probe
                results[model_id] = {"ok": False, "error": _redact(f"{type(e).__name__}: {str(e)[:200]}")}
        return results[model_id]["ok"]

    configured = {role: p.get(role) or chain[0] for role, chain in FALLBACK_CHAINS.items()}

    def candidates(role: str) -> list[str]:
        return [configured[role]] + [m for m in FALLBACK_CHAINS[role] if m != configured[role]]

    def first_available(role: str, exclude: set[str] = frozenset(), after: str | None = None) -> str | None:
        cands = candidates(role)
        if after in cands:
            cands = cands[cands.index(after) + 1:]
        return next((m for m in cands if m not in exclude and probe(m)), None)

    resolved: dict[str, str] = {}; notes: list[str] = []; needs_human: list[str] = []
    for role in FALLBACK_CHAINS:
        chosen = first_available(role)
        if chosen is None:
            needs_human.append(f"{role}: {configured[role]} and its fallbacks {candidates(role)[1:]} are all unavailable")
            chosen = configured[role]
        elif chosen != configured[role]:
            notes.append(f"{role}: {configured[role]} unavailable; fell back to {chosen}")
        resolved[role] = chosen
    if resolved["judge_model"] == resolved["draft_model"]:
        shared = resolved["draft_model"]
        alt = first_available("draft_model", exclude={shared}, after=shared)
        if alt is not None:
            notes.append(f"judge and draft both resolved to {shared}; advanced the draft chain to {alt} so their errors stay uncorrelated")
            resolved["draft_model"] = alt
        else:
            alt = first_available("judge_model", exclude={shared}, after=shared)
            if alt is not None:
                notes.append(f"judge and draft both resolved to {shared}; draft chain exhausted, advanced the judge chain to {alt}")
                resolved["judge_model"] = alt
            else:
                needs_human.append(f"judge and draft both resolve to {shared} and no distinct model is available")
    solver_unavailable = [m for m in (p.get("models") or []) if not probe(m)]
    needs_human += [f"solver model {m} unavailable; no silent substitution" for m in solver_unavailable]
    substitutions = {role: m for role, m in resolved.items() if m != configured[role]}
    out = {"models": results, "configured": configured, "resolved": resolved, "substitutions": substitutions, "notes": notes,
           "needs_human": needs_human, "solver_unavailable": solver_unavailable, "chains": FALLBACK_CHAINS, "probed_at": time.time()}
    atomic_json(WS.root / "evidence" / "model-probe.json", out)
    return out


@activity.defn
async def probe_models_activity(p: dict) -> dict:
    return await blocking(lambda: probe_models(p))


@activity.defn
async def mine_activity(p: dict) -> dict:
    import importlib

    M = importlib.import_module("swesim.stages.mine")
    source = p.get("source", "swebench-annotated")
    if source == "github-repo":
        fn = M.mine_github_repo
        kwargs = _accepted_kwargs(fn, {k: p[k] for k in ("pr", "since", "max_candidates", "max_builds", "max_prs", "kind") if k in p})
        path = await blocking(lambda: fn(WS, p["repo"], **kwargs))
    else:
        fn = getattr(M, "mine_swebench_annotated", None) or M.mine_swebench
        pytest_only = p.get("pytest_repos_only", True)
        kwargs = _accepted_kwargs(fn, {"csv_path": Path(p.get("csv_path") or DEFAULT_CSV), "limit": p.get("limit"), "pytest_repos_only": pytest_only,
                                       "instance_ids": p.get("instance_ids"), "pytest_only": pytest_only, "pool": p.get("pool", "A")})
        path = await blocking(lambda: fn(WS, **kwargs))
    return {"candidates_path": str(path)}


@activity.defn
async def judge_activity(p: dict) -> dict:
    from swesim.stages.judge import judge_candidate

    src = Path(p["candidates_path"]); out = src.with_suffix(".judged.jsonl"); rows = []
    # Idempotent stage output is retained; an explicit new pipeline version is required to regenerate.
    if out.exists():
        rows = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    else:
        lines = [line for line in src.read_text().splitlines() if line.strip()][: p.get("max_candidates", 20)]
        for line in lines:
            row = json.loads(line)
            row["judge"] = await blocking(lambda row=row: judge_candidate(row, p["judge_model"]))
            rows.append(row)
        tmp = out.with_name(out.name + ".tmp")  # write-then-rename: a crash never leaves a truncated file that a rerun would trust
        tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
        os.replace(tmp, out)
    return {"judged_path": str(out), "instance_ids": [r["instance_id"] for r in rows if r.get("judge", {}).get("keep")]}


def is_leak_reason(reason) -> bool:
    """Same rule as workflows._is_leak_reason (kept separate: the workflow module imports only temporalio)."""
    r = str(reason).strip().lower()
    return r.startswith("withholding") or "leak" in r or "withheld" in r


@activity.defn
async def list_candidates_activity(p: dict) -> dict:
    """Candidate identifiers from a candidates.jsonl (for CreateTasksWorkflow's fan-out); ids only, no row bodies."""
    lines = [line for line in Path(p["candidates_path"]).read_text().splitlines() if line.strip()]
    if p.get("max_candidates"):
        lines = lines[: int(p["max_candidates"])]
    out = []
    for line in lines:
        row = json.loads(line)
        out.append({"instance_id": row["instance_id"], "scenario_id": scenario_id_for(row["instance_id"])})
    return {"candidates": out}


@activity.defn
async def judge_one_activity(p: dict) -> dict:
    """Judge one candidate row into its own one-row `<candidates_dir>/judged/<scenario_id>.judged.jsonl`
    (parallel-safe; retained on rerun). draft_activity reads that file unchanged."""
    from swesim.stages.judge import judge_candidate

    src = Path(p["candidates_path"]); sid = scenario_id_for(p["instance_id"])
    out = src.parent / "judged" / f"{sid}.judged.jsonl"
    if out.exists():
        row = json.loads(out.read_text().splitlines()[0])
    else:
        row = next((json.loads(line) for line in src.read_text().splitlines() if line.strip() and json.loads(line).get("instance_id") == p["instance_id"]), None)
        if row is None:
            raise ApplicationError(f"candidate {p['instance_id']} not in {src}", non_retryable=True)
        row["judge"] = await blocking(lambda: judge_candidate(row, p["judge_model"]))
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".tmp"); tmp.write_text(json.dumps(row) + "\n"); os.replace(tmp, out)
    judge = row.get("judge", {})
    return {"judged_path": str(out), "instance_id": p["instance_id"], "scenario_id": sid, "keep": bool(judge.get("keep")),
            "reasons": [] if judge.get("keep") else ["judge did not keep the candidate"], "judge_model": judge.get("model_id", p.get("judge_model"))}


@activity.defn
async def precheck_draft_activity(p: dict) -> dict:
    """Hold mode: schema, asset and withholding-leak checks on scenario.draft.json; writes nothing, approves nothing."""
    import importlib

    AP = importlib.import_module("swesim.stages.approve")
    sid = p["scenario_id"]; sdir = WS.scenario_dir(sid); draft = sdir / "scenario.draft.json"
    if not draft.is_file():
        return {"ok": False, "reasons": [f"missing {draft}"], "scenario_id": sid, "spec_hash": None}
    try:
        spec = ScenarioSpec.model_validate_json(draft.read_text())
    except Exception as e:  # noqa: BLE001 - schema failure is a result, not an infrastructure error
        return {"ok": False, "reasons": [f"schema: {str(e)[:300]}"], "scenario_id": sid, "spec_hash": None}
    reasons: list[str] = []
    check_assets = getattr(AP, "check_assets", None)
    if check_assets is not None:
        reasons += [str(r) for r in (await blocking(lambda: check_assets(sdir, spec)) or [])]
    auto_gates = getattr(AP, "auto_gates", None)
    if auto_gates is not None:
        reasons += [str(r) for r in (await blocking(lambda: auto_gates(spec, sdir)) or []) if is_leak_reason(r)]
    return {"ok": not reasons, "reasons": reasons, "scenario_id": spec.id, "spec_hash": spec.spec_hash}


@activity.defn
async def human_approve_activity(p: dict) -> dict:
    """Hold mode approve: the signalled spec_hash must equal the draft on disk; then approve_scenario(auto=False)
    writes scenario.json + approval.json (approved_by human-command, review_needed false). Mismatch -> non-retryable."""
    from swesim.stages.approve import approve_scenario

    sid = p["scenario_id"]; draft = WS.scenario_dir(sid) / "scenario.draft.json"
    if not draft.is_file():
        raise ApplicationError(f"missing {draft}", non_retryable=True)
    spec = ScenarioSpec.model_validate_json(draft.read_text())
    if p.get("spec_hash") != spec.spec_hash:
        raise ApplicationError("signalled spec_hash differs from the draft on disk; refresh the review queue", non_retryable=True)
    r = await blocking(lambda: approve_scenario(WS, sid, auto=False))
    if not r.get("approved"):
        raise ApplicationError("human approval refused by the schema/asset checks: " + "; ".join(map(str, r.get("reasons", []))), non_retryable=True)
    if r.get("spec_hash") != spec.spec_hash:
        raise ApplicationError("approval hash differs from the reviewed draft", non_retryable=True)
    atomic_json(WS.scenario_dir(sid) / "approval.note.json", {"scenario_id": sid, "spec_hash": spec.spec_hash, "note": p.get("note"), "at": time.time()})
    return {"approved": True, "scenario_id": sid, "spec_hash": spec.spec_hash, "approved_by": "human-command"}


@activity.defn
async def draft_activity(p: dict) -> dict:
    from swesim.stages.draft_spec import draft

    rows = [json.loads(line) for line in Path(p["judged_path"]).read_text().splitlines() if line.strip()]
    row = next(r for r in rows if r["instance_id"] == p["instance_id"])
    sid = scenario_id_for(p["instance_id"]); sdir = WS.scenario_dir(sid); path = sdir / "scenario.draft.json"
    if p.get("force") and path.exists():
        n = 1
        while (sdir / f"scenario.draft.rejected-{n}.json").exists():
            n += 1
        path.rename(sdir / f"scenario.draft.rejected-{n}.json")  # keep the rejected draft as evidence
    if not path.exists():
        kwargs = _accepted_kwargs(draft, {"reject_reasons": p["reject_reasons"]}) if p.get("reject_reasons") else {}
        await blocking(lambda: draft(row, row.get("judge", {}), p["draft_model"], sdir, **kwargs))
    return {"scenario_id": sid, "draft_path": str(path)}


@activity.defn
async def auto_approve_activity(p: dict) -> dict:
    from swesim.stages.approve import approve_scenario

    # Code-owned gates decide; approval.json records approved_by: auto-validators, review_needed: true.
    return await blocking(lambda: approve_scenario(WS, p["scenario_id"], auto=True))


@activity.defn
async def verify_approval_activity(p: dict) -> dict:
    if not (WS.scenario_dir(p["scenario_id"]) / "scenario.json").exists():
        raise ApplicationError(f"scenario.json missing for {p['scenario_id']}; run `swesim approve` before signalling", non_retryable=True)
    spec = _spec(p["scenario_id"]); approval_path = WS.scenario_dir(spec.id) / "approval.json"
    if not approval_path.exists():
        raise ApplicationError(f"approval.json missing for {spec.id}", non_retryable=True)
    approval = json.loads(approval_path.read_text())
    if p["spec_hash"] != spec.spec_hash or approval.get("spec_hash") != spec.spec_hash:
        raise ApplicationError("approval hash differs from frozen spec", non_retryable=True)
    return {"scenario_id": spec.id, "spec_hash": spec.spec_hash}


@activity.defn
async def compile_activity(p: dict) -> dict:
    from swesim.stages.compile import compile_scenario

    r = await blocking(lambda: compile_scenario(WS, _spec(p["scenario_id"]), RunConfig.default("interactive"), p["stakeholder_url"]))
    task_dirs = getattr(r, "task_dirs", None) or {}
    return {"compile_path": str(r.compile_json), "image_digest": getattr(r, "image_digest", None), "task_dirs": {k: str(v) for k, v in dict(task_dirs).items()}}


@activity.defn
async def validate_activity(p: dict) -> dict:
    from swesim.stages.validate import validate_scenario

    r = await blocking(lambda: validate_scenario(WS, _spec(p["scenario_id"]), RunConfig.default("full"), p["stakeholder_url"]))
    return {"status": r["status"], "failures": r.get("failures", [])}


@activity.defn
async def register_activity(p: dict) -> dict:
    from swesim.stages.register import register_scenario

    r = await blocking(lambda: register_scenario(WS, _spec(p["scenario_id"])))
    # Coerce to plain JSON (Path, datetime, models -> str) so the activity result never fails to serialize after the registry write.
    return {"registry_entry": json.loads(json.dumps(r, default=str))}


def plan_trials(p: dict) -> dict:
    """Precompute trial requests with the actual per-model RunConfig; trial ids are the workflow ids."""
    spec = _spec(p["scenario_id"]); comp = json.loads((WS.scenario_dir(spec.id) / "compile.json").read_text()); trials = []
    offset = int(p.get("attempt_offset", 0))  # manual retries use fresh attempt indices, never the same trial id
    stakeholder_model = p.get("stakeholder_model")  # the probe's resolved stakeholder id; part of every trial's RunConfig and hash
    for cond in p.get("conditions", ["interactive"]):
        for model in p["models"]:
            for i in range(offset, offset + p.get("k", 1)):
                profile = p.get("brief_profile", "withheld")
                if cond == "full" or not any(b.profile == profile for b in spec.briefs):
                    profile = "natural"
                kwargs = dict(condition=cond, model=model, compiled_hash=comp["compiled_hash"], purpose=p.get("purpose", "demonstration"), brief_profile=profile)
                if stakeholder_model:
                    kwargs["stakeholder"] = {"model_id": stakeholder_model}
                rc = RunConfig(**kwargs)
                tid = trial_id(spec.spec_hash, cond, rc.run_config_hash, i)
                trials.append(dict(trial_id=tid, scenario_id=spec.id, spec_hash=spec.spec_hash, condition=cond, model=model,
                                   stakeholder_model=rc.stakeholder.model_id, run_config=rc.model_dump(mode="json"),
                                   run_config_hash=rc.run_config_hash, attempt_index=i, stakeholder_url=p["stakeholder_url"]))
    if len({t["trial_id"] for t in trials}) != len(trials):
        raise ValueError("duplicate matrix entries")
    return {"scenario_id": spec.id, "trials": trials}


@activity.defn
async def plan_trials_activity(p: dict) -> dict:
    return plan_trials(p)


@activity.defn
async def score_activity(p: dict) -> dict:
    from swesim.stages.score import score_scenario

    return {"scores_path": str(await blocking(lambda: score_scenario(WS, p["scenario_id"])))}


@activity.defn
async def export_activity(p: dict) -> dict:
    from swesim.stages.export import export_scenario

    return {"manifest_paths": [str(x) for x in await blocking(lambda: export_scenario(WS, p["scenario_id"]))]}


# ------------------------------------------------------------------ trial lifecycle: evidence and decision

def unit_name(tid: str) -> str:
    return "swesim-trial-" + tid


def trial_dir(tid: str) -> Path:
    return WS.root / "trials" / tid


@dataclass(frozen=True)
class TrialEvidence:
    has_record: bool
    has_runner_error: bool
    status: str | None          # status.json written by swesim.stages.run (running | completed | launch-failed)
    launch_phase: str | None    # launch.json written here: intent | acknowledged | failed
    launch_returncode: int | None
    load_state: str             # systemd LoadState: loaded | not-found
    active_state: str           # systemd ActiveState: active | activating | inactive | failed | ...
    sub_state: str              # systemd SubState: running | exited | dead | failed | ...


def decide_launch(ev: TrialEvidence) -> str:
    """Pure decision. Returns one of: ingest | runner-error | reconcile | unresolved | launch-failed | launch.
    Only "launch" may start a solver; every other branch reads existing state."""
    if ev.has_record or (ev.status and ev.status != "running"):
        return "ingest"
    if ev.has_runner_error:
        return "runner-error"
    if ev.load_state == "loaded":
        return "reconcile"  # the unit exists (running, exited or failed): inspect it, never start another
    if ev.launch_phase == "acknowledged" and ev.launch_returncode not in (None, 0):
        return "launch-failed"  # systemd-run refused; nothing ran
    if ev.launch_phase in ("intent", "acknowledged") or ev.status == "running":
        return "unresolved"  # a launch was attempted and the unit is gone (VM loss, reboot, manual cleanup): reconcile by hand
    if ev.launch_phase == "failed":
        return "launch-failed"
    return "launch"


def systemd_unit_state(unit: str) -> dict:
    """Only an explicit `LoadState=not-found` means the unit is gone; a failing systemctl raises (retryable)."""
    cp = RUN(["systemctl", "--user", "show", unit, "--property=LoadState,ActiveState,SubState,Result"])
    props = dict(line.split("=", 1) for line in (cp.stdout or "").splitlines() if "=" in line)
    load = props.get("LoadState")
    if load is None or (cp.returncode != 0 and load != "not-found"):
        raise UnitStateUnavailable(f"systemctl --user show {unit} failed (rc={cp.returncode}): {((cp.stderr or '') + (cp.stdout or ''))[-300:]}")
    return {"load_state": load, "active_state": props.get("ActiveState", "inactive"),
            "sub_state": props.get("SubState", "dead"), "result": props.get("Result", "")}


def subprocess_unit_state(launch: dict) -> dict:
    """Non-production: liveness of the detached child recorded in launch.json (pid reuse is possible)."""
    pid = launch.get("pid")
    if not pid:
        return {"load_state": "not-found", "active_state": "inactive", "sub_state": "dead", "result": ""}
    proc = _CHILDREN.get(pid)
    if proc is not None:
        rc = proc.poll()
        if rc is None:
            return {"load_state": "loaded", "active_state": "active", "sub_state": "running", "result": ""}
        return {"load_state": "loaded", "active_state": "active" if rc == 0 else "failed", "sub_state": "exited" if rc == 0 else "failed", "result": str(rc)}
    try:
        os.kill(pid, 0)
        return {"load_state": "loaded", "active_state": "active", "sub_state": "running", "result": ""}
    except ProcessLookupError:
        return {"load_state": "loaded", "active_state": "inactive", "sub_state": "dead", "result": "unknown"}
    except PermissionError:
        return {"load_state": "loaded", "active_state": "active", "sub_state": "running", "result": ""}


def unit_state(tid: str, launch: dict | None = None) -> dict:
    launch = launch if launch is not None else _read_json(trial_dir(tid) / "launch.json")
    if launch.get("launcher", LAUNCHER) == "subprocess":
        return subprocess_unit_state(launch)
    return systemd_unit_state(unit_name(tid))


def read_evidence(tid: str) -> TrialEvidence:
    d = trial_dir(tid); launch = _read_json(d / "launch.json"); state = unit_state(tid, launch)
    return TrialEvidence(has_record=(d / "record.json").exists(), has_runner_error=(d / "runner-error.json").exists(),
                         status=_read_json(d / "status.json").get("status"), launch_phase=launch.get("phase"),
                         launch_returncode=launch.get("returncode"), load_state=state["load_state"],
                         active_state=state["active_state"], sub_state=state["sub_state"])


# ------------------------------------------------------------------ trial lifecycle: launch once

def build_trial_command(request_path: Path) -> list[str]:
    python = os.environ.get("SWESIM_TRIAL_PYTHON") or sys.executable
    return [python, "-m", "swesim.workflows.activities", "run-trial", str(request_path)]


def _assert_no_secret_values(parts: list[str]) -> None:
    joined = "\x00".join(parts)
    for k, v in os.environ.items():
        if len(v) >= 8 and any(m in k.upper() for m in SECRET_MARKERS) and v in joined:
            raise RuntimeError(f"refusing to launch: the value of {k} would appear in the unit command line")


def build_systemd_run_argv(unit: str, command: list[str], cwd: Path, env: dict[str, str]) -> list[str]:
    argv = ["systemd-run", "--user", f"--unit={unit}", f"--description=swesim trial {unit}", "--property=RemainAfterExit=yes",
            f"--working-directory={cwd}"]
    argv += [f"--setenv={k}={v}" for k, v in sorted(env.items())]
    argv += list(command)
    _assert_no_secret_values(argv)
    return argv


def _boot_id() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None


def _unit_env() -> dict[str, str]:
    """The only variables a trial unit receives: paths and PATH. Secrets come from the env file via python-dotenv."""
    return {"SWESIM_WORKSPACE": str(WS.root),
            "SWESIM_ENV_FILE": os.environ.get("SWESIM_ENV_FILE", str(Path.home() / "Documents" / ".env")),
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "PYTHONUNBUFFERED": "1"}


def _launch_systemd(unit: str, command: list[str]) -> tuple[int, str, str]:
    cp = RUN(build_systemd_run_argv(unit, command, REPO_ROOT, _unit_env()))
    return cp.returncode, cp.stdout or "", cp.stderr or ""


def _launch_subprocess(command: list[str], d: Path, intent: dict) -> tuple[int, str, str]:
    log = open(d / "launcher.log", "ab")  # noqa: SIM115 - inherited by the detached child
    env = {k: os.environ[k] for k in ("HOME", "USER", "LANG", "TMPDIR") if k in os.environ}  # same shape as the systemd path
    env.update(_unit_env())
    proc = subprocess.Popen(command, cwd=str(REPO_ROOT), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, env=env)
    log.close(); _CHILDREN[proc.pid] = proc; intent["pid"] = proc.pid
    return 0, "", f"detached pid {proc.pid} via subprocess launcher (NOT production)"


def launch_once(p: dict) -> dict:
    """Launch the trial unit exactly once; otherwise report the reconciliation decision without side effects."""
    tid = p["trial_id"]; unit = unit_name(tid); d = trial_dir(tid); d.mkdir(parents=True, exist_ok=True)
    request = d / "request.json"
    if request.exists() and json.loads(request.read_text()) != p:
        raise TrialBindingConflict(f"trial {tid} already bound to a different request")
    decision = decide_launch(read_evidence(tid))
    out = {"trial_id": tid, "unit": unit, "decision": decision, "launched": False, "launcher": LAUNCHER}
    if decision != "launch":
        return out
    atomic_json(request, p)
    intent = {"trial_id": tid, "unit": unit, "phase": "intent", "launcher": LAUNCHER, "started_at": time.time(), "boot_id": _boot_id()}
    atomic_json(d / "launch.json", intent)  # written before systemd-run: a lost acknowledgement is reconciled, never relaunched
    command = build_trial_command(request)
    try:
        if LAUNCHER == "systemd":
            rc, stdout, stderr = _launch_systemd(unit, command)
        elif LAUNCHER == "subprocess":
            rc, stdout, stderr = _launch_subprocess(command, d, intent)
        else:
            raise LaunchFailed(f"unknown launcher {LAUNCHER!r}")
    except Exception as e:
        atomic_json(d / "launch.json", {**intent, "phase": "failed", "error": repr(e)})
        raise LaunchFailed(f"launcher failed for {unit}: {e}") from e
    atomic_json(d / "launch.json", {**intent, "phase": "acknowledged", "returncode": rc, "stdout": stdout[-500:], "stderr": stderr[-1000:]})
    if rc:
        raise LaunchFailed(f"launch of {unit} returned {rc}; reconcile before any new attempt")
    return {**out, "launched": True}


@activity.defn
async def launch_trial_activity(p: dict) -> dict:
    async with LAUNCH_SLOTS:
        return await blocking(lambda: launch_once(p))


@activity.defn
async def await_trial_activity(p: dict) -> dict:
    """Read-only, retryable, heartbeating poll until terminal evidence exists. Never launches."""
    tid = p["trial_id"]; d = trial_dir(tid); unit = unit_name(tid); deadline = time.monotonic() + AWAIT_DEADLINE_SEC; polls = 0
    while True:
        polls += 1
        ev = read_evidence(tid)
        _heartbeat({"trial_id": tid, "poll": polls, "unit": f"{ev.load_state}/{ev.active_state}/{ev.sub_state}", "status": ev.status})
        if ev.has_record:
            r = json.loads((d / "record.json").read_text())
            return {"trial_id": r.get("trial_id", tid), "status": r.get("status", "completed"), "reward": r.get("reward", {})}
        if ev.has_runner_error:
            return {"trial_id": tid, "status": "runner-error", "unit": unit, "error": _read_json(d / "runner-error.json").get("error")}
        decision = decide_launch(ev)
        if decision == "launch-failed":
            return {"trial_id": tid, "status": "launch-failed", "unit": unit, "reason": "launcher refused or returned nonzero; nothing ran; never relaunch automatically"}
        if decision == "unresolved":
            return {"trial_id": tid, "status": "unresolved", "unit": unit,
                    "reason": "launch evidence exists but the unit is gone without a terminal record (VM loss or manual cleanup); inspect the unit journal and Harbor job; never relaunch"}
        if decision == "launch" and polls >= LAUNCH_GRACE_POLLS:
            return {"trial_id": tid, "status": "launch-failed", "unit": unit, "reason": "no launch evidence for this trial; the launch activity did not reach the launcher"}
        if decision == "ingest" and not (ev.active_state in RUNNING_STATES and ev.sub_state != "exited"):
            # Terminal status.json without record.json and the unit is not running: nothing more will appear.
            return {"trial_id": tid, "status": ev.status or "unknown", "unit": unit, "reward": {},
                    "reason": "status.json is terminal but record.json is absent; read the unit journal; never relaunch"}
        if decision == "reconcile":
            if ev.active_state == "failed":
                return {"trial_id": tid, "status": "unit-failed", "unit": unit, "reason": "unit failed without a terminal record; read its journal; never relaunch"}
            if ev.active_state not in RUNNING_STATES or ev.sub_state == "exited":
                return {"trial_id": tid, "status": "unresolved", "unit": unit, "reason": "unit finished without terminal evidence; inspect its journal; never relaunch"}
        if time.monotonic() >= deadline:
            raise ApplicationError("no terminal evidence before the await deadline; inspect the same unit; never relaunch", non_retryable=True)
        await asyncio.sleep(POLL_SECONDS)


# ------------------------------------------------------------------ the per-trial process (runs inside the unit)

def execute_trial_request(request_path: Path) -> dict:
    """Body of the trial unit: load the frozen spec and the exact RunConfig, check identity, run the trial.
    Secrets come from the env file through python-dotenv, never from arguments."""
    p = json.loads(Path(request_path).read_text())
    ws = Workspace(os.environ.get("SWESIM_WORKSPACE", str(WS.root)))
    from dotenv import load_dotenv

    load_dotenv(os.environ.get("SWESIM_ENV_FILE", str(Path.home() / "Documents" / ".env")))
    d = ws.root / "trials" / p["trial_id"]
    try:
        from swesim.stages.run import run_trial

        spec = ScenarioSpec.model_validate_json((ws.scenario_dir(p["scenario_id"]) / "scenario.json").read_text())
        rc = RunConfig.model_validate(p["run_config"])
        if spec.spec_hash != p["spec_hash"]:
            raise ValueError("frozen spec changed")
        expected = trial_id(spec.spec_hash, rc.condition, rc.run_config_hash, p["attempt_index"])
        if expected != p["trial_id"]:
            raise ValueError("trial id does not match its run config; refusing to run under another identity")
        secret = os.environ.get("SWESIM_WORKER_SECRET", "")
        if rc.condition == "interactive" and not secret:
            raise ValueError("SWESIM_WORKER_SECRET missing from the env file")
        rec = run_trial(ws, spec, rc, p["attempt_index"], p["stakeholder_url"], secret)
        return {"trial_id": rec.trial_id, "status": rec.status, "reward": rec.reward}
    except Exception as e:
        atomic_json(d / "runner-error.json", {"trial_id": p["trial_id"], "error": _redact(repr(e)), "type": type(e).__name__, "at": time.time()})
        raise


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or argv[0] != "run-trial":
        print("usage: python -m swesim.workflows.activities run-trial <workspace/trials/<trial_id>/request.json>", file=sys.stderr)
        return 2
    print(json.dumps(execute_trial_request(Path(argv[1]))))
    return 0


ALL = [probe_models_activity, mine_activity, judge_activity, list_candidates_activity, judge_one_activity, draft_activity,
       auto_approve_activity, precheck_draft_activity, human_approve_activity, verify_approval_activity, compile_activity,
       validate_activity, register_activity, plan_trials_activity, launch_trial_activity, await_trial_activity, score_activity,
       export_activity]

if __name__ == "__main__":
    raise SystemExit(main())
