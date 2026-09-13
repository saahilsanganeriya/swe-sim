#!/bin/bash
# Verify the hosted stack (brief §9). Exit non-zero when any check fails. The full output is saved to
# $EVIDENCE_DIR/verify_cloud-<UTC>.txt (default workspace/evidence/cloud) and copied into gs://<bucket>/evidence/cloud/.
#
#   EVIDENCE_DIR=... SWESIM_ENV_FILE=... bash scripts/cloud/verify_cloud.sh
#
# Inputs: infra/outputs.json when present, else `terraform -chdir=infra output -json`; gcloud as the operator or the
# deployer; SWESIM_ENV_FILE for the Temporal check. Prints names and auth modes, never a secret.
#
# Checks:
#   1 Temporal Cloud reachable with the API key (auth mode only)   2 IAP: the console answers 302 to accounts.google.com
#   3 the four worker pools and the console are Ready              4 the bucket holds registry.json
#   5 pools + console run the image Terraform pinned               6 one Job and one execution per swesim-trial-* Job
#   7 Postgres counts via `swesim index status` when the command and SWESIM_DATABASE_URL are available (else a notice)
set -uo pipefail
cd "$(dirname "$0")/../.."

UTC=$(date -u +%Y%m%dT%H%M%SZ)
EVIDENCE_DIR=${EVIDENCE_DIR:-workspace/evidence/cloud}
mkdir -p "$EVIDENCE_DIR"
OUT="$EVIDENCE_DIR/verify_cloud-$UTC.txt"
HEADERS="$EVIDENCE_DIR/iap-headers-$UTC.txt"

outputs=$(mktemp)
trap 'rm -f "$outputs"' EXIT
if [ -f infra/outputs.json ]; then cp infra/outputs.json "$outputs"; else terraform -chdir=infra output -json > "$outputs"; fi
o() { python3 -c 'import json,sys; v=json.load(open(sys.argv[1]))[sys.argv[2]]["value"]; print(v if isinstance(v, str) else json.dumps(v))' "$outputs" "$1"; }
pools() { python3 -c 'import json,sys; print(" ".join(sorted(json.load(open(sys.argv[1]))["worker_pools"]["value"].values())))' "$outputs"; }
ready_of_service() { python3 -c 'import json,sys; d=json.load(sys.stdin); print(next((c.get("status") for c in d.get("status",{}).get("conditions",[]) if c.get("type")=="Ready"), "Unknown"))'; }

PROJECT=$(o project); REGION=$(o region); BUCKET=$(o bucket); CONSOLE_URL=$(o console_url); CONSOLE=$(o console_service); IMAGE=$(o image)
GC=(gcloud --project "$PROJECT" --quiet)

fail=0
pass() { echo "PASS  $*"; }
failed() { echo "FAIL  $*"; fail=1; }
notice() { echo "NOTE  $*"; }

