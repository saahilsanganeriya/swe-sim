#!/bin/bash
# Usage: bash scripts/verify.sh <milestone 1-10>. Exit 0 iff every check up to that milestone passes.
set -u
N=${1:-10}; FAIL=0
ok(){ printf "  [ok] %s\n" "$1"; }; bad(){ printf "  [FAIL] %s\n" "$1"; FAIL=1; }
has(){ [ -e "$1" ] && ok "$1 exists" || bad "$1 missing"; }
jq_get(){ python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(eval(sys.argv[2], {}, {'d': d}))" "$1" "$2" 2>/dev/null; }
echo "== M1 models"
if [ "$N" -ge 1 ]; then uv run pytest -q tests/test_models.py >/dev/null 2>&1 && ok "test_models" || bad "test_models"; fi
echo "== M2 verifier"
if [ "$N" -ge 2 ]; then uv run pytest -q tests/test_policy.py tests/test_parsers.py tests/test_grader.py >/dev/null 2>&1 && ok "verifier tests" || bad "verifier tests"
  grep -rlE '^\s*(import|from) (pydantic|typer|jinja2|fastapi)' swesim/verifier/*.py >/dev/null 2>&1 && bad "verifier imports non-stdlib" || ok "verifier stdlib-only"; fi
echo "== M3 compile + first Modal run"
if [ "$N" -ge 3 ]; then uv run pytest -q tests/test_compile.py tests/test_patch_agent.py >/dev/null 2>&1 && ok "compile tests" || bad "compile tests"
  ls workspace/jobs/validate/*/*--oracle/*__*/verifier/reward.json >/dev/null 2>&1 && ok "oracle ran on Modal" || bad "no oracle job under workspace/jobs/validate"; fi
echo "== M4 validate"
if [ "$N" -ge 4 ]; then V=$(ls workspace/scenarios/*/validation.json 2>/dev/null | head -1); [ -n "$V" ] && [ "$(jq_get "$V" "d['status']")" = "valid" ] && ok "a scenario is valid ($V)" || bad "no valid validation.json"; fi
echo "== M5 stakeholder + first episode"
if [ "$N" -ge 5 ]; then has .stakeholder_url; uv run pytest -q tests/test_session.py tests/test_service.py tests/test_run.py >/dev/null 2>&1 && ok "stakeholder tests" || bad "stakeholder tests"
  found=0; for r in workspace/trials/*/record.json; do [ -f "$r" ] || continue; c=$(jq_get "$r" "d['condition']"); q=$(jq_get "$r" "sum(1 for e in d.get('transcript',{}).get('events',[]) if e.get('kind')=='message' and e.get('role')=='user')"); [ "$c" = "interactive" ] && [ "${q:-0}" -ge 1 ] && found=1; done; [ $found = 1 ] && ok "interactive trial with >=1 question" || bad "no interactive trial with a question"; fi
echo "== M6 github-repo source"
if [ "$N" -ge 6 ]; then uv run pytest -q tests/test_github_repo.py >/dev/null 2>&1 && ok "github_repo tests" || bad "github_repo tests"; ls workspace/candidates/github-repo--*.jsonl >/dev/null 2>&1 && ok "github-repo candidates" || bad "no github-repo candidates"
  n=0; for v in workspace/scenarios/*/validation.json; do [ -f "$v" ] && [ "$(jq_get "$v" "d['status']")" = "valid" ] && n=$((n+1)); done; [ "$n" -ge 2 ] && ok "two valid scenarios" || bad "fewer than two valid scenarios ($n)"; fi
echo "== M7 temporal"
if [ "$N" -ge 7 ]; then uv run pytest -q tests/test_workflows.py >/dev/null 2>&1 && ok "workflow tests" || bad "workflow tests"; grep -qi "restart check" docs/ops.md 2>/dev/null && ok "restart check documented" || bad "docs/ops.md lacks restart check"; fi
echo "== M8 score/export/register"
if [ "$N" -ge 8 ]; then uv run pytest -q tests/test_score.py >/dev/null 2>&1 && ok "score tests" || bad "score tests"; ls workspace/scenarios/*/scores.json >/dev/null 2>&1 && ok "scores.json" || bad "no scores.json"; ls workspace/exports/*.episode.json >/dev/null 2>&1 && ok "episode manifests" || bad "no episode manifests"
  [ -f registry.json ] && [ "$(jq_get registry.json "sum(1 for s in d['scenarios'].values() if s['validation_status']=='valid')")" -ge 2 ] && ok "registry has 2 valid" || bad "registry lacks 2 valid scenarios"; fi
echo "== M9 VM + dashboard"
if [ "$N" -ge 9 ]; then for f in scripts/vm/create.sh scripts/vm/setup.sh scripts/vm/swesim-worker.service; do has "$f"; done; uv run pytest -q tests/test_dashboard.py >/dev/null 2>&1 && ok "dashboard tests" || bad "dashboard tests"; grep -qi "acceptance" docs/ops.md 2>/dev/null && ok "acceptance test documented" || bad "docs/ops.md lacks acceptance test"; fi
echo "== M10 example + docs"
if [ "$N" -ge 10 ]; then ls -d examples/*/runs/* >/dev/null 2>&1 && ok "example run" || bad "no examples/<id>/runs"; for f in README.md docs/grading.md docs/architecture.md; do has "$f"; done
  grep -rE 'sk-(ant|proj)-[A-Za-z0-9]{8}' examples docs README.md >/dev/null 2>&1 && bad "secret-like string in tracked docs/examples" || ok "no secrets in tracked files"; fi
echo "== all fast tests"; uv run pytest -q >/dev/null 2>&1 && ok "pytest" || bad "pytest"
[ $FAIL = 0 ] && echo "VERIFY OK through M$N" || echo "VERIFY FAILED (through M$N)"; exit $FAIL
