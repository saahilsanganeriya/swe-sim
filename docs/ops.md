# swesim operations: VM, Temporal, laptop-off runs

Status legend: **[measured]** values come from inspected artifacts; **[placeholder]** is filled in by the
integrator during Phase 3. Nothing below claims a cloud result until the placeholder is replaced.

## Topology

| Component | Where | How it runs | Reach it |
|---|---|---|---|
| Temporal dev server | GCE VM `swesim-worker` (e2-standard-4, Debian 12, 100 GB pd-balanced) | user unit `swesim-temporal`: `temporal server start-dev --db-filename /var/lib/swesim/temporal.db --ip 127.0.0.1 --port 7233 --ui-port 8233` | SSH tunnel: UI http://localhost:8233, gRPC localhost:7233 |
| Temporal worker | same VM | user unit `swesim-worker`: `/opt/swesim/.venv/bin/python -m swesim.workflows.worker`, task queue `swesim`, 4 concurrent light activities, one trial launch at a time; `EnvironmentFile=/etc/swesim/env` (mode 0600) | `journalctl --user -u swesim-worker` |
| Trial units | same VM | one transient user unit per trial, `swesim-trial-<trial_id>`, created once by the worker with `systemd-run --user --property=RemainAfterExit=yes`; it runs `python -m swesim.workflows.activities run-trial workspace/trials/<trial_id>/request.json`, which calls `swesim.stages.run.run_trial` (Harbor on Modal). The worker only polls it. | `systemctl --user list-units 'swesim-trial-*' --all`, `journalctl --user -u swesim-trial-<trial_id>` |
| Dashboard | same VM | user unit `swesim-dashboard`: `uvicorn swesim.dashboard.app:app --host 127.0.0.1 --port 8080` | SSH tunnel: http://localhost:8080 |
| Agent sandboxes, image builds, separate verifiers | Modal, through Harbor 0.23 (`harbor run -e modal`) | launched by the trial unit; no Docker on the VM | Harbor job dirs under `/opt/swesim/workspace/jobs` |
| Stakeholder service | Modal web endpoint | deployed from the laptop (`scripts/deploy_stakeholder.sh`); URL in the env file as `SWESIM_STAKEHOLDER_URL` | |
| Laptop | thin client | `scripts/vm/create.sh`, `scripts/vm/tunnel.sh`, `swesim pipeline|approve-signal|matrix` through the tunnel, browsers | |

Files on the VM: `/opt/swesim` (git checkout at the deployed commit), `/opt/swesim/workspace/{candidates,scenarios,tasks,trials,jobs,exports,evidence}`, `/opt/swesim/registry.json`, `/var/lib/swesim/temporal.db`, `/etc/swesim/env` (dotenv, 0600, copied from the laptop; never from the repo), `~/.modal.toml` (0600). All service ports bind to 127.0.0.1; nothing is exposed on the VM's public address.

Why per-trial units: `systemd-run` places the Harbor process in its own unit under the user manager (lingering enabled), so `systemctl --user restart swesim-worker` cannot kill it. The worker records `launch.json` (intent, then acknowledgement) before and after the single `systemd-run`; every later poll reads that file and the unit state and never launches again (`swesim/workflows/activities.py: decide_launch`).

## Deploy (VM)

Prerequisites on the laptop:

1. `gcloud` authenticated for the project. The dotenv file (`SWESIM_ENV_FILE`, default `~/Documents/.env`) contains
   `GCP_PROJECT`, `GCP_ZONE`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `SWESIM_WORKER_SECRET`, `SWESIM_STAKEHOLDER_URL`
   and a **read-only** `GH_TOKEN` (from `gh auth token`; written into the ignored env file, never into tracked files).
   `~/.modal.toml` holds the Modal token. Both files are mode 0600.
2. The commit to deploy is pushed to `origin` (the VM clones it over HTTPS with `GH_TOKEN`).

Commands:

```bash
bash scripts/vm/create.sh            # creates e2-standard-4 Debian 12 VM (or reuses it), copies env + modal.toml, runs setup.sh on the VM
bash scripts/vm/tunnel.sh            # second terminal; forwards 8233, 7233, 8080 to localhost
```

