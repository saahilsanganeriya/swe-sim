#!/bin/bash
# Create the swesim GCE VM and bootstrap it (laptop side). Re-runnable: an existing instance is reused.
#
# Inputs (never printed, never evaluated as shell):
#   SWESIM_ENV_FILE  dotenv file, default ./.env. Read with a line parser (env_get) for GCP_PROJECT and
#                    GCP_ZONE when they are not already in the environment. The laptop file itself is NOT shipped:
#                    render_worker_env writes a mode-0600 temp file holding only the worker's keys
#                    (ANTHROPIC_API_KEY, OPENAI_API_KEY, SWESIM_WORKER_SECRET, SWESIM_STAKEHOLDER_URL, read-only GH_TOKEN)
#                    and that rendered file becomes /etc/swesim/env on the VM (systemd EnvironmentFile= and python-dotenv).
#   MODAL_TOML       Modal token file, default ~/.modal.toml, copied to the VM (mode 0600).
#   VM_NAME          default swesim-worker.   SWESIM_REPO_URL  default: the https form of `git remote origin`.
# The VM clones the repo at the current HEAD commit, so push before running this.
set -euo pipefail
cd "$(dirname "$0")/../.."

ENV_FILE=${SWESIM_ENV_FILE:-.env}
MODAL_TOML=${MODAL_TOML:-$HOME/.modal.toml}
VM_NAME=${VM_NAME:-swesim-worker}
MACHINE_TYPE=${MACHINE_TYPE:-e2-standard-4}
IMAGE_FAMILY=${IMAGE_FAMILY:-debian-12}
IMAGE_PROJECT=${IMAGE_PROJECT:-debian-cloud}
DISK_SIZE=${DISK_SIZE:-100GB}

# env_get FILE KEY: print the value of the first `KEY=value` line (optional quotes stripped). No evaluation.
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

# render_worker_env SRC DST: write only the worker's keys from SRC into DST (mode 0600), one `KEY='value'` per line so
# systemd EnvironmentFile= and python-dotenv both read the value literally. Values are never printed.
render_worker_env() {
  local src=$1 dst=$2 key value
  local keys=(ANTHROPIC_API_KEY OPENAI_API_KEY SWESIM_WORKER_SECRET SWESIM_STAKEHOLDER_URL GH_TOKEN TEMPORAL_ADDRESS TEMPORAL_NAMESPACE TEMPORAL_API_KEY)
  local missing=()
  : > "$dst"
  chmod 600 "$dst"
  for key in "${keys[@]}"; do
    if value=$(env_get "$src" "$key"); then
      case $value in
        *\'*|*$'\n'*) echo "$key: values containing a single quote or newline are not supported" >&2; return 1 ;;
      esac
      printf "%s='%s'\n" "$key" "$value" >> "$dst"
    else
      missing+=("$key")
    fi
  done
  if [ ${#missing[@]} -gt 0 ]; then echo "warning: not set in $src: ${missing[*]}" >&2; fi
}

test -f "$ENV_FILE" || { echo "env file $ENV_FILE missing" >&2; exit 1; }
test -f "$MODAL_TOML" || { echo "Modal token file $MODAL_TOML missing" >&2; exit 1; }
GCP_PROJECT=${GCP_PROJECT:-$(env_get "$ENV_FILE" GCP_PROJECT || true)}
GCP_ZONE=${GCP_ZONE:-$(env_get "$ENV_FILE" GCP_ZONE || true)}
: "${GCP_PROJECT:?GCP_PROJECT missing (environment or $ENV_FILE)}" "${GCP_ZONE:?GCP_ZONE missing (environment or $ENV_FILE)}"
for key in ANTHROPIC_API_KEY SWESIM_WORKER_SECRET GH_TOKEN; do
  env_get "$ENV_FILE" "$key" >/dev/null || { echo "$key is required in $ENV_FILE" >&2; exit 1; }
done

WORKER_ENV=$(mktemp "${TMPDIR:-/tmp}/swesim-worker-env.XXXXXX")
trap 'rm -f "$WORKER_ENV"' EXIT
render_worker_env "$ENV_FILE" "$WORKER_ENV"

REPO_URL=${SWESIM_REPO_URL:-$(git remote get-url origin)}
case $REPO_URL in git@github.com:*) REPO_URL="https://github.com/${REPO_URL#git@github.com:}" ;; esac
COMMIT=$(git rev-parse HEAD)
if ! git branch -r --contains "$COMMIT" 2>/dev/null | grep -q .; then
  echo "warning: commit $COMMIT is not on any remote branch; the VM clone will fail to check it out unless you push first" >&2
fi

GC=(gcloud --quiet --project "$GCP_PROJECT")
if ! "${GC[@]}" compute instances describe "$VM_NAME" --zone "$GCP_ZONE" >/dev/null 2>&1; then
  gcloud compute instances create --quiet "$VM_NAME" --project "$GCP_PROJECT" --zone "$GCP_ZONE" \
    --machine-type "$MACHINE_TYPE" --image-family "$IMAGE_FAMILY" --image-project "$IMAGE_PROJECT" \
    --boot-disk-size "$DISK_SIZE" --boot-disk-type pd-balanced
else
  echo "instance $VM_NAME exists; reusing it" >&2
fi

echo "waiting for ssh on $VM_NAME" >&2
for _ in $(seq 1 30); do
  if "${GC[@]}" compute ssh "$VM_NAME" --zone "$GCP_ZONE" --command 'true' >/dev/null 2>&1; then break; fi
  sleep 10
done
SSH=("${GC[@]}" compute ssh "$VM_NAME" --zone "$GCP_ZONE" --command)
SCP=("${GC[@]}" compute scp --zone "$GCP_ZONE")

"${SSH[@]}" 'mkdir -p ~/swesim-bootstrap && chmod 700 ~/swesim-bootstrap'
"${SCP[@]}" "$WORKER_ENV" "$VM_NAME":~/swesim-bootstrap/env
"${SCP[@]}" "$MODAL_TOML" "$VM_NAME":~/swesim-bootstrap/modal.toml
"${SCP[@]}" scripts/vm/setup.sh "$VM_NAME":~/swesim-bootstrap/setup.sh
"${SSH[@]}" "chmod 600 ~/swesim-bootstrap/env ~/swesim-bootstrap/modal.toml && bash ~/swesim-bootstrap/setup.sh '$REPO_URL' '$COMMIT'"

echo "VM $VM_NAME is set up at commit $COMMIT."
echo "Next: bash scripts/vm/tunnel.sh   (Temporal UI http://localhost:8233, dashboard http://localhost:8080)"
