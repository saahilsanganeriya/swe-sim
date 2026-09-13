#!/bin/bash
# Update the VM to the current origin/main (laptop side): fast-forward /opt/swesim, re-sync the locked dependencies,
# restart the worker and the dashboard (never the Temporal server, never trial units), then print the three unit states
# and the checked-out commit. Refuses to run while the laptop branch has unpushed commits, because origin/main is what
# the VM pulls and those commits would not be in it.
#
# GCP_PROJECT / GCP_ZONE come from the environment or from SWESIM_ENV_FILE (default ~/Documents/.env) via a line parser.
# The VM-side pull authenticates with the read-only GH_TOKEN already in /etc/swesim/env (gh credential helper).
set -euo pipefail
cd "$(dirname "$0")/../.."

ENV_FILE=${SWESIM_ENV_FILE:-$HOME/Documents/.env}
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

STATUS_LINE=$(git status -sb | head -n1)
if printf '%s' "$STATUS_LINE" | grep -q '\[ahead'; then
  echo "refusing to update: this branch has unpushed commits ($STATUS_LINE); push first so origin/main carries them" >&2
  exit 1
fi
if ! printf '%s' "$STATUS_LINE" | grep -q '\.\.\.'; then
  echo "warning: the current branch has no upstream; cannot tell whether it is pushed ($STATUS_LINE)" >&2
fi
LOCAL_MAIN=$(git rev-parse --short origin/main 2>/dev/null || echo unknown)
echo "updating $VM_NAME to origin/main (laptop sees origin/main at $LOCAL_MAIN; run 'git fetch' first if that is stale)" >&2

# Runs on the VM. The clone stores no credentials: GH_TOKEN is read from /etc/swesim/env with the env_get line parser and
# handed to git through a transient credential helper for this one pull (process environment only; never .git/config).
# (read -d '' instead of $(cat <<EOF): bash 3.2 mis-parses heredocs with unbalanced parentheses inside $(...).)
read -r -d '' REMOTE <<'REMOTE_EOF' || true
set -euo pipefail
cd /opt/swesim
export PATH="$HOME/.local/bin:$PATH"
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
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
TOKEN=$(env_get /etc/swesim/env GH_TOKEN) || { echo "GH_TOKEN missing from /etc/swesim/env" >&2; exit 1; }
GH_TOKEN="$TOKEN" git -c credential.helper= -c 'credential.helper=!f() { echo username=x-access-token; echo password=$GH_TOKEN; }; f' pull --ff-only
unset TOKEN
uv sync --extra dev --frozen --python 3.12
systemctl --user restart swesim-worker swesim-dashboard
sleep 3
echo "units:"
for unit in swesim-temporal swesim-worker swesim-dashboard; do
  printf '  %s: %s\n' "$unit" "$(systemctl --user is-active "$unit" || true)"
done
echo "commit: $(git rev-parse --short HEAD) $(git log -1 --format=%s)"
REMOTE_EOF
gcloud compute ssh --quiet "$VM_NAME" --project "$GCP_PROJECT" --zone "$GCP_ZONE" --command "$REMOTE"
