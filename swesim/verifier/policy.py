"""Path policy, safe archive extraction and change-set application.

Standard library only: this file is copied into the verifier container.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import tarfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

MAX_BYTES = 500 * 1024 * 1024
MAX_FILES = 100000
# Never carried from the submission into the graded tree, and never diffed.
EXCLUDED = {'.git', '.venv', 'node_modules', '__pycache__', '.pytest_cache', '.tox', '.nox', '.mypy_cache', '.ruff_cache'}
# Always protected in addition to the task's own list (contract test files come from the task).
DEFAULT_PROTECTED = [
    '**/conftest.py', '**/sitecustomize.py', '**/usercustomize.py', '**/*.pth',
    'pytest.ini', 'tox.ini', 'setup.cfg', 'pyproject.toml', '.github/**', '.gitattributes', '.gitignore',
    '**/uv.lock', '**/poetry.lock', '**/Pipfile.lock', '**/requirements*.txt',
    # pytest reads its config from the first of these found walking up from the test paths, at any depth
    '**/pytest.ini', '**/.pytest.ini', '**/tox.ini', '**/setup.cfg', '**/pyproject.toml',
    # installed-package metadata inside the tree: entry_points.txt can register a pytest11 plugin
    '**/*.egg-info/**', '**/*.dist-info/**',
]


def _norm(p):
    p = unicodedata.normalize('NFC', str(p).replace('\\', '/'))
    while p.startswith('./'):
        p = p[2:]
    return p.casefold()


def is_protected(path, globs):
    n = _norm(path)
    for g in globs:
        g = _norm(g)
        if fnmatch.fnmatchcase(n, g):
            return True
        if g.startswith('**/') and fnmatch.fnmatchcase(n, g[3:]):
            return True
        if g.endswith('/**'):
            base = g[:-3]
            if n == base or n.startswith(base + '/'):
                return True
    return False


@dataclass
class ExtractResult:
    extracted: int = 0
    rejected: list = field(default_factory=list)   # (member name, reason)


class InvalidArchive(ValueError):
    pass


def safe_extract(tar_path, dest):
    """Extract regular files and directories only, under dest, with bounds and collision checks."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    res = ExtractResult()
    seen = set()
    total = 0
    try:
        archive = tarfile.open(str(tar_path))
    except (tarfile.TarError, OSError, EOFError) as e:
        raise InvalidArchive('unreadable archive: %s' % e)
    try:
        with archive as t:
            for i, m in enumerate(t):
                if i >= MAX_FILES:
                    raise InvalidArchive('too many members')
                name = m.name
                p = PurePosixPath(name)
                if '\\' in name or p.is_absolute() or '..' in p.parts:
                    res.rejected.append((name, 'escape'))
                    continue
                if not (m.isfile() or m.isdir()):
                    res.rejected.append((name, 'non-regular'))
                    continue
                parts = p.parts  # PurePosixPath drops '.' components
                if any(part in EXCLUDED for part in parts):
                    continue
                key = _norm(p.as_posix())
                if key != '.':
                    if key in seen:
                        raise InvalidArchive('duplicate or case/Unicode collision: %s' % name)
                    seen.add(key)
                if m.size < 0:
                    raise InvalidArchive('negative member size')
                total += m.size
                if total > MAX_BYTES:
                    raise InvalidArchive('oversized')
                dst = dest.joinpath(*parts)
                if m.isdir():
                    dst.mkdir(parents=True, exist_ok=True)
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    src = t.extractfile(m)
                    if src is None:
                        raise InvalidArchive('unreadable member: %s' % name)
                    with src, open(dst, 'xb') as out:
                        shutil.copyfileobj(src, out)
                    dst.chmod(0o755 if m.mode & 0o111 else 0o644)
                res.extracted += 1
    except (tarfile.TarError, EOFError) as e:
        raise InvalidArchive('corrupt archive: %s' % e)
    return res


def _hash(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _files(root):
    """Regular files under root keyed by posix relative path; excluded dirs and symlinks are skipped."""
    root = Path(root)
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED and not (here / d).is_symlink())
        for name in filenames:
            p = here / name
            if p.is_symlink() or not p.is_file():
                continue
            out[p.relative_to(root).as_posix()] = p
    return out


@dataclass
class ChangeSet:
    added: list = field(default_factory=list)
    modified: list = field(default_factory=list)
    deleted: list = field(default_factory=list)


def change_set(pristine, tree):
    a, b = _files(pristine), _files(tree)
    cs = ChangeSet()
    for rel in sorted(a.keys() | b.keys()):
        if rel not in b:
            cs.deleted.append(rel)
        elif rel not in a:
            cs.added.append(rel)
        elif a[rel].stat().st_size != b[rel].stat().st_size or _hash(a[rel]) != _hash(b[rel]):
            cs.modified.append(rel)
    return cs


def change_hash(base, tree, cs):
    """Content hash of a change set: path plus before/after file digests."""
    base, tree = Path(base), Path(tree)
    rows = []
    for p in sorted(set(cs.added) | set(cs.modified) | set(cs.deleted)):
        b, t = base / p, tree / p
        rows.append({'path': p,
                     'before': _hash(b) if b.is_file() else None,
                     'after': _hash(t) if t.is_file() else None})
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@dataclass
class ApplyResult:
    applied: list = field(default_factory=list)
    dropped: list = field(default_factory=list)


def apply_change_set(target, tree, cs, protected):
    """Copy allowed changes from tree onto target; protected paths are dropped and reported."""
    target, tree = Path(target), Path(tree)
    res = ApplyResult()
    deleted = set(cs.deleted)
    deletions, writes = [], []
    for rel in sorted(set(cs.added) | set(cs.modified) | deleted):
        if is_protected(rel, protected):
            res.dropped.append(rel)
            continue
        (deletions if rel in deleted else writes).append(rel)
    for rel in deletions:
        dst = target / rel
        if dst.is_symlink() or dst.is_file():
            dst.unlink()
        res.applied.append(rel)
    for rel in deletions:  # prune directories emptied by deletions so a file may take their place
        parent = (target / rel).parent
        while parent != target and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    for rel in writes:
        src, dst = tree / rel, target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_symlink():
            dst.unlink()
        shutil.copyfile(src, dst)
        shutil.copymode(src, dst)
        res.applied.append(rel)
    res.applied.sort()
    return res
