#!/bin/bash
# Mirror the record from the workspace bucket into the laptop checkout (merge only: nothing local is deleted).
# Copied: workspace/{scenarios,tasks,exports,candidates,evidence,probes,calibration}, workspace/trials without any
# trials/<id>/task/ (task.toml carries the trial's session token), and registry.json. Harbor job directories are not
# mirrored (large; the essentials live in the trial records).
#
#   bash scripts/cloud/pull.sh [bucket]     # bucket: argument, SWESIM_BUCKET, or infra/outputs.json
set -euo pipefail
cd "$(dirname "$0")/../.."

BUCKET=${1:-${SWESIM_BUCKET:-}}
if [ -z "$BUCKET" ] && [ -f infra/outputs.json ]; then
  BUCKET=$(python3 -c 'import json; print(json.load(open("infra/outputs.json"))["bucket"]["value"])')
fi
: "${BUCKET:?bucket name required (argument, SWESIM_BUCKET, or infra/outputs.json)}"
DEST=${SWESIM_WORKSPACE_ROOT:-.}
GC=(gcloud --quiet)

before=$(ls "$DEST"/workspace/trials/*/record.json 2>/dev/null | wc -l | tr -d ' ')
for prefix in scenarios tasks exports candidates evidence probes calibration; do
  if "${GC[@]}" storage ls "gs://$BUCKET/$prefix/" >/dev/null 2>&1; then
    mkdir -p "$DEST/workspace/$prefix"
    "${GC[@]}" storage rsync -r "gs://$BUCKET/$prefix" "$DEST/workspace/$prefix"
  fi
done
if "${GC[@]}" storage ls "gs://$BUCKET/trials/" >/dev/null 2>&1; then
  mkdir -p "$DEST/workspace/trials"
  "${GC[@]}" storage rsync -r --exclude='^[^/]+/task/.*' "gs://$BUCKET/trials" "$DEST/workspace/trials"
fi
if "${GC[@]}" storage ls "gs://$BUCKET/registry.json" >/dev/null 2>&1; then
  "${GC[@]}" storage cp "gs://$BUCKET/registry.json" "$DEST/registry.json"
fi

leaked=$(grep -rl --include=task.toml 'SWESIM_SESSION_TOKEN=' "$DEST/workspace/trials" 2>/dev/null || true)
if [ -n "$leaked" ]; then
  echo "refusing: a task.toml with a session token was mirrored; removing it" >&2
  printf '%s\n' "$leaked" | xargs rm -f
  exit 1
fi
after=$(ls "$DEST"/workspace/trials/*/record.json 2>/dev/null | wc -l | tr -d ' ')
scenarios=$(python3 -c "import json; print(len(json.load(open('$DEST/registry.json'))['scenarios']))" 2>/dev/null || echo "?")
echo "laptop trial records: $before -> $after; registry.json from gs://$BUCKET ($scenarios scenarios)"