run_checks() {
  echo "verify_cloud $UTC project=$PROJECT region=$REGION bucket=$BUCKET console=$CONSOLE_URL"
  echo "image (terraform): $IMAGE"

  # 1 Temporal
  if desc=$(uv run --frozen python - <<'PY' 2>&1
import asyncio
from swesim.workflows.client import connect, describe_connection
async def main():
    client = await asyncio.wait_for(connect(), 45)
    async for _ in client.list_workflows(limit=1):
        break
    print(describe_connection())
asyncio.run(main())
PY
  ); then pass "temporal: $desc"; else failed "temporal: $(echo "$desc" | tail -n 3 | tr '\n' ' ')"; fi

  # 2 IAP redirect
  if curl -sS -o /dev/null -D "$HEADERS" --max-time 30 "$CONSOLE_URL"; then
    status=$(head -n1 "$HEADERS" | awk '{print $2}')
    location=$(grep -i '^location:' "$HEADERS" | head -n1 | tr -d '\r')
    if [ "$status" = "302" ] && grep -qi 'accounts.google.com' <<<"$location"; then
      pass "iap: $status -> accounts.google.com (headers: $HEADERS)"
    else
      failed "iap: expected 302 to accounts.google.com, got status=$status ${location:-<no location>}"
    fi
  else
    failed "iap: curl $CONSOLE_URL failed"
  fi

  # 3 readiness
  for p in $(pools); do
    state=$("${GC[@]}" run worker-pools describe "$p" --region "$REGION" --format='value(terminalCondition.state)' 2>&1)
    if [ "$state" = "CONDITION_SUCCEEDED" ]; then pass "pool $p ready"; else failed "pool $p state=$state"; fi
  done
  ready=$("${GC[@]}" run services describe "$CONSOLE" --region "$REGION" --format=json 2>/dev/null | ready_of_service)
  if [ "$ready" = "True" ]; then pass "console $CONSOLE ready"; else failed "console $CONSOLE Ready=$ready"; fi

  # 4 registry in the bucket
  if "${GC[@]}" storage ls "gs://$BUCKET/registry.json" >/dev/null 2>&1; then pass "bucket gs://$BUCKET has registry.json"; else failed "bucket gs://$BUCKET has no registry.json"; fi

  # 5 image parity
  case $IMAGE in *@sha256:*) ;; *) notice "image is not digest-pinned (bootstrap apply); parity is checked on the tag" ;; esac
  for p in $(pools); do
    running=$("${GC[@]}" run worker-pools describe "$p" --region "$REGION" --format='value(template.containers[0].image)' 2>/dev/null)
    if [ "$running" = "$IMAGE" ]; then pass "pool $p runs the pinned image"; else failed "pool $p runs ${running:-<unknown>} != $IMAGE"; fi
  done
  running=$("${GC[@]}" run services describe "$CONSOLE" --region "$REGION" --format='value(spec.template.spec.containers[0].image)' 2>/dev/null)
  if [ "$running" = "$IMAGE" ]; then pass "console runs the pinned image"; else failed "console runs ${running:-<unknown>} != $IMAGE"; fi

  # 6 trial Jobs: one Job per trial id, one execution per Job
  jobs=$("${GC[@]}" run jobs list --region "$REGION" --filter='metadata.name~^swesim-trial-' --format='value(metadata.name)' 2>/dev/null | sort)
  if [ -z "$jobs" ]; then
    notice "no swesim-trial-* Jobs yet (nothing launched on this stack)"
  else
    dups=$(printf '%s\n' "$jobs" | uniq -d)
    [ -z "$dups" ] && pass "trial jobs: $(printf '%s\n' "$jobs" | wc -l | tr -d ' ') unique names" || failed "duplicate trial job names: $dups"
    n=0
    for j in $jobs; do
      n=$((n + 1)); [ $n -gt 50 ] && { notice "execution check capped at 50 jobs"; break; }
      count=$("${GC[@]}" run jobs executions list --job "$j" --region "$REGION" --format='value(metadata.name)' 2>/dev/null | wc -l | tr -d ' ')
      if [ "$count" = "1" ]; then pass "job $j: 1 execution"; else failed "job $j: $count executions"; fi
    done
  fi

  # 7 Postgres counts
  if uv run --frozen swesim index --help >/dev/null 2>&1; then
    if uv run --frozen python -c 'import sys; from swesim.settings import has_secret; sys.exit(0 if has_secret("SWESIM_DATABASE_URL") else 1)' 2>/dev/null; then
      if out=$(uv run --frozen swesim index status 2>&1); then pass "index status: $(echo "$out" | tr '\n' ' ')"; else failed "index status: $(echo "$out" | tail -n 2 | tr '\n' ' ')"; fi
    else
      notice "index status skipped: SWESIM_DATABASE_URL is not available here (Cloud SQL is reachable from Cloud Run only)"
    fi
  else
    notice "index status skipped: 'swesim index' is not available in this checkout"
  fi

  echo "result: $([ $fail -eq 0 ] && echo OK || echo FAILED)"
  return $fail
}

run_checks 2>&1 | tee "$OUT"
rc=${PIPESTATUS[0]}
if "${GC[@]}" storage cp "$OUT" "$HEADERS" "gs://$BUCKET/evidence/cloud/" >/dev/null 2>&1; then
  echo "evidence: $OUT (mirrored to gs://$BUCKET/evidence/cloud/)"
else
  echo "evidence: $OUT (bucket mirror skipped)"
fi
exit "$rc"
