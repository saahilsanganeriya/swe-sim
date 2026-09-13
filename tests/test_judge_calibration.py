"""Judge calibration (B-P2-8): per-instance predictions with the positive defined as `verified_rubric_score >= 2`,
agreement/precision/recall over the judged rows, the usage and cost of the run, the file under
`Layout.calibration_path`, and the judge's own usage on every judged row. Offline: the judge call is faked."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path


from swesim import costs, llm
from swesim.sources import swebench_annotated as sb
from swesim.stages import judge
from swesim.storage.layout import Layout

CSV = Path(__file__).resolve().parents[1] / "data" / "annotations" / "ensembled_annotations_public.csv"
OPUS = "anthropic/claude-opus-5"


def _rows(n_pos: int = 3, n_neg: int = 3):
    ann = sb.annotation_rows(CSV)
    pos = [i for i in ann if float(ann[i]["underspecified"]) >= 2][:n_pos]
    neg = [i for i in ann if float(ann[i]["underspecified"]) < 2][:n_neg]
    return pos, neg, ann, {i: {"instance_id": i} for i in pos + neg}


def _fake_judge(scores: dict, usage: bool = True):
    def judge_candidate(row, model_id):
        iid = row["instance_id"]
        if isinstance(scores[iid], Exception):
            raise scores[iid]
        out = {"verified_rubric_score": scores[iid], "keep": scores[iid] >= 2, "gaps": [{"id": "g"}] * scores[iid],
               "model_id": model_id, "prompt_version": judge.JUDGE_PROMPT_VERSION}
        if usage:
            out["usage"] = costs.Usage(input_tokens=1000, output_tokens=100, cached_tokens=0, calls=1).model_dump(mode="json")
        return out

    return judge_candidate


def test_calibration_records_per_instance_predictions_with_the_threshold_positive(monkeypatch):
    pos, neg, ann, rows = _rows()
    scores = {pos[0]: 3, pos[1]: 2, pos[2]: 1, neg[0]: 0, neg[1]: 2, neg[2]: ValueError("candidate exceeds authoring context budget")}
    monkeypatch.setattr(judge, "judge_candidate", _fake_judge(scores))
    out = judge.calibrate(CSV, rows, OPUS, n=6, seed=0)
    assert out["positive"] == "verified_rubric_score >= 2" and out["threshold"] == 2 and out["model_id"] == OPUS and out["seed"] == 0
    assert out["n"] == out["n_judged"] == 5 and out["n_errored"] == 1 and out["n_requested"] == 6
    by = {p["instance_id"]: p for p in out["per_instance"]}
    assert set(by) == {pos[0], pos[1], pos[2], neg[0], neg[1]}
    assert by[pos[0]] == {"instance_id": pos[0], "human": float(ann[pos[0]]["underspecified"]), "model": 3, "human_positive": True,
                          "model_positive": True, "outcome": "tp", "keep": True, "gaps": 3}
    assert by[pos[2]]["outcome"] == "fn" and by[neg[1]]["outcome"] == "fp" and by[neg[0]]["outcome"] == "tn"
    assert out["confusion"] == {"tp": 2, "fp": 1, "tn": 1, "fn": 1}
    assert out["agreement"] == 3 / 5 and out["precision"] == 2 / 3 and out["recall"] == 2 / 3
    assert [p["instance_id"] for p in out["per_instance"]] == sorted(by)                  # deterministic regardless of worker order
    assert out["usage"]["calls"] == 5 and out["usage"]["input_tokens"] == 5000 and out["usage"]["output_tokens"] == 500
    assert Decimal(out["cost_usd"]) == Decimal("0.0375") and out["price_source"] == "anthropic-pricing-page"   # 5000 x $5 + 500 x $25 per M
    assert out["errors"] == [{"instance_id": neg[2], "error": "ValueError: candidate exceeds authoring context budget"}]
    assert out["prompt_version"] == judge.JUDGE_PROMPT_VERSION and out["started_at"] <= out["finished_at"]


def test_calibration_with_workers_gives_the_same_result_as_sequential(monkeypatch):
    pos, neg, ann, rows = _rows(4, 4)
    monkeypatch.setattr(judge, "judge_candidate", _fake_judge({i: 3 for i in pos} | {i: 0 for i in neg}))
    seq = judge.calibrate(CSV, rows, OPUS, n=8, seed=1)
    par = judge.calibrate(CSV, rows, OPUS, n=8, seed=1, workers=4)
    for k in ("per_instance", "confusion", "agreement", "precision", "recall", "usage", "cost_usd", "n", "errors"):
        assert seq[k] == par[k], k
    assert seq["agreement"] == 1.0 and seq["confusion"] == {"tp": 4, "fp": 0, "tn": 4, "fn": 0}


def test_calibration_tolerates_judged_rows_without_usage(monkeypatch):
    pos, neg, ann, rows = _rows(1, 1)
    monkeypatch.setattr(judge, "judge_candidate", _fake_judge({pos[0]: 2, neg[0]: 0}, usage=False))
    out = judge.calibrate(CSV, rows, "fake/model", n=2)
    assert out["usage"] is None and out["cost_usd"] is None and out["price_source"] is None and out["n"] == 2
    assert out["per_instance"][0]["keep"] is not None                                        # keep and gaps come from the judged row
    monkeypatch.setattr(judge, "judge_candidate", lambda row, model_id: {"verified_rubric_score": 2})
    out2 = judge.calibrate(CSV, rows, "fake/model", n=2)
    assert all(p["keep"] is None and p["gaps"] is None for p in out2["per_instance"])       # absent fields are unknown, never invented


def test_run_calibration_writes_under_layout_calibration_path_and_records_the_judge_cost(tmp_path, monkeypatch):
    pos, neg, ann, rows = _rows(2, 2)
    monkeypatch.setattr(judge, "judge_candidate", _fake_judge({pos[0]: 2, pos[1]: 3, neg[0]: 1, neg[1]: 0}))
    layout = Layout(root=tmp_path / "ws")
    ref = {"model_id": "openai/gpt-5.5", "agreement": 0.655, "precision": 0.644, "recall": 0.460, "source": "workspace/judge_calibration.json"}
    day = date(2026, 9, 13)
    path = judge.run_calibration(layout, CSV, rows, OPUS, n=4, seed=0, day=day, reference=ref)
    assert path == layout.calibration_path(OPUS, day) == tmp_path / "ws" / "calibration" / "anthropic--claude-opus-5" / "2026-09-13.json"
    saved = json.loads(path.read_text())
    assert saved["reference"] == ref and saved["confusion"] == {"tp": 2, "fp": 0, "tn": 2, "fn": 0} and len(saved["per_instance"]) == 4
    assert saved["day"] == "2026-09-13" and saved["model_id"] == OPUS and saved["positive"] == "verified_rubric_score >= 2"
    cost_path = tmp_path / "ws" / "costs" / "calibration-anthropic--claude-opus-5-2026-09-13.json"
    rows_ = costs.read_costs(cost_path)
    assert len(rows_) == 1 and rows_[0].role == "judge" and rows_[0].model == OPUS and rows_[0].input_tokens == 4000 and rows_[0].trial_id is None
    assert Decimal(saved["cost_usd"]) == rows_[0].usd and saved["cost_record_id"] == rows_[0].cost_id
    judge.run_calibration(layout, CSV, rows, OPUS, n=4, seed=0, day=day, reference=ref)    # rerun: rewritten in place, identical usage not double-billed
    assert len(costs.read_costs(cost_path)) == 1 and json.loads(path.read_text())["confusion"] == saved["confusion"]


def test_judge_candidate_carries_the_calls_usage_when_the_llm_layer_recorded_one(monkeypatch):
    row = {"instance_id": "o__r-1", "problem_statement": "It breaks on empty input", "test_patch": "", "patch": "", "thread": {},
           "fail_to_pass": ["tests/test_a.py::test_b"], "pass_to_pass": []}

    def fake_chat(model_id, system, messages, schema_hint, temperature=None, max_tokens=4000, **kw):
        llm.note_usage(costs.Usage(input_tokens=10, output_tokens=2, calls=1))                # what chat_json_with_usage does for the thread
        return {"gaps": [], "verified_rubric_score": 1, "complexity": 1, "test_behavioralness": 3, "self_contained": True,
                "alternatives_accepted": False, "rationale": "r"}

    monkeypatch.setattr(judge, "chat_json", fake_chat)
    out = judge.judge_candidate(row, OPUS)
    assert out["usage"] == {"input_tokens": 10, "output_tokens": 2, "cached_tokens": None, "calls": 1, "reported_cost_usd": None,
                            "reported_cost_source": None, "agent_reported_cost_usd": None}
    assert out["verified_rubric_score"] == 1 and out["model_id"] == OPUS
    monkeypatch.setattr(judge, "chat_json", lambda *a, **k: {"gaps": [], "verified_rubric_score": 0, "self_contained": True})
    assert judge.judge_candidate(row, OPUS)["usage"] is None                                 # a seam that records nothing: unknown, not zero
