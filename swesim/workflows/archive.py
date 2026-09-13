"""Idempotent archive copies using file operations supported by the bucket mount.

Every destination file is published with write-then-rename. Existing files must
match the source exactly; interrupted copies can resume without replacing evidence.
The caller publishes its snapshot manifest only after the complete tree is copied.
Flat-namespace GCS directory renames are not atomic; this deliberately renames files only:
https://github.com/GoogleCloudPlatform/gcsfuse/blob/master/docs/semantics.md
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


def fingerprint(path: Path) -> dict:
    return {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'mode': path.stat().st_mode & 0o777}


def copy_file(source: Path, destination: Path) -> None:
    if source.is_symlink():
        raise ValueError(f'cannot freeze a symlink as an immutable archive: {source.name}')
    expected = fingerprint(source)
    if destination.exists():
        if destination.is_symlink() or fingerprint(destination) != expected:
            raise ValueError(f'archive already contains different bytes or mode: {destination.name}')
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=destination.parent, prefix='.archive-')
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(source.read_bytes()); handle.flush(); os.fsync(handle.fileno())
        if temporary.stat().st_mode & 0o777 != expected['mode']:
            temporary.chmod(expected['mode'])
        if fingerprint(temporary) != expected:
            raise ValueError('source changed while copying archive')
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def copy_tree(source: Path, destination: Path) -> None:
    if not source.is_dir():
        return
    for path in sorted(source.rglob('*')):
        if path.is_symlink():
            raise ValueError('archive source contains a symlink')
        if path.is_file():
            copy_file(path, destination / path.relative_to(source))


def manifest(directory: Path) -> dict:
    return {path.relative_to(directory).as_posix(): fingerprint(path) for path in sorted(directory.rglob('*'))
            if path.is_file() and path.name not in ('snapshot.json', 'archived.json')}
