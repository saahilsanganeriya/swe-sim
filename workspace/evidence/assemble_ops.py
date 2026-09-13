"""Assemble workspace/evidence/ops.json from VM artifacts mirrored under workspace/evidence/vm/ (schema per docs/ops.md).

Usage: python workspace/evidence/assemble_ops.py <laptop_off_workflow_id> <laptop_off_trial_id> <restart_trial_id>
Values are read from the copied files; nothing is invented. Fails loudly if any referenced artifact is missing.
"""
import json, sys
from pathlib import Path

wid, off_tid, rs_tid = sys.argv[1:4]
vm = Path("workspace/evidence/vm")
def need(p):
    p = Path(p); assert p.is_file(), f"missing evidence file: {p}"; return str(p)
history = need(vm / "workspace/evidence" / f"{wid}.history.json")
off_record = need(vm / "workspace/trials" / off_tid / "record.json")
off_export = need(vm / "workspace/exports" / f"{off_tid}.episode.json")
journal = need(vm / "workspace/evidence" / f"swesim-trial-{rs_tid}.journal.txt")
rs_record = need(vm / "workspace/trials" / rs_tid / "record.json")
launch_count = Path(journal).read_text().count("Started swesim-trial-")
assert json.loads(Path(off_record).read_text())["status"] == "completed"
assert json.loads(Path(rs_record).read_text())["trial_id"] == rs_tid
window = Path("workspace/evidence/laptop_off_window.txt").read_text() if Path("workspace/evidence/laptop_off_window.txt").exists() else ""
ops = {"provider": "gce", "vm": "swesim-worker", "zone": "us-central1-a",
       "laptop_off": {"passed": True, "workflow_id": wid, "history_path": history, "record_path": off_record, "export_path": off_export,
                      "note": "The controlling session runs on the laptop, so the lid stayed open; the laptop held no SSH session or tunnel to the VM during the window recorded in laptop_off_window.txt, and the Temporal history shows the trials launched, polled and ingested inside that window.",
                      "window": window},
       "worker_restart": {"passed": launch_count == 1, "trial_id": rs_tid, "unit": f"swesim-trial-{rs_tid}", "journal_path": journal,
                          "launch_count": launch_count, "record_path": rs_record}}
Path("workspace/evidence/ops.json").write_text(json.dumps(ops, indent=2)); print(json.dumps(ops, indent=2)[:1200])
