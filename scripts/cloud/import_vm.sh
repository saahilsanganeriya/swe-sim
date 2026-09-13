#!/bin/bash
# Import the laptop mirror of the VM record into the workspace bucket, then rebuild the index with rows labelled
# `origin: vm-systemd` (interfaces §2; brief §2.3 "Import").
#
# What goes up (from Settings.workspace, i.e. SWESIM_WORKSPACE): scenarios, tasks, exports, candidates, evidence, probes,
# calibration, costs; trials without trials/<id>/task/ (the task copy carries the trial's session token); jobs without
# config.json (absolute laptop paths), any task.toml (tokens) and *.lock files; then registry.json (Settings.registry_path)
# and dataset.json. Nothing in the bucket is deleted (`gcloud storage rsync` without --delete-unmatched-destination-objects),
# so a second run copies only what changed: idempotent.
#
#   SWESIM_ENV_FILE=<env file> bash scripts/cloud/import_vm.sh [bucket]
#     bucket: argument, SWESIM_BUCKET (env or env file, via swesim.settings), or infra/outputs.json
#   PYTHON overrides the interpreter used for settings and the rebuild (default: `uv run --frozen --offline python`).
set -euo pipefail
cd "$(dirname "$0")/../.."

PYTHON=${PYTHON:-uv run --frozen --offline python}
# Non-secret settings only (paths and the bucket name); the database URL stays inside the rebuild process.
# (No `mapfile`: macOS ships bash 3.2.)
CFG=$($PYTHON -c 'from swesim.settings import get_settings as g
s = g(); print(s.workspace.resolve()); print(s.registry_path.resolve()); print(s.bucket or "")')
WORKSPACE=$(printf '%s\n' "$CFG" | sed -n 1p)
REGISTRY=$(printf '%s\n' "$CFG" | sed -n 2p)
BUCKET=${1:-$(printf '%s\n' "$CFG" | sed -n 3p)}
if [ -z "$BUCKET" ] && [ -f infra/outputs.json ]; then
  BUCKET=$(python3 -c 'import json; print(json.load(open("infra/outputs.json"))["bucket"]["value"])')
fi
: "${BUCKET:?bucket name required (argument, SWESIM_BUCKET, or infra/outputs.json)}"
[ -d "$WORKSPACE" ] || { echo "import_vm: workspace $WORKSPACE does not exist" >&2; exit 2; }
GC=(gcloud --quiet)

# Refuse to upload a trial task copy that still holds a session token even if the exclude pattern were wrong.
leaked=$(grep -rlE --include=task.toml 'SWESIM_SESSION_TOKEN *= *"[^"]+"' "$WORKSPACE/trials" 2>/dev/null || true)
if [ -n "$leaked" ]; then
  echo "import_vm: trials/*/task/task.toml files carry session tokens; they are excluded from the upload:" >&2
  printf '  %s\n' "$leaked" >&2
fi

for prefix in scenarios tasks exports candidates evidence probes calibration costs; do
  if [ -d "$WORKSPACE/$prefix" ]; then
    "${GC[@]}" storage rsync -r "$WORKSPACE/$prefix" "gs://$BUCKET/$prefix"
  fi
done
if [ -d "$WORKSPACE/trials" ]; then
  "${GC[@]}" storage rsync -r --exclude='^[^/]+/task/.*|.*\.lock$' "$WORKSPACE/trials" "gs://$BUCKET/trials"
fi
if [ -d "$WORKSPACE/jobs" ]; then
  "${GC[@]}" storage rsync -r --exclude='.*/config\.json$|.*/task\.toml$|.*\.lock$' "$WORKSPACE/jobs" "gs://$BUCKET/jobs"
fi
[ -f "$REGISTRY" ] && "${GC[@]}" storage cp "$REGISTRY" "gs://$BUCKET/registry.json"
[ -f "$WORKSPACE/dataset.json" ] && "${GC[@]}" storage cp "$WORKSPACE/dataset.json" "gs://$BUCKET/dataset.json"

if [ "$($PYTHON -c 'from swesim.settings import get_settings; print(int(get_settings().index_enabled))')" = 1 ]; then
  $PYTHON -m swesim.index.cli migrate
  $PYTHON -m swesim.index.cli rebuild --origin vm-systemd
else
  # The production database is reachable through the Cloud Run SQL socket. A laptop needs no database credential.
  cloud_cfg=$($PYTHON -c 'import json
from pathlib import Path
d = json.loads(Path("infra/outputs.json").read_text())
for key in ("project", "region", "index_maintenance_job"): print(d[key]["value"])')
  project=$(printf '%s\n' "$cloud_cfg" | sed -n 1p)
  region=$(printf '%s\n' "$cloud_cfg" | sed -n 2p)
  job=$(printf '%s\n' "$cloud_cfg" | sed -n 3p)
  "${GC[@]}" run jobs execute "$job" --project "$project" --region "$region" --args=migrate --wait
  "${GC[@]}" run jobs execute "$job" --project "$project" --region "$region" --args=rebuild,--origin=vm-systemd --wait
fi
echo "import_vm: $WORKSPACE -> gs://$BUCKET; index rebuilt with origin vm-systemd"
