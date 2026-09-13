"""FileIndexReader gives the console the same answers as IndexStore after a rebuild from the same files, and
`get_reader` picks one or the other from Settings."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from swesim.index import models as M
from swesim.index.files import FileIndexReader
from swesim.index.reads import IndexReader, get_reader
from swesim.index.store import IndexStore
from swesim.settings import Settings

try:
    from test_index_project import SID, Record, build_record
except ImportError:  # pragma: no cover
    from tests.test_index_project import SID, Record, build_record

STAMPS = {"created_at", "updated_at"}


@pytest.fixture(scope="module")
def record(tmp_path_factory) -> Record:
    return build_record(tmp_path_factory.mktemp("record"))


@pytest.fixture
def settings(tmp_path, record: Record) -> Settings:
    return Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "none.env"), "SWESIM_SPEND_CAP_USD": "100", "SWESIM_ORIGIN": "laptop",
                              "SWESIM_WORKSPACE": str(record.layout.root), "SWESIM_REGISTRY": str(record.registry_path)})


@pytest.fixture
def readers(settings: Settings, record: Record) -> tuple[FileIndexReader, IndexStore]:
    store = IndexStore("sqlite://", settings=settings)
    store.migrate()
    store.rebuild_from_layout(record.layout, record.registry_path)
    return FileIndexReader(record.layout, record.registry_path, settings=settings), store


def _strip(rows):
    return [r.model_dump(exclude=STAMPS) for r in rows]


def test_file_reader_matches_the_sql_reader(readers, record: Record):
    files, sql = readers
    assert isinstance(files, IndexReader)
    f = M.TrialFilter(order_by="trial_id", desc=False)
    assert _strip(files.trials(f).items) == _strip(sql.trials(f).items) and files.trials(f).total == sql.trials(f).total
    f2 = M.TrialFilter(status=["completed"], limit=2, offset=1, order_by="trial_id", desc=False)
    assert _strip(files.trials(f2).items) == _strip(sql.trials(f2).items)
    assert _strip([files.trial(record.tids["done"])]) == _strip([sql.trial(record.tids["done"])]) and files.trial("0" * 24) is None
    sf = M.ScenarioFilter(order_by="scenario_id", desc=False)
    assert _strip(files.scenarios(sf).items) == _strip(sql.scenarios(sf).items)
    assert _strip(files.scenarios(M.ScenarioFilter(latest_only=True, order_by="scenario_id", desc=False)).items) == \
        _strip(sql.scenarios(M.ScenarioFilter(latest_only=True, order_by="scenario_id", desc=False)).items)
    assert _strip(files.scenario_versions(SID)) == _strip(sql.scenario_versions(SID)) and _strip([files.scenario(SID, 1)]) == _strip([sql.scenario(SID, 1)])
    cf = M.CandidateFilter(order_by="instance_id", desc=False)
    assert _strip(files.candidates(cf).items) == _strip(sql.candidates(cf).items) and files.funnel() == sql.funnel()
    assert files.funnel("swebench-annotated") == sql.funnel("swebench-annotated")
    assert _strip(files.scores(M.ScoreFilter())) == _strip(sql.scores(M.ScoreFilter()))
    for evaluator in (True, False):
        assert _strip(files.exchanges(record.tids["done"], evaluator)) == _strip(sql.exchanges(record.tids["done"], evaluator))
    for lf in (M.LeaderboardFilter(purpose="calibration"), M.LeaderboardFilter(purpose="calibration", include_saturated=True), M.LeaderboardFilter()):
        assert files.leaderboard(lf) == sql.leaderboard(lf)
    assert files.spend() == sql.spend() and _strip(files.costs(M.CostFilter())) == _strip(sql.costs(M.CostFilter()))
    tf = M.TableFilter(where={"status": "completed"}, order_by="trial_id", desc=False)
    assert [r["trial_id"] for r in files.table("trials", tf).items] == [r["trial_id"] for r in sql.table("trials", tf).items]
    assert files.table("scores", M.TableFilter()).total == sql.table("scores", M.TableFilter()).total == 3
    with pytest.raises(ValueError):
        files.table("nope", M.TableFilter())
    with pytest.raises(ValueError):
        files.trials(M.TrialFilter(order_by="bogus"))


def test_file_reader_reads_jobs_actions_and_rebuild_history(readers, record: Record):
    files, sql = readers
    page = files.workflows(M.WorkflowFilter())
    assert page.total == 1 and page.items[0].workflow_id == record.tids["done"] and files.workflow(record.tids["done"]).status == "completed"
    assert files.workflows(M.WorkflowFilter(status=["failed"])).total == 0 and files.workflow("nope") is None
    actions = files.operator_actions(M.PageFilter())
    assert actions.total == 1 and actions.items[0].action == "run-tasks" and actions.items[0].at == datetime(2026, 9, 13, 12, tzinfo=UTC)
    # the SQL rebuild published its own snapshot into the record: the file reader sees the same rebuild history
    assert [r.rebuild_id for r in files.rebuilds()] == [r.rebuild_id for r in sql.rebuilds()] and files.rebuilds()
    assert files.table("index_rebuilds", M.TableFilter()).total == len(sql.rebuilds())
    assert files.table("workflows", M.TableFilter()).items[0]["workflow_id"] == record.tids["done"]


def test_get_reader_follows_settings(settings: Settings, tmp_path):
    reader = get_reader(settings)
    assert isinstance(reader, FileIndexReader) and reader.layout.root == settings.workspace and reader.registry_path == settings.registry_path
    enabled = Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "none.env"), "SWESIM_DATABASE_URL": f"sqlite:///{tmp_path / 'i.db'}"})
    assert isinstance(get_reader(enabled), IndexStore)
