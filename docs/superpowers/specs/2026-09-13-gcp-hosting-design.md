# swesim hosted on GCP: design proposal (for review, not yet approved)

Status: proposal written 2026-09-13 at the user's request ("make it a properly hosted online thing using cloud Temporal and cloud everything, on GCP"). Nothing here is implemented. Decisions marked **recommend** are mine; the open questions at the end need the user.

## What runs today (measured)

One GCE VM (`swesim-worker`, e2 class, us-central1-a, project `YOUR_PROJECT`) runs three user systemd units: `swesim-temporal` (Temporal **dev server**, SQLite at `/var/lib/swesim/temporal.db`), `swesim-worker` (the Temporal worker; launches one transient systemd unit per trial), and `swesim-dashboard` (FastAPI on 127.0.0.1:8080). The workspace is a directory on the VM's boot disk. Secrets are rendered into `/etc/swesim/env` by `scripts/vm/setup.sh` from a file copied off the laptop. The console, Temporal UI and API are reachable only through `scripts/vm/tunnel.sh` (SSH port forwards). The stakeholder service runs on Modal; Harbor builds and graded runs also run on Modal. The laptop only submits workflows and opens tunnels.

What is wrong with it for a hosted product: the dev server is single-process SQLite with no backups; the console has no authentication and no public address; the only durable copy of the workspace is one boot disk; secrets come from a laptop; recovery of the VM is a manual procedure.

## Goals

1. No laptop in the loop: submit, approve, watch and read results from a browser with Google login.
2. Durable orchestration state (Temporal) and durable artifacts (scenarios, trials, exports, evidence) with backups.
3. Secrets in a managed store, never on a laptop or in an image.
4. The launch-once guarantee stays exactly as measured (one systemd unit per trial, reconcile on restart).
5. Reproducible provisioning: the environment is created from code, not from a shell history.

## Design

### 1. Temporal: Temporal Cloud (**recommend**) over self-hosting

- **Temporal Cloud**: managed service, namespace `swesim.<account>`, mTLS certificate or API key. Code change is one function: `swesim/workflows/client.py:connect` gains `TEMPORAL_TLS_CERT/KEY` or `TEMPORAL_API_KEY` handling (`Client.connect(address, namespace=..., tls=TLSConfig(...))` or `api_key=...`). The worker is unchanged. The Temporal UI comes with it, behind Temporal's own login. Cost is a base fee plus per-action pricing; at our volume (thousands of actions per day, not millions) the base fee dominates.
- **Self-hosted**: Temporal server on GKE Autopilot with Cloud SQL for PostgreSQL, plus the UI. More moving parts, more to patch, and no one on call for it. Only worth it if the Temporal Cloud account is a blocker.

### 2. Workers stay on GCE VMs

The launch-once design depends on a real host with systemd (`systemd-run` units, `reconcile` by unit state). Cloud Run and GKE pods do not give that without a redesign, so the worker stays on a VM. Two changes:

- The VM is created from an **instance template** whose startup script runs `scripts/vm/setup.sh` (pull `main`, `uv sync`, render env from Secret Manager, enable units). A managed instance group of size 1 with autohealing gives automatic recreation; the workspace lives on a **separate persistent disk** so a recreated VM reattaches it.
- **Scaling out** later requires one code change: trial launch and poll activities must run on the host that owns the unit. The plan is a per-host task queue for `launch`/`await` (`swesim-launch-<hostname>`) with the `TrialWorkflow` choosing the queue when it launches; creation activities (judge, draft, compile, validate) stay on the shared queue. Not needed for one VM.

### 3. Console: same app, behind an HTTPS load balancer with Identity-Aware Proxy (**recommend** first step)

The console reads workspace files directly, so it should run where the files are. Put an external HTTPS load balancer in front of the VM's port 8080 with **IAP** (Google login, an allowlist of emails). No code change except trusting the IAP identity header for the evaluator gate (`?evaluator=1` becomes "IAP identity is in the evaluator list"). A domain (`swesim.<yourdomain>`) and a Google-managed certificate complete it. Cloud Run for the console is a later option that needs a storage abstraction (see 4).

