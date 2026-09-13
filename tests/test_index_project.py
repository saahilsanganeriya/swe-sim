"""Projection of record files into index rows (interfaces §2, brief design notes). `build_record` writes a synthetic
record with every file kind the projection reads; the other index tests reuse it."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from swesim.index import models as M
from swesim.index import project as P
from swesim.index import reports as R
from swesim.models import StakeholderConfig
from swesim.paths import Workspace
from swesim.stages.register import register_scenario
from swesim.stages.score import score_scenario
from swesim.storage.layout import Layout

try:  # pytest's default (prepend) import mode puts tests/ on sys.path
    from test_score import COMPILED, HAIKU, IMAGE, T0, ask_events, make_spec, make_workspace, review_events, write_trial, write_unresolved
except ImportError:  # pragma: no cover - importlib mode
    from tests.test_score import COMPILED, HAIKU, IMAGE, T0, ask_events, make_spec, make_workspace, review_events, write_trial, write_unresolved

SID = "example-scenario"
SAT = "saturated-scenario"
GPT = "openai/gpt-5.5"


def confirm_events(t0: float, remaining: int, rid: str = "c1", seq: int = 4) -> list[dict]:
    return [
        {"kind": "message", "ts": t0, "seq": seq, "role": "user", "actor": "coding-agent", "content": "Raising ValueError on bad input; ok?",
         "request_id": rid, "exchange": "confirm"},
        {"kind": "message", "ts": t0 + 2, "seq": seq + 1, "role": "assistant", "actor": "stakeholder", "content": "Yes.", "request_id": rid,
         "exchange": "confirm", "disclosed_fact_ids": [], "tier": "full", "patience_remaining": remaining, "tool_calls": [],
         "reviewed_diff_sha256": None, "delivery": "generated", "regenerated": True, "redacted_terms": ["t1"]},
    ]


def _w(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=1))


@dataclass
class Record:
    ws: Workspace
    layout: Layout
    registry_path: Path
    spec: object
    tids: dict[str, str] = field(default_factory=dict)


def build_record(tmp_path: Path) -> Record:
    spec = make_spec(version=2, with_author_only=True)
    ws = make_workspace(tmp_path, spec)
    layout = Layout(root=ws.root)
    sdir = ws.scenario_dir(SID)
    _w(sdir / "approval.json", {"scenario_id": SID, "spec_hash": spec.spec_hash, "version": 2, "approved_by": "auto-validators",
                                "review_needed": True, "gates": ["schema"], "approved_at": "2026-09-13T06:55:21+00:00"})
    _w(sdir / "scenario.draft.json", json.loads(spec.model_dump_json()))
    _w(sdir / "draft.meta.json", {"version": 2, "spec_hash": spec.spec_hash, "created_at": "2026-09-13T06:54:17+00:00", "model_id": "anthropic/claude-sonnet-5"})
    _w(sdir / "judge.json", {"keep": True, "verified_rubric_score": 2, "complexity": 3, "model_id": GPT})
    _w(sdir / "difficulty.json", {"tier": "informative", "model": "openrouter/deepseek/deepseek-v3.2", "hidden": {"n": 4, "successes": 1},
                                  "full": {"n": 4, "successes": 3}, "labelled_at": "2026-09-13T07:00:00+00:00", "trial_ids": []})
    _w(sdir / "audit.json", {"coherent": True, "fair_tests": True, "plausible_withholding": True, "reasons": {}, "model": "anthropic/claude-opus-5",
                             "audited_at": "2026-09-13T07:10:00+00:00", "rubric_version": "v1"})
    v1 = make_spec(version=1, with_author_only=True)
    adir = sdir / "archive" / "v1"
    _w(adir / "scenario.json", json.loads(v1.model_dump_json()))
    _w(adir / "validation.json", {"status": "valid", "failures": [], "spec_hash": v1.spec_hash, "compiled_hash": "a" * 64, "image_digest": IMAGE})
    _w(adir / "approval.json", {"scenario_id": SID, "spec_hash": v1.spec_hash, "version": 1, "approved_by": "auto-validators"})
    _w(adir / "compile.json", {"compiled_hash": "a" * 64, "spec_hash": v1.spec_hash, "image_digest": IMAGE})
    _w(adir / "archived.json", {"scenario_id": SID, "previous_version": 1, "at": T0 - 3600, "archived": ["scenario.json"]})
    # a draft awaiting approval, an invalid scenario, and a valid but saturated one
    second = make_spec("second-scenario")
    _w(ws.scenario_dir("second-scenario") / "scenario.draft.json", json.loads(second.model_dump_json()))
    _w(ws.scenario_dir("second-scenario") / "draft.meta.json", {"version": 1, "spec_hash": second.spec_hash, "created_at": "2026-09-13T06:54:17+00:00"})
    broken = make_spec("broken-scenario")
    bdir = ws.scenario_dir("broken-scenario")
    _w(bdir / "scenario.json", json.loads(broken.model_dump_json()))
    _w(bdir / "approval.json", {"scenario_id": "broken-scenario", "spec_hash": broken.spec_hash, "version": 1})
    _w(bdir / "compile.json", {"compiled_hash": "b" * 64, "spec_hash": broken.spec_hash, "image_digest": IMAGE, "origin": "vm-systemd"})
    _w(bdir / "validation.json", {"status": "invalid", "failures": ["oracle: failed"], "spec_hash": broken.spec_hash, "compiled_hash": "b" * 64})
    _w(bdir / "difficulty.json", {"tier": "broken", "model": "x", "hidden": {"n": 4, "successes": 0}, "full": {"n": 4, "successes": 0}, "labelled_at": None})
    saturated = make_spec(SAT)
    make_workspace(tmp_path, saturated)
    _w(ws.scenario_dir(SAT) / "difficulty.json", {"tier": "saturated", "model": "x", "hidden": {"n": 4, "successes": 4}, "full": {"n": 4, "successes": 4}, "labelled_at": None})
    _w(ws.scenario_dir(SAT) / "audit.json", {"coherent": True, "fair_tests": True, "plausible_withholding": False, "reasons": {}, "model": "m", "audited_at": None, "rubric_version": "v1"})
    # trials
    tids: dict[str, str] = {}
    events = ask_events(T0 + 6, "Which exception should be raised?", "ValueError.", ["f01"], 5) + review_events(T0 + 30, 3) + confirm_events(T0 + 20, 4)
    tids["done"] = write_trial(ws, spec, events=events, label={"label": "valid", "note": "looks right"})
    tids["wrong"] = write_trial(ws, spec, attempt=1, outcome_code=2, success=0, reward=0.0)
    tids["infra"] = write_trial(ws, spec, attempt=2, outcome_code=7, success=0, reward=0.0)
    tids["full"] = write_trial(ws, spec, condition="full", purpose="comparison")
    tids["runner-error"] = write_unresolved(ws, spec, kind="runner-error", attempt=3)
    tids["launch-failed"] = write_unresolved(ws, spec, kind="launch-failed", attempt=4)
    tids["request-only"] = write_unresolved(ws, spec, kind="request-only", attempt=5)
    tids["stale-running"] = write_unresolved(ws, spec, kind="stale-running", attempt=6)
    tids["launch-only"] = write_unresolved(ws, spec, kind="request-only", attempt=7)
    _w(ws.root / "trials" / tids["launch-only"] / "launch.json", {"trial_id": tids["launch-only"], "phase": "acknowledged", "launcher": "systemd",
                                                                    "unit": f"swesim-trial-{tids['launch-only']}", "returncode": 0, "started_at": T0 + 100,
                                                                    "workflow_id": tids["launch-only"], "run_id": "r-1", "image_digest": "sha256:" + "e" * 64})
    tids["running"] = write_unresolved(ws, spec, kind="request-only", attempt=8)
    _w(ws.root / "trials" / tids["running"] / "launch.json", {"trial_id": tids["running"], "phase": "acknowledged", "launcher": "cloudrun", "handle": "swesim-trial-x", "returncode": 0})
    _w(ws.root / "trials" / tids["running"] / "status.json", {"status": "running", "trial_id": tids["running"], "started_at": "2026-09-13T12:00:00Z", "finished_at": None})
    tids["saturated"] = write_trial(ws, saturated)
    (ws.root / "trials" / "notatrial").mkdir()
    (ws.root / "trials" / ("f" * 24)).mkdir()  # an empty directory: no evidence at all
    done = ws.root / "trials" / tids["done"]
    _w(done / "costs.json", [
        {"cost_id": "01J8Z0000000000000000COST1", "trial_id": tids["done"], "workflow_id": None, "role": "solver", "provider": "anthropic", "model": HAIKU,
         "input_tokens": 1000, "output_tokens": 200, "cached_tokens": 0, "usd": "0.01", "price_source": "anthropic-2026-09", "at": T0 + 60},
        {"cost_id": "01J8Z0000000000000000COST2", "trial_id": tids["done"], "workflow_id": None, "role": "stakeholder", "provider": "openai", "model": GPT,
         "input_tokens": 500, "output_tokens": 50, "cached_tokens": None, "usd": 0.02, "price_source": "openai-2026-09", "at": "2026-09-13T10:02:00+00:00"},
        {"cost_id": "01J8Z0000000000000000COST3", "trial_id": tids["done"], "workflow_id": None, "role": "judge", "provider": "openai", "model": "openai/unknown",
         "input_tokens": 10, "output_tokens": 1, "cached_tokens": None, "usd": None, "price_source": "unknown", "at": T0 + 70},
    ])
    _w(ws.root / "costs" / "create-1.json", [
        {"cost_id": "01J8Z0000000000000000JUDG1", "trial_id": None, "workflow_id": "create-1", "role": "judge", "provider": "openai", "model": GPT,
         "input_tokens": 100, "output_tokens": 10, "cached_tokens": None, "usd": "0.005", "price_source": "openai-2026-09", "at": "2026-09-13T08:00:00+00:00"},
        {"cost_id": "01J8Z0000000000000000JUDG2", "trial_id": None, "workflow_id": "create-1", "role": "drafter", "provider": "anthropic", "model": "anthropic/unknown",
         "input_tokens": 5, "output_tokens": 5, "cached_tokens": None, "usd": None, "price_source": "unknown", "at": "2026-09-13T08:01:00+00:00"},
    ])
    _w(done / "audit.json", {"questions": [{"seq": 0, "specific": True, "decision_oriented": True, "before_implementation": True, "redundant": False, "fishing": False, "reason": "asks the exception type"}],
                             "replies": [{"seq": 1, "targeted": True, "volunteered": False, "test_or_identifier_disclosed": False, "behaviour_level": True, "reason": "names the behaviour"}],
                             "asked_before_implementing": True, "useful_question_rate": 1.0, "model": "anthropic/claude-opus-5", "audited_at": "2026-09-13T11:00:00+00:00", "rubric_version": "v1"})
    register_scenario(ws, spec)
    register_scenario(ws, saturated)
    score_scenario(ws, SID)
    score_scenario(ws, SAT)
    # candidates: a whole-file funnel (SWE-bench) and a per-candidate funnel (repository source)
    cdir = ws.candidates_dir
    cdir.mkdir(parents=True, exist_ok=True)
    rows = [{"instance_id": SID, "repo": "example/repo", "source": "swebench-annotated", "problem_statement": "It breaks."},
            {"instance_id": "other__lib-12", "repo": "other/lib", "problem_statement": "x"},
            {"instance_id": "third__lib-7", "repo": "third/lib", "problem_statement": "y"}]
    (cdir / "swebench-annotated.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n")
    (cdir / "swebench-annotated.judged.jsonl").write_text(json.dumps({**rows[0], "judge": {"keep": True, "model_id": GPT}}) + "\n"
                                                          + json.dumps({**rows[1], "judge": {"keep": False, "model_id": GPT}}) + "\n")
    (cdir / "swebench-annotated.judged.probed.jsonl").write_text(json.dumps({**rows[0], "judge": {"keep": True}, "probe": {"statuses": {"t1": "failed"}, "stages": ["base", "gold"]}}) + "\n")
    (cdir / "github-repo--fourth__lib.jsonl").write_text(json.dumps({"instance_id": "fourth__lib-3", "repo": "fourth/lib", "source": "github-repo", "discovery": {"kind": "bugfix"}}) + "\n")
    (cdir / "judged").mkdir()
    (cdir / "judged" / "fourth--lib-3.judged.jsonl").write_text(json.dumps({"instance_id": "fourth__lib-3", "source": "github-repo", "judge": {"keep": True}}) + "\n")
    (cdir / "judged" / "fourth--lib-3.judged.probed.jsonl").write_text(json.dumps({"instance_id": "fourth__lib-3", "source": "github-repo", "judge": {"keep": False},
                                                                                    "reject_reason": "not-reproducible-offline", "probe": {"statuses": {}, "error": "ValueError: nothing fails on base"}}) + "\n")
    (cdir / "github-repo--fourth__lib.rejects.json").write_text(json.dumps({"no-linked-issue": 3}))
    (cdir / "github-repo--fifth__lib.jsonl").write_text(json.dumps({"instance_id": "fifth__lib-9", "repo": "fifth/lib", "source": "github-repo"}) + "\n")
    (cdir / "fifth__lib-9.error.json").write_text(json.dumps({"error": "unreliable base/gold probe; keep evidence and reject"}))
    # a Harbor job directory (jobs/runs/<tid>) and the console's action log
    jdir = ws.jobs_dir / "runs" / tids["done"]
    _w(jdir / "config.json", {"job_name": tids["done"], "agents": [{"name": "mini-swe-agent", "model_name": HAIKU}], "tasks": [{"path": str(done / "task")}]})
    _w(jdir / "result.json", {"id": "job-uuid-1", "started_at": "2026-09-12T20:34:24.435808", "finished_at": "2026-09-12T20:35:10.777282", "n_total_trials": 1,
                              "stats": {"n_completed_trials": 1, "n_errored_trials": 0, "n_running_trials": 0, "n_cancelled_trials": 0, "evals": {"e": {"exception_stats": {}}}}})
    _w(jdir / "lock.json", {"created_at": "2026-09-13T03:34:24.440426Z", "harbor": {"version": "0.23.0"}})
    actions = ws.root / "evidence" / "console" / "actions.jsonl"
    actions.parent.mkdir(parents=True)
    actions.write_text(json.dumps({"action_id": "01J8Z0000000000000000ACT01", "actor": "a@example.com", "action": "run-tasks", "target": SID, "params": {"k": 3},
                                   "confirmed": True, "origin_header": "https://console.example", "result": "accepted", "workflow_id": "run-1",
                                   "at": "2026-09-13T12:00:00+00:00", "origin": "laptop"}) + "\n{not json}\n")
    return Record(ws=ws, layout=layout, registry_path=ws.registry_path, spec=spec, tids=tids)


@pytest.fixture(scope="module")
def record(tmp_path_factory) -> Record:
    return build_record(tmp_path_factory.mktemp("record"))


@pytest.fixture(scope="module")
def registry(record: Record) -> dict:
    return P.project_registry(record.layout, record.registry_path)


# ----------------------------------------------------------------------------- primitives

def test_primitives():
    assert P.aware(T0) == datetime(2026, 9, 13, 10, 0, tzinfo=UTC) and P.aware("2026-09-13T10:00:00Z") == P.aware(T0)
    assert P.aware("2026-09-13T10:00:00") == P.aware(T0) and P.aware("2026-09-13T03:00:00-07:00") == P.aware(T0)
    assert P.aware(None) is None and P.aware(True) is None and P.aware("soon") is None and P.aware(float("nan")) is None
    assert P.dec(0.1) == Decimal("0.1") and P.dec("1.5") == Decimal("1.5") and P.dec(3) == Decimal(3) and P.dec(float("inf")) is None
    assert P.dec(True) is None and P.dec("x") is None and isinstance(P.dec(0.5), Decimal)
    assert P.provider_of("openrouter/deepseek/deepseek-v3.2") == "openrouter" and P.provider_of("anthropic/claude-opus-5") == "anthropic"
    assert P.scenario_id_for("Py-PDF__pypdf-3826") == "py-pdf--pypdf-3826" and P.scenario_id_for("a/b") is None
    assert P.pr_number_of("mwaskom__seaborn-3190") == 3190 and P.pr_number_of("example-scenario") is None


# ----------------------------------------------------------------------------- scenarios

def test_registry_and_scenario_versions(record: Record, registry: dict):
    assert set(registry) == {SID, SAT}
    rows = P.project_scenario_versions(record.layout, SID, registry=registry, origin="laptop")
    assert [r.version for r in rows] == [1, 2]
    v1, v2 = rows
    assert v1.validation_status == "archived" and v1.archived_at == P.aware(T0 - 3600) and v1.registry_json is None and v1.compiled_hash == "a" * 64
    assert v2.validation_status == "valid" and v2.spec_hash == record.spec.spec_hash and v2.compiled_hash == COMPILED and v2.image_digest == IMAGE
    assert v2.source == "authored" and v2.source_ref == "example" and v2.difficulty_tier == "informative"
    assert v2.difficulty_screen == {"model": "openrouter/deepseek/deepseek-v3.2", "hidden": {"n": 4, "successes": 1}, "full": {"n": 4, "successes": 3},
                                    "labelled_at": "2026-09-13T07:00:00+00:00"}
    assert v2.task_sense["coherent"] is True and v2.registry_json["spec_hash"] == record.spec.spec_hash and v2.origin == "laptop"
    same = {"exclude": {"created_at", "updated_at"}}
    assert P.project_scenario(record.layout, SID, 1, registry=registry, origin="laptop").model_dump(**same) == v1.model_dump(**same)
    assert P.project_scenario(record.layout, SID, 2, registry=registry, origin="laptop").model_dump(**same) == v2.model_dump(**same)


def test_validation_status_is_derived_from_the_files(record: Record, registry: dict):
    second = P.project_scenario(record.layout, "second-scenario", registry=registry, origin="laptop")
    assert second.validation_status == "drafting" and second.version == 1 and second.spec_hash is not None and second.registry_json is None
    broken = P.project_scenario(record.layout, "broken-scenario", registry=registry, origin="laptop")
    assert broken.validation_status == "invalid" and broken.difficulty_tier == "broken" and broken.origin == "vm-systemd"  # the file's label wins
    saturated = P.project_scenario(record.layout, SAT, registry=registry, origin="laptop")
    assert saturated.validation_status == "valid" and saturated.difficulty_tier == "saturated"
    assert P.scenario_ids(record.layout, registry) == ["broken-scenario", SID, SAT, "second-scenario"]
    with pytest.raises(P.ProjectionError):
        P.project_scenario(record.layout, "no-such-scenario", registry=registry, origin="laptop")
    with pytest.raises(P.ProjectionError):
        P.project_scenario(record.layout, SID, 5, registry=registry, origin="laptop")
    for present, files, expected in (
        ({"scenario.json", "validation.json"}, {"validation.json": {"status": "valid"}}, "valid"),
        ({"scenario.json", "validation.json"}, {"validation.json": {"status": "invalid"}}, "invalid"),
        ({"scenario.json", "compile.json"}, {}, "validating"),
        ({"scenario.json", "approval.json"}, {}, "compiling"),
        ({"scenario.draft.json", "review.json"}, {"review.json": {"decision": "reject"}}, "rejected"),
        ({"scenario.draft.json"}, {}, "drafting"),
    ):
        full = {name: files.get(name, {}) for name in P.SCENARIO_FILES}
        assert P.validation_status_of(present, full) == expected, (present, expected)


# ----------------------------------------------------------------------------- candidates

def test_candidates_reach_their_furthest_stage(record: Record, registry: dict):
    assert P.source_keys(record.layout) == ["github-repo--fifth__lib", "github-repo--fourth__lib", "swebench-annotated"]
    rows = {r.instance_id: r for r in P.project_candidates(record.layout, "swebench-annotated", registry=registry, origin="laptop")}
    assert set(rows) == {SID, "other__lib-12", "third__lib-7"}
    reg = rows[SID]
    assert (reg.stage, reg.status, reg.gate) == ("registered", "kept", None) and reg.scenario_id == SID and reg.version == 2 and reg.source == "swebench-annotated"
    assert reg.repo == "example/repo" and reg.pr_number is None and reg.judged_at is not None
    rej = rows["other__lib-12"]
    assert (rej.stage, rej.status, rej.gate) == ("judged", "rejected", "judge") and rej.reasons and rej.pr_number == 12 and rej.scenario_id is None
    assert (rows["third__lib-7"].stage, rows["third__lib-7"].status, rows["third__lib-7"].judged_at) == ("mined", "pending", None)
    fourth = P.project_candidates(record.layout, "github-repo--fourth__lib", registry=registry, origin="laptop")[0]
    assert (fourth.stage, fourth.status, fourth.gate) == ("probed", "rejected", "probe") and fourth.reasons[0] == "not-reproducible-offline"
    assert fourth.source == "github-repo" and fourth.pr_kind == "bugfix" and fourth.pr_number == 3
    fifth = P.project_candidates(record.layout, "github-repo--fifth__lib", registry=registry, origin="laptop")[0]
    assert (fifth.stage, fifth.status, fifth.gate) == ("probed", "error", "probe") and "unreliable" in fifth.reasons[0]
    steps = {s.stage: s for s in R.funnel_steps([*rows.values(), fourth, fifth])}
    assert [s for s in steps] == list(M.FUNNEL_ORDER)
    assert (steps["mined"].kept, steps["mined"].pending) == (4, 1)
    assert (steps["judged"].kept, steps["judged"].rejected) == (3, 1)
    assert (steps["probed"].kept, steps["probed"].rejected, steps["probed"].error) == (1, 1, 1)
    assert steps["registered"].kept == 1 and steps["drafted"].kept == 1


# ----------------------------------------------------------------------------- trials

def test_completed_trial_projects_row_exchanges_and_costs(record: Record):
    tid = record.tids["done"]
    row, exchanges, costs = P.project_trial(record.layout, tid, origin="laptop")
    assert row.trial_id == tid and row.scenario_id == SID and row.version == 2 and row.spec_hash == record.spec.spec_hash
    assert (row.condition, row.brief_profile, row.purpose, row.status) == ("interactive", "withheld", "calibration", "completed")
    assert row.model == HAIKU and row.provider == "anthropic" and row.attempt_index == 0
    assert row.outcome_code == 1 and row.reward == Decimal("1") and isinstance(row.reward, Decimal) and row.success is True
    assert row.integrity_signal_count == 0 and row.questions_asked == 1 and row.confirmations == 1 and row.asked_before_implementing is True
    assert row.started_at == datetime(2026, 9, 13, 9, 59, tzinfo=UTC) and row.finished_at == datetime(2026, 9, 13, 10, 5, tzinfo=UTC)
    assert row.launcher is None and row.job_name is None
    assert row.cost_usd == Decimal("0.03") and row.input_tokens == 1510 and row.output_tokens == 251  # tokens include the unpriced record
    default_stakeholder = StakeholderConfig()  # the RunConfig the fixture ran with carries the current defaults
    assert row.stakeholder_model == default_stakeholder.model_id and row.stakeholder_prompt_version == default_stakeholder.prompt_version
    assert row.origin == "laptop"
    assert [e.seq for e in exchanges] == [0, 1, 2, 3, 4, 5]
    by_seq = {e.seq: e for e in exchanges}
    assert (by_seq[0].role, by_seq[0].exchange, by_seq[0].text_public) == ("agent", "ask", "Which exception should be raised?")
    assert (by_seq[1].role, by_seq[1].exchange, by_seq[1].patience_after, by_seq[1].disclosed_fact_ids) == ("stakeholder", "ask", 5, ["f01"])
    assert by_seq[0].audit["specific"] is True and by_seq[1].audit["targeted"] is True and by_seq[2].audit is None
    assert (by_seq[2].exchange, by_seq[3].exchange, by_seq[4].exchange, by_seq[5].exchange) == ("review", "review", "confirm", "confirm")
    assert by_seq[5].regenerated is True and by_seq[5].redacted_terms == ["t1"] and by_seq[1].ts == P.aware(T0 + 8) and by_seq[0].patience_after is None
    assert [c.role for c in costs] == ["solver", "stakeholder"] and all(c.trial_id == tid for c in costs)
    assert costs[1].usd == Decimal("0.02") and isinstance(costs[1].usd, Decimal) and costs[1].at == datetime(2026, 9, 13, 10, 2, tzinfo=UTC)
    public = R.blank_exchange(by_seq[5])
    assert (public.disclosed_fact_ids, public.redacted_terms, public.regenerated, public.audit) == ([], [], None, None) and public.text_public == "Yes."


def test_other_outcomes_and_conditions(record: Record):
    wrong, _e, _c = P.project_trial(record.layout, record.tids["wrong"], origin="laptop")
    assert (wrong.outcome_code, wrong.success, wrong.reward, wrong.questions_asked, wrong.cost_usd) == (2, False, Decimal(0), 0, None)
    infra, _e, _c = P.project_trial(record.layout, record.tids["infra"], origin="laptop")
    assert infra.outcome_code == 7 and infra.status == "completed"
    full, exchanges, _c = P.project_trial(record.layout, record.tids["full"], origin="laptop")
    assert (full.condition, full.brief_profile, full.purpose, full.questions_asked) == ("full", "natural", "comparison", 0) and exchanges == []


def test_attempts_without_a_record_get_a_status_from_the_evidence(record: Record):
    def status(name: str) -> M.TrialRow:
        return P.project_trial(record.layout, record.tids[name], origin="laptop")[0]

    runner = status("runner-error")
    assert runner.status == "runner-error" and runner.outcome_code is None and runner.questions_asked is None and runner.attempt_index == 3
    assert status("launch-failed").status == "launch-failed"
    planned = status("request-only")
    assert planned.status == "planned" and planned.started_at is None
    launched = status("launch-only")
    assert (launched.status, launched.launcher, launched.job_name) == ("unresolved", "systemd", f"swesim-trial-{record.tids['launch-only']}")
    assert (launched.workflow_id, launched.run_id, launched.worker_image_digest) == (record.tids["launch-only"], "r-1", "sha256:" + "e" * 64)
    assert launched.started_at == P.aware(T0 + 100)
    running = status("running")
    assert (running.status, running.launcher, running.job_name) == ("running", "cloudrun", "swesim-trial-x")
    with pytest.raises(P.ProjectionError):  # status.json alone cannot identify the trial
        status("stale-running")
    with pytest.raises(P.ProjectionError):  # an empty directory: no evidence
        P.project_trial(record.layout, "f" * 24, origin="laptop")
    with pytest.raises(P.ProjectionError):
        P.project_trial(record.layout, "0" * 24, origin="laptop")
    assert "notatrial" not in P.trial_ids(record.layout) and "f" * 24 not in P.trial_ids(record.layout)
    assert len(P.trial_ids(record.layout)) == len(record.tids)


def test_workflow_costs_come_from_the_costs_directory(record: Record, tmp_path: Path):
    rows = P.project_workflow_costs(record.layout, origin="laptop")
    assert [(r.cost_id, r.workflow_id, r.trial_id, r.usd) for r in rows] == [("01J8Z0000000000000000JUDG1", "create-1", None, Decimal("0.005"))]
    assert P.project_workflow_costs(Layout(root=tmp_path), origin="laptop") == []
    bad = Layout(root=tmp_path)
    (tmp_path / "costs").mkdir()
    (tmp_path / "costs" / "w.json").write_text("{}")
    with pytest.raises(P.ProjectionError):
        P.project_workflow_costs(bad, origin="laptop")


def test_costs_file_must_be_a_list_of_records(tmp_path: Path):
    bad = tmp_path / "costs.json"
    bad.write_text(json.dumps({"usd": 1}))
    with pytest.raises(P.ProjectionError):
        P.project_costs("a" * 24, bad, origin="laptop")
    bad.write_text(json.dumps([{"cost_id": "x", "usd": "0.1"}]))  # missing required fields
    with pytest.raises(P.ProjectionError):
        P.project_costs("a" * 24, bad, origin="laptop")
    assert P.project_costs("a" * 24, tmp_path / "absent.json", origin="laptop") == ([], None, None, None)


# ----------------------------------------------------------------------------- scores, jobs, leaderboard

def test_scores_project_per_group(record: Record):
    rows = P.project_scores(record.layout, SID, origin="laptop")
    assert len(rows) == 2 and all(r.spec_hash == record.spec.spec_hash and r.scenario_id == SID for r in rows)
    by_cond = {r.condition: r for r in rows}
    inter = by_cond["interactive"]
    assert (inter.n, inter.success_count, inter.brief_profile, inter.model) == (2, 1, "withheld", HAIKU)
    assert inter.pass_at_k is None and inter.mean_reward == Decimal("0.5") and isinstance(inter.mean_reward, Decimal)
    assert (inter.questions_asked, inter.confirmations) == (1, 1) and inter.computed_at.tzinfo is not None
    assert (by_cond["full"].n, by_cond["full"].brief_profile) == (1, "natural")
    assert P.project_scores(record.layout, "second-scenario", origin="laptop") == []


def test_harbor_job_directories_become_workflow_rows(record: Record):
    rows = P.project_workflows_from_jobs(record.layout, origin="laptop")
    assert [r.workflow_id for r in rows] == [record.tids["done"]]
    w = rows[0]
    assert (w.type, w.status, w.run_id, w.failure) == ("harbor-job", "completed", "job-uuid-1", None)
    assert w.started_at == datetime(2026, 9, 13, 3, 34, 24, 440426, tzinfo=UTC)
    assert w.closed_at is not None and 46 <= (w.closed_at - w.started_at).total_seconds() <= 47
    assert w.params_summary["model"] == HAIKU and w.params_summary["trial_id"] == record.tids["done"]


def test_leaderboard_gate(record: Record, registry: dict):
    trials = [P.project_trial(record.layout, tid, origin="laptop")[0] for name, tid in record.tids.items() if name != "stale-running"]
    scenarios = [row for sid in P.scenario_ids(record.layout, registry) for row in P.project_scenario_versions(record.layout, sid, registry=registry, origin="laptop")]
    board = R.compute_leaderboard(trials, scenarios, M.LeaderboardFilter(purpose="calibration"))
    assert len(board) == 1
    cell = board[0]
    assert (cell.model, cell.condition, cell.brief_profile, cell.n, cell.scenarios, cell.success_count) == (HAIKU, "interactive", "withheld", 2, 1, 1)
    assert cell.pass_rate == Decimal("0.5") and cell.mean_reward == Decimal("0.5") and cell.questions_asked == 1 and cell.confirmations == 1
    assert cell.cost_usd == Decimal("0.03") and cell.cost_per_success == Decimal("0.03")
    with_saturated = R.compute_leaderboard(trials, scenarios, M.LeaderboardFilter(purpose="calibration", include_saturated=True))
    assert (with_saturated[0].n, with_saturated[0].scenarios) == (3, 2)
    assert R.compute_leaderboard(trials, scenarios, M.LeaderboardFilter(purpose="comparison"))[0].condition == "full"
    assert R.compute_leaderboard(trials, scenarios, M.LeaderboardFilter(purpose="calibration", min_n=3)) == []
    # the gate: not valid, not informative, or not coherent/fair -> excluded
    ungated = [s.model_copy(update={"task_sense": {"coherent": False, "fair_tests": True}}) for s in scenarios]
    assert R.compute_leaderboard(trials, ungated, M.LeaderboardFilter(purpose="calibration")) == []
    broken = [s.model_copy(update={"difficulty_tier": "broken"}) for s in scenarios]
    assert R.compute_leaderboard(trials, broken, M.LeaderboardFilter(purpose="calibration", include_saturated=True)) == []
