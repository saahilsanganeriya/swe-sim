#!/bin/bash
# Mirror the VM's record into the laptop workspace (laptop side): workspace/{scenarios,tasks,trials,exports,candidates,evidence},
# registry.json, and the small essentials of each Harbor job directory (runs: agent/trajectory.json, result.json, verifier files
# under 4 MB; validate: result/reward/report json). Merge only: nothing on the laptop is deleted. Never copied: workspace/trials/*/task/ (task.toml carries the
# trial's session token), Harbor config.json of run jobs (the materialized task environment), env files, the Temporal database.
#
# GCP_PROJECT / GCP_ZONE come from the environment or from SWESIM_ENV_FILE (default ./.env) via a line parser.
# Usage: bash scripts/vm/pull.sh            # prints record counts before/after; the tarball is removed afterwards
set -euo pipefail
cd "$(dirname "$0")/../.."

ENV_FILE=${SWESIM_ENV_FILE:-.env}
VM_NAME=${VM_NAME:-swesim-worker}
DEST=${SWESIM_WORKSPACE_ROOT:-.}     # repo root: workspace/ and registry.json live here

env_get() {
  local file=$1 key=$2 line
  line=$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$file" 2>/dev/null | head -n1) || true
  [ -n "$line" ] || return 1
  line=${line#*=}; line=${line%$'\r'}
  case $line in
    \"*\") line=${line#\"}; line=${line%\"} ;;
    \'*\') line=${line#\'}; line=${line%\'} ;;
  esac
  printf '%s' "$line"
}

GCP_PROJECT=${GCP_PROJECT:-$(env_get "$ENV_FILE" GCP_PROJECT || true)}
GCP_ZONE=${GCP_ZONE:-$(env_get "$ENV_FILE" GCP_ZONE || true)}
: "${GCP_PROJECT:?GCP_PROJECT missing (environment or $ENV_FILE)}" "${GCP_ZONE:?GCP_ZONE missing (environment or $ENV_FILE)}"
GC=(gcloud --quiet)
SSH=("${GC[@]}" compute ssh "$VM_NAME" --project "$GCP_PROJECT" --zone "$GCP_ZONE" --ssh-flag="-o ConnectTimeout=25")
SCP=("${GC[@]}" compute scp --project "$GCP_PROJECT" --zone "$GCP_ZONE")

before=$(ls "$DEST"/workspace/trials/*/record.json 2>/dev/null | wc -l | tr -d ' ')
read -r -d '' REMOTE <<'REMOTE_EOF' || true
set -euo pipefail
cd /opt/swesim
list=$(mktemp)
{
  find workspace/scenarios workspace/tasks workspace/exports workspace/candidates workspace/evidence -type f
  find workspace/trials -type f -not -path 'workspace/trials/*/task/*'
  find workspace/jobs/runs -maxdepth 4 -type f \( -name trajectory.json -o -name result.json -o -path '*/verifier/*' \) -size -4M
  find workspace/jobs/validate -type f \( -name result.json -o -name config.json -o -name reward.json -o -name report.json \) -size -4M   # validation tasks carry no session token
  [ -f registry.json ] && echo registry.json
} | sort -u > "$list"
tar czf /tmp/swesim-pull.tgz --files-from="$list"
echo "files: $(wc -l < "$list")  size: $(du -h /tmp/swesim-pull.tgz | cut -f1)  records: $(ls workspace/trials/*/record.json | wc -l)"
rm -f "$list"
REMOTE_EOF
"${SSH[@]}" --command "$REMOTE"
tmp=$(mktemp -d)
"${SCP[@]}" "$VM_NAME":/tmp/swesim-pull.tgz "$tmp/pull.tgz"
"${SSH[@]}" --command "rm -f /tmp/swesim-pull.tgz"
if grep -q 'SWESIM_SESSION_TOKEN=' <(tar tzf "$tmp/pull.tgz"); then echo "refusing: tarball lists a task.toml" >&2; exit 1; fi
tar xzf "$tmp/pull.tgz" -C "$DEST"
rm -rf "$tmp"
after=$(ls "$DEST"/workspace/trials/*/record.json 2>/dev/null | wc -l | tr -d ' ')
echo "laptop trial records: $before -> $after; registry.json now from the VM ($(python3 -c "import json;print(len(json.load(open('$DEST/registry.json'))['scenarios']))") scenarios)"