### 4. Persistence: files stay the record, GCS is the durable copy

- Temporal is the source of truth for workflow state; the workspace files (`scenario.json`, `record.json`, exports, evidence) are the source of truth for results. Do not introduce a database as a third source of truth now.
- The workspace moves to a dedicated persistent disk with a daily snapshot schedule.
- A systemd timer (or the last activity of each workflow) runs `gcloud storage rsync` of `workspace/{scenarios,trials,exports,evidence,candidates}` and `registry.json` to a versioned GCS bucket. Restoring a VM is: attach disk or rsync back from the bucket.
- If the console needs faster listing than globbing thousands of trial directories, add a SQLite index rebuilt from the files (derived data, disposable). Cloud SQL is not justified at this scale.

### 5. Secrets: Secret Manager

`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `SWESIM_WORKER_SECRET`, `GH_TOKEN`, the Modal token, and the Temporal Cloud credential live in Secret Manager. The VM's service account has `secretAccessor` on exactly those; the startup script renders `/etc/swesim/env` (mode 0600) from them. `scripts/vm/setup.sh` loses its "copy the laptop's env file" step. Rotation is: update the secret, run `update.sh`.

### 6. Stakeholder service and Modal: unchanged

The stakeholder stays on Modal (already hosted, per-session Volume persistence). Its single-container assumption is documented; a Cloud Run port is possible later but not needed. Harbor builds and graded runs stay on Modal.

### 7. Provisioning and deploy

- `infra/` with Terraform: VPC and firewall (no public SSH; SSH only via IAP TCP forwarding), service accounts, Secret Manager secrets (values set out of band), instance template and MIG, persistent disk and snapshot schedule, GCS bucket, HTTPS LB with IAP, DNS record.
- Deploy: GitHub Actions on push to `main` runs the offline suite; a manual "deploy" job runs `scripts/vm/update.sh` through IAP SSH (fast-forward, `uv sync`, restart worker and console). The worker restart is safe by design (launch-once, reconcile).

### 8. Observability

Ops Agent ships journald to Cloud Logging; an uptime check on the console; alerting on `swesim-worker` unit failure and on any `TrialWorkflow` that reports `reconcile` more than once; Temporal Cloud's own metrics for workflow failures.

## Rough monthly cost

| Item | Estimate |
|---|---|
| Temporal Cloud (base) | ~$100–200 |
| GCE worker VM (e2-standard-4) plus disk and snapshots | ~$110 |
| HTTPS load balancer with IAP | ~$20 |
| GCS bucket | under $5 |
| Modal (stakeholder, builds, runs) | usage-based, unchanged |

## Migration order (each step has an acceptance check)

1. Temporal Cloud namespace; client credential handling; worker reconnects; one `RunTasksWorkflow` completes end to end. Check: the workflow appears in Temporal Cloud's UI and the trial record is written.
2. Secret Manager and the startup script; recreate the VM from the template with the workspace disk attached. Check: `/etc/swesim/env` rendered without any laptop file; units active; `verify.sh 10` passes on the VM.
3. HTTPS load balancer with IAP in front of the console; evaluator gate bound to IAP identity. Check: a browser login from a non-allowlisted account is refused; the allowlisted one sees the console.
4. GCS mirroring timer and snapshot schedule. Check: delete a trial directory on a scratch copy and restore it from the bucket.
5. Terraform for everything above; destroy and recreate in a scratch project. Check: the console and a smoke workflow work on the recreated stack.
6. CI deploy job. Check: a docs-only push deploys without a human on the VM.

## Open questions for the user

1. Temporal Cloud account and budget, or self-host?
2. Who gets console access (email allowlist), and is there a domain to use?
3. One VM with autohealing now, with per-host queues deferred, or build the multi-worker path immediately?
4. Should the OpenRouter weak-model sweep run from the same worker (rate limits per provider) or a second one?
