#!/bin/bash
# Bootstrap the swesim VM. Runs ON THE VM as the login user (sudo for packages and system directories).
# Re-runnable: it updates the checkout to <commit>, re-syncs dependencies and restarts the units.
#
#   bash setup.sh <repo_https_url> <commit>
#
# Expects ~/swesim-bootstrap/env (dotenv: provider keys, SWESIM_WORKER_SECRET, SWESIM_STAKEHOLDER_URL, a
# read-only GH_TOKEN) and ~/swesim-bootstrap/modal.toml, both copied by create.sh. Installs: git, gh, uv,
# Python 3.12 (via uv), Temporal CLI v1.8.3, the repo at /opt/swesim, and three user systemd units.
# Docker is NOT required: Harbor runs on Modal. Trial units are transient user units created by the worker.
set -euo pipefail

REPO_URL=${1:?usage: setup.sh <repo_https_url> <commit>}
COMMIT=${2:?usage: setup.sh <repo_https_url> <commit>}
BOOT=$HOME/swesim-bootstrap
SWESIM_USER=$(id -un)
TEMPORAL_VERSION=1.8.3
TEMPORAL_URL='https://temporal.download/cli/archive/v1.8.3?platform=linux&arch=amd64'
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}

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
  local keys=(ANTHROPIC_API_KEY OPENAI_API_KEY SWESIM_WORKER_SECRET SWESIM_STAKEHOLDER_URL GH_TOKEN)
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

echo "== packages"
sudo apt-get update -y
sudo apt-get install -y --no-install-recommends git curl ca-certificates gnupg file
if ! command -v gh >/dev/null 2>&1; then
  sudo mkdir -p -m 755 /etc/apt/keyrings
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null
  sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
  sudo apt-get update -y
  sudo apt-get install -y gh
fi

echo "== uv and Python 3.12"
if [ ! -x "$HOME/.local/bin/uv" ]; then curl -LsSf https://astral.sh/uv/install.sh | sh; fi
export PATH="$HOME/.local/bin:$PATH"
uv python install 3.12

echo "== Temporal CLI v$TEMPORAL_VERSION (pinned; archive inspected before extraction)"
if ! /usr/local/bin/temporal --version 2>/dev/null | grep -q "temporal version $TEMPORAL_VERSION"; then
  curl -fLsS "$TEMPORAL_URL" -o /tmp/temporal_cli.tar.gz
  file /tmp/temporal_cli.tar.gz | grep -q 'gzip compressed'
  tar -tzf /tmp/temporal_cli.tar.gz | grep -qx 'temporal'
  tar -xzf /tmp/temporal_cli.tar.gz -C /tmp temporal
  sudo install -m 755 /tmp/temporal /usr/local/bin/temporal
  rm -f /tmp/temporal /tmp/temporal_cli.tar.gz
fi
/usr/local/bin/temporal --version

echo "== directories: /opt/swesim (checkout), /var/lib/swesim (Temporal DB), /etc/swesim (env file)"
sudo mkdir -p /opt/swesim /var/lib/swesim /etc/swesim
sudo chown "$SWESIM_USER:$SWESIM_USER" /opt/swesim /var/lib/swesim /etc/swesim
chmod 750 /var/lib/swesim
chmod 700 /etc/swesim

echo "== secrets (rendered from the copied file: only the worker's keys reach /etc/swesim/env; never from the repo)"
test -f "$BOOT/env" || { echo "$BOOT/env missing; create.sh renders it from SWESIM_ENV_FILE" >&2; exit 1; }
test -f "$BOOT/modal.toml" || { echo "$BOOT/modal.toml missing; create.sh copies it from ~/.modal.toml" >&2; exit 1; }
render_worker_env "$BOOT/env" /etc/swesim/env
install -m 600 "$BOOT/modal.toml" "$HOME/.modal.toml"
chmod 600 /etc/swesim/env "$HOME/.modal.toml"

echo "== repository at /opt/swesim (HTTPS clone; the read-only GH_TOKEN reaches git only through a transient credential helper)"
# The token lives in the process environment of this one git call; nothing is written to .git/config or ~/.gitconfig.
GIT_AUTH=(-c credential.helper= -c 'credential.helper=!f() { echo username=x-access-token; echo password=$GH_TOKEN; }; f')
GH_TOKEN_VALUE=$(env_get /etc/swesim/env GH_TOKEN) || { echo "GH_TOKEN missing from /etc/swesim/env" >&2; exit 1; }
if [ ! -d /opt/swesim/.git ]; then
  GH_TOKEN="$GH_TOKEN_VALUE" git "${GIT_AUTH[@]}" clone --quiet "$REPO_URL" /opt/swesim
fi
GH_TOKEN="$GH_TOKEN_VALUE" git -C /opt/swesim "${GIT_AUTH[@]}" fetch --quiet origin
unset GH_TOKEN_VALUE
# A local `main` tracking origin/main at the requested commit, so scripts/vm/update.sh can `git pull --ff-only` later.
git -C /opt/swesim checkout --quiet -B main "$COMMIT"
git -C /opt/swesim branch --quiet --set-upstream-to=origin/main main
echo "checked out $(git -C /opt/swesim rev-parse --short HEAD) on main (tracking origin/main)"

echo "== Python environment (locked)"
cd /opt/swesim
uv sync --extra dev --frozen --python 3.12
mkdir -p /opt/swesim/workspace/evidence
.venv/bin/python -c 'import temporalio, harbor, modal; print("imports ok")'

echo "== systemd user units (lingering so trial units outlive SSH sessions and the worker)"
sudo loginctl enable-linger "$SWESIM_USER"
mkdir -p "$HOME/.config/systemd/user"
for unit in swesim-temporal swesim-worker swesim-dashboard; do
  install -m 644 "/opt/swesim/scripts/vm/$unit.service" "$HOME/.config/systemd/user/$unit.service"
done
systemd-analyze --user verify "$HOME/.config/systemd/user/swesim-temporal.service" "$HOME/.config/systemd/user/swesim-worker.service" "$HOME/.config/systemd/user/swesim-dashboard.service"
systemctl --user daemon-reload
systemctl --user enable --now swesim-temporal swesim-worker swesim-dashboard
systemctl --user restart swesim-worker swesim-dashboard
sleep 3
systemctl --user --no-pager --lines=5 status swesim-temporal swesim-worker swesim-dashboard || true
systemctl --user is-active swesim-temporal swesim-worker swesim-dashboard

echo "setup complete. Reach the loopback ports through scripts/vm/tunnel.sh from the laptop."
