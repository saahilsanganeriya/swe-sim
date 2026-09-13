"""The creation funnel reconciles stage to stage (regression for the hosted Home funnel that did not add up).

The fixture reproduces the three measured causes: (1) the same candidate instance in two candidate files (the legacy
`candidates/<key>.judged.jsonl`, a per-instance `probe_<instance>.jsonl`, a second source key that scanned the same PR)
counted as two candidates; (2) candidates kept by the judge but never advanced (legacy chain not selecting them, a
frozen plan's cap, a stopped workflow) vanishing instead of reading "pending, why"; (3) an archived v1 of a rebuilt
scenario vanishing instead of reading "superseded". Every candidate version gets exactly one terminal state at the last
stage it reached, `entered[n+1] == kept[n]` at every stage, and the last stage's kept are the registry's scenarios.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

import pytest

from swesim.dashboard import charts
from swesim.index import models as M
from swesim.index import project as P
from swesim.index import reports as R
from swesim.index.files import FileIndexReader
from swesim.settings import Settings
from swesim.storage import history
from swesim.storage.layout import Layout

KEY = "swebench-annotated"
OTHER_KEY = "github-repo--mwaskom__seaborn--bugfix"  # a repository scan that listed the same PR as the SWE-bench pool
A, B, C, D = "pytest-dev__pytest-7283", "mwaskom__seaborn-3190", "sphinx-doc__sphinx-9231", "psf__requests-1537"
E, F, G, H = "astropy__astropy-13469", "matplotlib__matplotlib-24189", "pydata__xarray-4759", "scikit-learn__scikit-learn-10452"
T0 = datetime(2026, 9, 13, 10, 0, tzinfo=UTC)
IMAGE = "sha256:" + "d" * 64


def _w(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) if not isinstance(value, str) else value)


def _jsonl(path: Path, rows: list[dict]) -> None:
    _w(path, "".join(json.dumps(r) + "\n" for r in rows))


def _row(iid: str, **extra) -> dict:
    return {"instance_id": iid, "repo": iid.split("-")[0].replace("__", "/"), "source": KEY, "problem_statement": "It breaks.", **extra}


def _scenario(layout: Layout, sid: str, source_ref: str, version: int, *, valid: bool = True, archive_v1: bool = False) -> None:
    sdir = layout.scenario_dir(sid)
    spec = {"id": sid, "version": version, "source": KEY, "source_ref": source_ref}
    _w(sdir / "scenario.draft.json", spec)
    _w(sdir / "draft.meta.json", {"version": version, "spec_hash": "h" * 64})
    _w(sdir / "approval.json", {"scenario_id": sid, "version": version, "spec_hash": "h" * 64, "approved_by": "auto-validators"})
    _w(sdir / "scenario.json", spec)
    _w(sdir / "compile.json", {"compiled_hash": "c" * 64, "spec_hash": "h" * 64, "image_digest": IMAGE})
    _w(sdir / "validation.json", {"status": "valid" if valid else "invalid", "failures": [] if valid else ["oracle: not fully correct"], "spec_hash": "h" * 64})
    if archive_v1:
        adir = sdir / "archive" / "v1"
        _w(adir / "scenario.json", {**spec, "version": 1})
        _w(adir / "approval.json", {"scenario_id": sid, "version": 1, "spec_hash": "g" * 64})
        _w(adir / "compile.json", {"compiled_hash": "a" * 64, "spec_hash": "g" * 64, "image_digest": IMAGE})
        _w(adir / "validation.json", {"status": "valid", "failures": [], "spec_hash": "g" * 64})
        _w(adir / "archived.json", {"scenario_id": sid, "previous_version": 1, "at": T0.timestamp() - 3600, "archived": ["scenario.json"]})


def _receipt(layout: Layout, **fields) -> M.CandidateEventRow:
    data = {"source_key": KEY, "candidates_path": None, "scenario_id": None, "version": 1, "spec_hash": None, "phase": "register",
            "result_status": None, "stage": "registered", "status": "kept", "gate": None, "reasons": [], "terminal": True,
            "occurred_at": T0, "workflow_id": "create:x", "run_id": "run-1", "origin": "cloudrun", **fields}
    row = M.CandidateEventRow(event_id=M.candidate_event_identity(data), **data)
    history.persist_row(layout, row)
    return row


def _workflow(layout: Layout, workflow_id: str, status: str) -> None:
    history.persist_row(layout, M.WorkflowRow(workflow_id=workflow_id, run_id="run-" + workflow_id, type="CreateTasksWorkflow", parent_workflow_id=None,
                                              status=status, params_summary={}, failure="worker lost" if status == "failed" else None, started_at=T0,
                                              closed_at=None if status == "running" else T0, task_queue="swesim-authoring", cost_usd=None,
                                              search_attributes={}, origin="cloudrun"))


def _plan(layout: Layout, workflow_id: str, offered: list[dict], selected: list[str]) -> Path:
    """A frozen candidate plan as `prepare_candidate_plan_activity` writes it: the source snapshot and the selected items."""
    d = layout.root / "evidence" / "workflows" / hashlib.sha256(workflow_id.encode()).hexdigest()[:32]
    snapshot = d / "0.candidates.jsonl"
    _jsonl(snapshot, offered)
    items = [{"instance_id": iid, "scenario_id": P.scenario_id_for(iid), "source_key": KEY, "candidates_path": "/opt/swesim/workspace/evidence/workflows/x/" + snapshot.name}
             for iid in selected]
    _w(d / (hashlib.sha256(b"candidates").hexdigest()[:24] + ".plan.json"), {"items": items, "duplicates": 0})
    return snapshot


@pytest.fixture
def record(tmp_path: Path) -> tuple[Layout, Path]:
    layout = Layout(root=tmp_path / "workspace")
    cdir = layout.candidates_dir()
    mined = [_row(x) for x in (A, B, C, D, E, F, G, H)]
    _jsonl(cdir / f"{KEY}.jsonl", mined)
    # the legacy whole-file judge: A, B, C, G, H kept, D rejected; E never judged (the judge's cap), F judged per candidate below
    _jsonl(cdir / f"{KEY}.judged.jsonl", [{**_row(x), "judge": {"keep": x != D, "model_id": "openai/gpt-5.5"}} for x in (A, B, C, D, G, H)])
    _jsonl(cdir / f"{KEY}.judged.probed.jsonl", [])
    # cause (1)+(2): the legacy chain probed A and B through per-instance files that used to be read as source keys of their own
    for iid in (A, B):
        _jsonl(cdir / f"probe_{iid}.jsonl", [{**_row(iid), "judge": {"keep": True}}])
        _jsonl(cdir / f"probe_{iid}.probed.jsonl", [{**_row(iid), "judge": {"keep": True}, "probe": {"statuses": {"base": {"t": "failed"}, "gold": {"t": "passed"}}, "stages": ["base", "gold"]}}])
        _w(cdir / f"probe_{iid}.probed.summary.json", {"probed": 1, "reproducible": 1, "rows": 1})
    # cause (1): a repository scan listed the same seaborn PR under another source key
    _jsonl(cdir / f"{OTHER_KEY}.jsonl", [{**_row(B), "source": "github-repo", "discovery": {"kind": "bugfix"}}])
    _jsonl(cdir / f"{OTHER_KEY}.judged.jsonl", [{**_row(B), "source": "github-repo", "judge": {"keep": True}}])
    # cause (3): seaborn was rebuilt as v2; its v1 is archived. pytest-7283 is a v1 registration.
    _scenario(layout, "mwaskom--seaborn-3190", B, 2, archive_v1=True)
    _scenario(layout, "pytest-dev--pytest-7283", A, 1)
    # H: files reached approval, but the workflow's receipt says auto-approval rejected it (receipts win over file inference)
    hdir = layout.scenario_dir("scikit-learn--scikit-learn-10452")
    _w(hdir / "scenario.draft.json", {"id": "scikit-learn--scikit-learn-10452", "version": 1, "source": KEY, "source_ref": H})
    _w(hdir / "approval.json", {"scenario_id": "scikit-learn--scikit-learn-10452", "version": 1, "spec_hash": "h" * 64})
    _receipt(layout, instance_id=H, scenario_id="scikit-learn--scikit-learn-10452", phase="auto-approval", stage="approved", status="rejected",
             gate="auto-approval", reasons=["withheld brief lost the natural title line"], workflow_id="create:scikit-learn--scikit-learn-10452")
    _receipt(layout, instance_id=B, scenario_id="mwaskom--seaborn-3190", version=2, workflow_id="create:mwaskom--seaborn-3190@v2",
             candidates_path="/opt/swesim/workspace/evidence/workflows/x/0.candidates.jsonl")
    # cause (2): a frozen plan of a workflow that failed selected C (kept, never probed); F was offered but cut by the source limit;
    # a running workflow selected G
    _plan(layout, "create-1", [_row(B), _row(C), _row(F)], selected=[B, C])
    _plan(layout, "create-2", [_row(G)], selected=[G])
    _workflow(layout, "create-1", "failed")
    _workflow(layout, "create-2", "running")
    registry = tmp_path / "registry.json"
    _w(registry, {"scenarios": {
        "mwaskom--seaborn-3190": {"id": "mwaskom--seaborn-3190", "version": 2, "source": KEY, "source_ref": B, "validation_status": "valid", "spec_hash": "h" * 64},
        "pytest-dev--pytest-7283": {"id": "pytest-dev--pytest-7283", "version": 1, "source": KEY, "source_ref": A, "validation_status": "valid", "spec_hash": "h" * 64}}})
    return layout, registry


def _rows(layout: Layout, registry_path: Path) -> list[M.CandidateRow]:
    registry = P.project_registry(layout, registry_path)
    events, plans = P.candidate_events_by_identity(layout), P.candidate_plans(layout)
    return [r for key in P.source_keys(layout) for r in P.project_candidates(layout, key, registry=registry, origin="laptop", events=events, plans=plans)]


def test_per_instance_probe_files_are_joined_not_source_keys(record):
    layout, registry_path = record
    assert P.source_keys(layout) == [OTHER_KEY, KEY]  # no `probe_<instance>` key
    rows = _rows(layout, registry_path)
    assert len(rows) == 9 and len({(r.source_key, r.instance_id) for r in rows}) == 9  # exactly one row per (source_key, instance_id)
    by_id = {r.instance_id: r for r in rows if r.source_key == KEY}
    a = by_id[A]  # probed through probe_<instance>.probed.jsonl, then built and registered as v1
    assert (a.stage, a.status, a.version, a.scenario_id, a.earlier_versions) == ("registered", "kept", 1, "pytest-dev--pytest-7283", None)
    assert a.judged_at is not None


def test_every_candidate_has_one_terminal_state_with_a_reason(record):
    layout, registry_path = record
    by_id = {r.instance_id: r for r in _rows(layout, registry_path) if r.source_key == KEY}
    assert (by_id[D].stage, by_id[D].status, by_id[D].gate) == ("judged", "rejected", "judge")
    assert (by_id[E].stage, by_id[E].status, by_id[E].reasons) == ("mined", "pending", ["cap-cut"])  # mined, never judged: the judge's cap
    assert (by_id[F].stage, by_id[F].status, by_id[F].reasons) == ("mined", "pending", ["cap-cut"])  # offered by the frozen plan, cut by the limit
    assert (by_id[C].stage, by_id[C].status, by_id[C].reasons) == ("judged", "pending", ["workflow-stopped"])  # selected; its workflow failed
    assert (by_id[G].stage, by_id[G].status, by_id[G].reasons) == ("judged", "pending", ["in-progress"])  # selected; its workflow is running
    h = by_id[H]  # the receipt's terminal rejection beats the approval-stage files
    assert (h.stage, h.status, h.gate, h.workflow_id) == ("approved", "rejected", "auto-approval", "create:scikit-learn--scikit-learn-10452")
    assert not any(r.status == "kept" and r.stage != "registered" for r in by_id.values())
    assert all(r.reasons and r.reasons[0] in P.PENDING_REASONS for r in by_id.values() if r.status == "pending")


def test_a_rebuilt_scenario_keeps_its_superseded_version(record):
    layout, registry_path = record
    b = {r.instance_id: r for r in _rows(layout, registry_path) if r.source_key == KEY}[B]
    assert (b.stage, b.status, b.version, b.workflow_id) == ("registered", "kept", 2, "create:mwaskom--seaborn-3190@v2")
    assert b.candidates_path == "/opt/swesim/workspace/evidence/workflows/x/0.candidates.jsonl"
    assert b.earlier_versions == [{"version": 1, "stage": "registered", "status": "superseded", "gate": None, "reasons": []}]


def test_funnel_reconciles_stage_to_stage_and_matches_the_registry(record):
    layout, registry_path = record
    rows = _rows(layout, registry_path)
    steps = {s.stage: s for s in R.funnel(rows)}
    assert list(steps) == list(M.FUNNEL_ORDER)
    for earlier, later in zip(M.FUNNEL_ORDER, M.FUNNEL_ORDER[1:]):
        assert steps[later].entered == steps[earlier].kept, (earlier, later)
    for s in steps.values():
        assert s.entered == s.kept + s.rejected + s.error + s.pending + s.superseded, s
    registered = {sid for sid in json.loads(registry_path.read_text())["scenarios"]}
    assert steps["registered"].kept == len(registered) == 2  # not 4 (probe_ keys) nor 3 (the second source key)
    assert steps["registered"].ids == ["mwaskom--seaborn-3190@v2", "pytest-dev--pytest-7283@v1"]
    assert (steps["registered"].entered, steps["registered"].superseded) == (3, 1)
    # nine candidate versions entered mining: eight instances (seaborn once, whichever source key) plus seaborn's archived v1
    assert (steps["mined"].entered, steps["mined"].kept, steps["mined"].pending) == (9, 7, 2)
    assert (steps["judged"].entered, steps["judged"].kept, steps["judged"].rejected, steps["judged"].pending) == (7, 4, 1, 2)
    assert (steps["probed"].entered, steps["probed"].kept) == (4, 4)
    assert (steps["approved"].entered, steps["approved"].kept, steps["approved"].rejected) == (4, 3, 1)
    assert (steps["validated"].entered, steps["validated"].kept) == (3, 3)
    # the per-source funnel keeps its own row: the repository scan's seaborn row is registered there too
    other = {s.stage: s for s in R.funnel([r for r in rows if r.source_key == OTHER_KEY])}
    assert other["registered"].kept == 1 and other["mined"].entered == 2  # v2 and its superseded v1
    assert R.funnel_steps is R.funnel


def test_file_reader_funnel_and_json_shape(record, tmp_path):
    layout, registry_path = record
    settings = Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "none.env"), "SWESIM_WORKSPACE": str(layout.root), "SWESIM_REGISTRY": str(registry_path),
                                  "SWESIM_ORIGIN": "laptop"})
    reader = FileIndexReader(layout, registry_path, settings=settings)
    steps = reader.funnel()
    assert steps == R.funnel(_rows(layout, registry_path))
    dumped = steps[-1].model_dump(mode="json")
    assert set(dumped) == {"stage", "kept", "rejected", "error", "pending", "superseded", "entered", "ids"}
    assert reader.candidates(M.CandidateFilter(status=["pending"])).total == 4
    assert reader.funnel(OTHER_KEY)[-1].kept == 1


class _Vocabulary(HTMLParser):
    """The element/attribute vocabulary `app.js` `patchCharts` accepts when it swaps a live chart."""

    ALLOWED_TAGS = set("div span section h2 h3 h4 p em strong figure figcaption ul li i button table caption thead tbody tr th td a svg g polyline path rect circle line text title tspan".split())
    ALLOWED_ATTRS = set("class id role title type tabindex href style scope colspan rowspan viewbox width height x y x1 y1 x2 y2 cx cy r rx d points text-anchor transform stroke-dasharray data-view data-chart-tabs".split())

    def __init__(self) -> None:
        super().__init__()
        self.bad: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.ALLOWED_TAGS:
            self.bad.append(("tag", tag))
        for name, _value in attrs:
            if name.lower() not in self.ALLOWED_ATTRS and not name.lower().startswith("aria-"):
                self.bad.append(("attr", name))


def test_home_funnel_chart_shows_five_states_gate_meanings_and_registered_ids(record):
    layout, registry_path = record
    steps = [s.model_dump(mode="json") for s in R.funnel(_rows(layout, registry_path))]
    html = charts.funnel(steps, "chart-home-funnel")
    for kind in ("bar good", "bar critical", "bar neutral", "bar warn"):  # kept, rejected, pending, superseded segments are drawn
        assert f"class='{kind}'" in html, kind
    assert "bar serious" not in html  # no error in this record
    for key, kind, label in charts.FUNNEL_STATES:
        assert f"<i class='swatch {kind}' aria-hidden='true'></i>{label}" in html or f"swatch {kind}" in html, key
    assert html.count("</strong> · rejected here: ") == len(M.FUNNEL_ORDER) and "<th>rejected here means</th>" in html  # one line per stage, chart and table twin
    for stage in M.FUNNEL_ORDER:
        assert charts.FUNNEL_GATES[stage] in html
    assert "judged · rejected: 1 of 7 entered — rejected here means: the judge did not keep it" in html
    assert "<title>registered · superseded: 1 of 3 entered</title>" in html and "2<tspan class='lbl'> kept of 3</tspan>" in html
    assert "Registered (2): <span class='mono'>mwaskom--seaborn-3190@v2</span>, <span class='mono'>pytest-dev--pytest-7283@v1</span>." in html
    assert "<th class='num'>entered</th>" in html and "<th class='num'>superseded</th>" in html
    parser = _Vocabulary()
    parser.feed(html)
    assert parser.bad == []
    assert "Registered: none yet." in charts.funnel([{**s, "ids": []} for s in steps]) and "No candidates yet" in charts.funnel([])


def test_source_funnels_count_entered_versions(record):
    from swesim.dashboard import pages
    layout, registry_path = record
    rows = _rows(layout, registry_path)
    by_key = {r["source_key"]: r for r in pages.source_funnels({}, rows, [KEY, OTHER_KEY])}
    assert (by_key[KEY]["judged"], by_key[KEY]["probed"], by_key[KEY]["registered"]) == (7, 4, 3)  # 3 = two registered + the superseded v1
    assert (by_key[OTHER_KEY]["judged"], by_key[OTHER_KEY]["registered"]) == (2, 2)