`setup.sh` (runs on the VM as the login user): apt `git gh curl ca-certificates file`; `uv` and `uv python install 3.12`;
Temporal CLI v1.8.3 pinned (archive inspected with `file` and `tar -t` before extraction, installed to `/usr/local/bin/temporal`);
directories `/opt/swesim`, `/var/lib/swesim`, `/etc/swesim` owned by the service user; `/etc/swesim/env` and `~/.modal.toml`
installed mode 0600 from the copied files; `gh repo clone` + `git checkout --detach <commit>` at `/opt/swesim`;
`uv sync --extra dev --frozen --python 3.12`; `loginctl enable-linger`; units installed to `~/.config/systemd/user/`,
verified with `systemd-analyze --user verify`, then `systemctl --user enable --now swesim-temporal swesim-worker swesim-dashboard`.

Update to a new commit (re-runnable):

```bash
gcloud compute ssh swesim-worker --project "$GCP_PROJECT" --zone "$GCP_ZONE" -- \
  bash /opt/swesim/scripts/vm/setup.sh https://github.com/<owner>/swesim.git <commit>
```

Health checks on the VM:

```bash
systemctl --user status swesim-temporal swesim-worker swesim-dashboard --no-pager
journalctl --user -u swesim-worker -n 50 --no-pager          # expect: "swesim worker: temporal=127.0.0.1:7233 ... launcher=systemd"
temporal --address 127.0.0.1:7233 workflow list
curl -s http://127.0.0.1:8080/ | head -c 200
```

Deployment record **[placeholder]**: VM name, zone, machine type, image, deployed commit, `setup.sh` completion time,
`systemd-analyze --user verify` output, `systemctl --user is-active` output.

## Laptop-off acceptance test (procedure + evidence placeholder)

Goal (M8): the pipeline progresses with the laptop closed, and a worker restart leaves exactly one launch and one terminal
trial record. Evidence file: `workspace/evidence/ops.json` (schema below; checked by `scripts/verify.sh 8`).

Start the pipeline **on the VM** (the CLI loads `/etc/swesim/env` through python-dotenv; `SWESIM_STAKEHOLDER_URL` comes from it):

```bash
gcloud compute ssh swesim-worker --project "$GCP_PROJECT" --zone "$GCP_ZONE"
cd /opt/swesim
export SWESIM_ENV_FILE=/etc/swesim/env          # a path, not a secret
uv run --frozen --offline swesim pipeline --source github-repo --repo https://github.com/py-pdf/pypdf \
  --judge-model "$JUDGE_MODEL" --draft-model "$DRAFT_MODEL" --model "$SOLVER_MODEL"
# prints the workflow id, e.g. pipeline-3f2a...; keep it:
WORKFLOW_ID=pipeline-...
```

Approval is unattended by default (`approval_mode=auto`): `auto_approve_activity` runs the code-owned gates and the
pipeline continues; `approval.json` records `approved_by: auto-validators, review_needed: true`. On a withholding leak the
workflow re-drafts once, then moves to the next judged candidate (bounded by `max_candidates_tried`, default 3).

Supervised mode (`--approval-mode signal`, not used overnight): the workflow parks at stage `awaiting-approval` for up to
24 h. Find the pending scenario id from the `progress` query, approve on the VM, then send the signal with the frozen hash:

```bash
uv run --frozen --offline python -c 'import asyncio,sys; from swesim.workflows.client import progress; print(asyncio.run(progress(sys.argv[1])))' "$WORKFLOW_ID"
SCENARIO_ID=...
uv run --frozen --offline swesim approve "$SCENARIO_ID"
SWESIM_HASH=$(uv run --frozen --offline python -c 'import json,sys; print(json.load(open("workspace/scenarios/"+sys.argv[1]+"/approval.json"))["spec_hash"])' "$SCENARIO_ID")
uv run --frozen --offline swesim approve-signal "$WORKFLOW_ID" "$SCENARIO_ID" "$SWESIM_HASH"
```

Close the laptop for at least 20 minutes. Reconnect, re-open the tunnel, and collect evidence on the VM:

```bash
cd /opt/swesim
temporal --address 127.0.0.1:7233 workflow show --workflow-id "$WORKFLOW_ID" --output json > workspace/evidence/pipeline-history.json
temporal --address 127.0.0.1:7233 workflow describe --workflow-id "$WORKFLOW_ID"
ls workspace/trials/*/record.json workspace/exports/*.episode.json
```

The history must show activity completions with timestamps inside the laptop-off window (`mine_activity` ... `validate_activity`
... child `TrialWorkflow` ... `export_activity`). A trial's `record.json` has `status: completed`; its export is under
`workspace/exports/<trial_id>.episode.json`.

`ops.json` shape (values come from the inspected artifacts; write it with the snippet below after both tests):

