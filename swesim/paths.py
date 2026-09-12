import json, os, tempfile
from pathlib import Path

def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, sort_keys=True, indent=2, allow_nan=False); f.flush(); os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)

class Workspace:
    def __init__(self, root: Path | str = "workspace", registry: Path | str = "registry.json"):
        self.root = Path(root).resolve(); self.registry_path = Path(registry).resolve()
    def scenario_dir(self, sid):
        from swesim.models import ID_RE
        if not ID_RE.fullmatch(sid): raise ValueError("invalid scenario id")
        return self.root / "scenarios" / sid
    def task_dir(self, sid, condition):
        if condition not in {"full", "hidden", "interactive"}: raise ValueError("condition")
        return self.root / "tasks" / f"{sid}__{condition}"
    @property
    def jobs_dir(self): return self.root / "jobs"
    @property
    def candidates_dir(self): return self.root / "candidates"
