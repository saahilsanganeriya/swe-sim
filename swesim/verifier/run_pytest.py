# Trusted pytest launcher, copied to /tests/run_pytest.py (root-owned) and started as
#   <task interpreter> -I /tests/run_pytest.py <pytest args>
# from the graded repository directory. -I keeps the script directory and PYTHON* variables out
# of the interpreter; pytest is imported from the task interpreter's site-packages FIRST, and only
# then are the submitted source roots added to sys.path. This file may run under old interpreters
# pinned by historical tasks, so it uses only long-stable syntax and stdlib modules.
import json
import os
import sys
from pathlib import Path

import pytest

_MANIFEST = os.environ.get('SWESIM_NODEIDS')


class Manifest:
    """Records the exact collected nodeids so the grader can require them to match the contract."""

    def pytest_collection_finish(self, session):
        if _MANIFEST:
            Path(_MANIFEST).write_text(json.dumps([item.nodeid for item in session.items]))


for rel in reversed(['.', 'src']):
    p = Path.cwd() / rel
    if p.is_dir():
        sys.path.insert(0, str(p))
raise SystemExit(pytest.main(sys.argv[1:], plugins=[Manifest()]))