```json
{"provider": "gce",
 "laptop_off": {"passed": true, "workflow_id": "pipeline-...", "history_path": "workspace/evidence/pipeline-history.json",
                "record_path": "workspace/trials/<trial_id>/record.json", "export_path": "workspace/exports/<trial_id>.episode.json"},
 "worker_restart": {"passed": true, "trial_id": "<trial_id>", "unit": "swesim-trial-<trial_id>",
                    "journal_path": "workspace/evidence/swesim-trial-<trial_id>.journal.txt", "launch_count": 1,
                    "record_path": "workspace/trials/<trial_id>/record.json"}}
```

```bash
uv run --frozen --offline python - "$WORKFLOW_ID" "$TRIAL_ID" <<'PY'
import json, sys
from pathlib import Path
wid, tid = sys.argv[1], sys.argv[2]
record = Path(f"workspace/trials/{tid}/record.json"); export = Path(f"workspace/exports/{tid}.episode.json")
journal = Path(f"workspace/evidence/swesim-trial-{tid}.journal.txt"); launch = json.loads(Path(f"workspace/trials/{tid}/launch.json").read_text())
assert json.loads(record.read_text())["status"] == "completed" and export.is_file() and journal.stat().st_size > 0
assert launch["phase"] == "acknowledged" and launch["returncode"] == 0
launch_count = journal.read_text().count("Started swesim trial")
ops = {"provider": "gce",
       "laptop_off": {"passed": True, "workflow_id": wid, "history_path": "workspace/evidence/pipeline-history.json", "record_path": str(record), "export_path": str(export)},
       "worker_restart": {"passed": launch_count == 1, "trial_id": tid, "unit": f"swesim-trial-{tid}", "journal_path": str(journal), "launch_count": launch_count, "record_path": str(record)}}
Path("workspace/evidence/ops.json").write_text(json.dumps(ops, indent=2)); print(json.dumps(ops, indent=2))
PY
```

Evidence **[placeholder]**:

| item | value |
|---|---|
| workflow id | |
| laptop closed from / to (local time) | |
| first and last activity completion inside the window (from history) | |
| trial id, record status, reward | |
| export path | |
| `ops.json` written at | |

## Worker restart test

Run while a trial unit is active (the solver is running on Modal). Restart **only** the worker; never the trial unit.

```bash
systemctl --user list-units 'swesim-trial-*' --all --no-pager      # note the unit and its MainPID
systemctl --user show swesim-trial-$TRIAL_ID -p MainPID -p ActiveState -p SubState
systemctl --user restart swesim-worker
sleep 5
systemctl --user show swesim-trial-$TRIAL_ID -p MainPID -p ActiveState -p SubState   # same MainPID, still active/running
journalctl --user -u swesim-worker -n 30 --no-pager                  # worker reconnected; await_trial_activity resumed (heartbeat timeout -> retry, read-only)
```

After the trial finishes:

```bash
UNIT=swesim-trial-$TRIAL_ID
journalctl --user -u "$UNIT" --no-pager > workspace/evidence/$UNIT.journal.txt
grep -c 'Started swesim trial' workspace/evidence/$UNIT.journal.txt                    # must be 1
cat workspace/trials/$TRIAL_ID/launch.json                                             # phase acknowledged, returncode 0, one file
temporal --address 127.0.0.1:7233 workflow show --workflow-id "$TRIAL_ID" --output json \
  | python3 -c 'import json,sys; ev=json.load(sys.stdin)["events"]; print(sum(1 for e in ev if e.get("activityTaskScheduledEventAttributes",{}).get("activityType",{}).get("name")=="launch_trial_activity"))'   # must be 1
ls workspace/trials/$TRIAL_ID/{record,reward,report,transcript,status}.json
```

Expected: one `launch_trial_activity` scheduled in the trial workflow's history, one `Started` line in the unit journal,
one `record.json`. A missing result after the restart is a failure and is recorded here as such; the same solver is never
restarted to manufacture recovery evidence. If the unit is gone without a record (VM reboot), the attempt is reported as
`unresolved` by `await_trial_activity` and stays unresolved; see "Manual retry lineage".

Evidence **[placeholder]**: trial id, unit, MainPID before/after, worker restart timestamp, journal path, launch count,
record status.

## Model probe and fallbacks

`PipelineWorkflow` first runs `probe_models_activity`: one tiny `chat_json` request per configured model id (judge, draft,
solver list, optional stakeholder). Results and substitutions are written to `workspace/evidence/model-probe.json`.

Rules (CLAUDE.md, unattended-run rules): if the judge or draft model is unavailable, substitute
`anthropic/claude-sonnet-5` (the stakeholder model likewise); the solver is **never** substituted silently. A solver that
fails the probe is listed under `needs_human` in the probe evidence and in the `progress` query, and this section records it.
The judge stage records the model actually used in `difficulty.judged.model_id`.

