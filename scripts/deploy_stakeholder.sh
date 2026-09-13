#!/bin/bash
# Deploy the stakeholder service to Modal and record its URL in the git-ignored .stakeholder_url.
# Reads ANTHROPIC_API_KEY, OPENAI_API_KEY and SWESIM_WORKER_SECRET from the ignored env file (python-dotenv,
# never shell evaluation), pushes them into the Modal Secret `swesim-llm`, deploys, probes /health.
# Prints the public URL and health JSON only; never a key or the worker secret.
# `allow_existing=True` keeps an existing Secret's values (Modal ignores differing env_dict); to rotate a key run
# `uv run modal secret delete swesim-llm` first, then redeploy.
set -euo pipefail
cd "$(dirname "$0")/.."

uv run --frozen python - <<'PYCODE'
import os
from pathlib import Path
import modal
from dotenv import dotenv_values
keys = dotenv_values(os.environ.get('SWESIM_ENV_FILE', str(Path.home() / 'Documents/.env')))
required = ['ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'SWESIM_WORKER_SECRET']
missing = [k for k in required if not keys.get(k)]
if missing: raise SystemExit('Set these keys once in the ignored env file (values not shown): ' + ', '.join(missing))
modal.Secret.objects.create('swesim-llm', {k: keys[k] for k in required}, allow_existing=True)
print('Modal secret swesim-llm present')
PYCODE

uv run --frozen modal deploy swesim/stakeholder/modal_app.py

uv run --frozen python - <<'PYCODE'
import os
from pathlib import Path
import modal
url = modal.Function.from_name('swesim-stakeholder', 'web').get_web_url()
if not url: raise SystemExit('deployment has no web URL')
p = Path('.stakeholder_url'); p.write_text(url.rstrip('/') + '\n'); os.chmod(p, 0o600)
print('stakeholder url: ' + url)
PYCODE

curl -fsS "$(cat .stakeholder_url)/health"; echo
