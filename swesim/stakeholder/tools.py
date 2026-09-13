"""Read-only stakeholder tools for the diff-only pilot review mode.

Everything here exposes agent-supplied text and labels it ``verified: False``. No sandbox, no shell, no code
execution. ``run_check`` (spec section 8) is deferred until a prepared user image exists; when it lands it must
use byte upload, no secrets and no network, never a diff interpolated into a shell heredoc.
"""
from __future__ import annotations

MAX_DIFF_BYTES = 200_000


def read_diff(diff: str) -> dict:
    if len(diff.encode()) > MAX_DIFF_BYTES: raise ValueError("review snapshot too large")
    return {"tool": "read_diff", "content": diff, "verified": False}


def read_changed_file(files: dict, path: str, changed_paths: list) -> dict:
    if path not in changed_paths or path not in files: raise ValueError("path not in supplied snapshot")
    return {"tool": "read_changed_file", "path": path, "content": files[path], "verified": False}


def changed_paths(diff: str) -> list[str]:
    """Paths named by ``+++ b/<path>`` headers of a unified diff, in order, without duplicates."""
    out: list[str] = []
    for line in diff.splitlines():
        if not line.startswith("+++ "): continue
        path = line[4:].split("\t")[0].strip()
        if path == "/dev/null": continue
        if path.startswith("b/"): path = path[2:]
        if path and path not in out: out.append(path)
    return out