Recorded probes and substitutions **[placeholder]**:

| date/time | role | configured id | result | substitution | evidence |
|---|---|---|---|---|---|
| | | | | | `workspace/evidence/model-probe.json` |

## Known failures and residuals

Design residuals (present before any cloud run):

- `--launcher subprocess` (`SWESIM_TRIAL_LAUNCHER=subprocess`) replaces `systemd-run` with a detached child process for
  macOS/local smoke tests. It is **not production**: unit-state evidence is a pid liveness check, lost across worker restarts,
  and pid reuse is possible. The VM unit forces `SWESIM_TRIAL_LAUNCHER=systemd`.
- Exactly-once launch rests on `launch.json` written before `systemd-run` plus the unit's persistence under
  `RemainAfterExit=yes`. A worker crash between the intent write and `systemd-run` leaves an `unresolved` attempt that a person
  reconciles (nothing ran, but the code cannot prove it). This is deliberate: unknown launch state is never retried blindly.
- A `TrialWorkflow` id is used once (`REJECT_DUPLICATE`, also for closed workflows). A launch that failed with nothing running
  still consumes that attempt index; the retry is a new attempt index (see below), so Temporal history keeps the lineage.
- `systemd-run` requires the worker to run inside the user's systemd manager (the user unit does; lingering keeps the manager
  alive). Running the worker from a plain SSH shell without `XDG_RUNTIME_DIR` fails at launch with `launch-failed`, recorded in
  `launch.json`.
- `temporalio`'s time-skipping test server is downloaded on first use by `tests/test_workflows.py`; offline, those tests fall
  back to the `temporal` CLI on PATH (`start_local`) and otherwise skip with an explicit reason (the pure decision tests still run).
- `gh repo clone` over HTTPS with `GH_TOKEN` and `gh auth setup-git` are the token path for the VM checkout; this is
  verified only when `create.sh` first runs.

Measured failures **[placeholder]** (each entry: date/time, stage, artifact path, what was observed, resolution or "unresolved"):

- 

## Manual retry lineage

Trial identity is `trial_id(spec_hash, condition, run_config_hash, attempt_index)`; it is also the Temporal workflow id and
the systemd unit suffix (`swesim-trial-<trial_id>`). Rules:

1. Never delete or edit `record.json`, `reward.json`, `report.json`, `transcript.json`, `launch.json` or `request.json`
   under `workspace/trials/<trial_id>/`. Copy anything you inspect into `workspace/evidence/`.
2. A retry is a **new attempt index**, never a Temporal retry and never a relaunch of the same unit:
   `uv run swesim matrix <scenario_id> --models <model> --conditions <cond> --k 1 --attempt-offset <next_index>`
   (the integrator wires `--attempt-offset`; it maps to `plan_trials(p)["attempt_offset"]`). Record here: the failed trial id,
   its terminal status (`launch-failed`, `unit-failed`, `runner-error`, `unresolved`), the evidence path, and the new trial id.
3. Statuses and what to inspect before deciding:
   - `launch-failed`: `launch.json` has `returncode != 0` or `phase: failed`; `systemctl --user show swesim-trial-<id>` says
     `LoadState=not-found`; `workspace/jobs/runs/<trial_id>` is absent. Nothing ran. Retry with a new attempt index.
   - `unit-failed`: the unit exists with `ActiveState=failed` and no `record.json` / `runner-error.json`; read
     `journalctl --user -u swesim-trial-<id>`. Check `workspace/jobs/runs/<trial_id>` for a Harbor job before any new attempt.
   - `runner-error`: `runner-error.json` explains why `run_trial` refused (frozen spec changed, identity mismatch, missing
     secret). Fix the cause; the attempt index is spent.
   - `unresolved`: launch evidence exists but the unit is gone (`LoadState=not-found`) without a record, or the await deadline
     passed. Check `journalctl --user -u swesim-trial-<id>`, the Harbor job dir and Modal's dashboard for a still-running
     sandbox. Only after confirming no job exists may a new attempt index be started.
4. `systemctl --user reset-failed swesim-trial-<id>` and `systemctl --user stop swesim-trial-<id>` only after the evidence
   above is saved; they remove the unit-state evidence.
5. Calibration and comparison runs are never pooled; a retried attempt keeps the original `purpose` in its `RunConfig`.

Lineage log **[placeholder]**:

| failed trial id | status | evidence | new trial id (attempt index) | note |
|---|---|---|---|---|
| | | | | |
