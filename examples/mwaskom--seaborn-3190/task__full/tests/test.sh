#!/bin/bash
# Separate, offline verifier entrypoint. The grader writes /logs/verifier/reward.json (finite numbers only);
# if it never gets there, the trap records an evaluator failure (outcome 7) so Harbor still sees a reward.
mkdir -p /logs/verifier
trap 'if [ ! -f /logs/verifier/reward.json ]; then echo "{\"reward\": 0, \"success\": 0, \"outcome_code\": 7}" > /logs/verifier/reward.json; fi' EXIT
set -o pipefail
/usr/local/bin/swesim-python -I /tests/grader.py --config /tests/grader_config.json 2>&1 | tee /logs/verifier/grader.log
