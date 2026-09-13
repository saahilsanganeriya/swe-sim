#!/bin/bash
# Forward the VM's loopback services to the laptop over gcloud compute ssh:
#   8233 Temporal UI, 7233 Temporal gRPC (for `swesim pipeline|approve-signal|matrix` and `temporal` CLI), 8080 dashboard.
# GCP_PROJECT / GCP_ZONE come from the environment or from SWESIM_ENV_FILE (default ./.env) via a line parser.
set -euo pipefail

ENV_FILE=${SWESIM_ENV_FILE:-.env}
VM_NAME=${VM_NAME:-swesim-worker}

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

echo "forwarding localhost:8233 (Temporal UI), :7233 (Temporal), :8080 (dashboard) to $VM_NAME; Ctrl-C to stop" >&2
exec gcloud compute ssh --quiet "$VM_NAME" --project "$GCP_PROJECT" --zone "$GCP_ZONE" -- \
  -N -L 8233:127.0.0.1:8233 -L 7233:127.0.0.1:7233 -L 8080:127.0.0.1:8080
