"""Root-owned collector installed at /opt/swesim/collect.py.

Invoked by the [[verifier.collect]] hook as root with `env -i` (cleared environment) and `swesim-python -I`.
Standard library only. Kills the agent user's processes, bounds the tree, archives it into the root-only
/var/swesim-collect, freezes the stakeholder session in `finally`, and always writes a JSON receipt.
No repository code and no git command runs here: the archive is the only trust anchor the verifier reads.
"""
import hashlib, json, os, pathlib, subprocess, tarfile, time, urllib.request

OUT = pathlib.Path('/var/swesim-collect')
LIMIT_BYTES = 500 * 1024 * 1024
LIMIT_MEMBERS = 100000
EXCLUDED = {'.git', '.venv', 'node_modules', '__pycache__', '.pytest_cache'}
RECEIPT_KEYS = ('status', 'rc', 'user', 'sha256', 'bytes', 'files', 'freeze_status', 'transcript_hash')
AGENT_USER = 'agent'
PKILL = '/usr/bin/pkill'


def kill_agent_processes(user=AGENT_USER, pkill=PKILL, attempts=10):
    """SIGKILL every process of the agent user; pkill exits 1 once nothing matches."""
    for _ in range(attempts):
        p = subprocess.run([pkill, '-KILL', '-u', user], capture_output=True)
        if p.returncode == 1: return
        if p.returncode != 0: raise RuntimeError('pkill failed with rc %d' % p.returncode)
        time.sleep(.05)
    raise RuntimeError('agent processes remain after %d kill rounds' % attempts)


def scan_tree(repo, limit_bytes=LIMIT_BYTES, limit_members=LIMIT_MEMBERS):
    """Return (status, members, apparent_bytes). Symlinks are recorded, never followed or counted."""
    repo = pathlib.Path(repo)
    if repo.is_symlink() or not repo.is_dir(): return 'invalid-tree', [], 0
    members = []; total = 0
    for top, dirs, names in os.walk(repo, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED)
        for name in sorted(names) + list(dirs):
            path = pathlib.Path(top) / name; st = path.lstat()
            if not path.is_symlink() and path.is_file(): total += st.st_size
            members.append(path)
            if total > limit_bytes or len(members) > limit_members: return 'oversized', [], total
    return 'ok', members, total


def write_archive(repo, members, tar_path):
    repo = pathlib.Path(repo); tar_path = pathlib.Path(tar_path)
    with tarfile.open(tar_path, 'w', dereference=False) as t:
        for p in sorted(members): t.add(p, arcname=p.relative_to(repo).as_posix(), recursive=False)
    h = hashlib.sha256()
    with tar_path.open('rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''): h.update(b)
    return h.hexdigest()


def freeze(stakeholder_url, token, opener=urllib.request.urlopen, attempts=2, timeout=30):
    """POST /freeze with the session bearer token. Returns (freeze_status, transcript_hash)."""
    if not token or not stakeholder_url: return 'n/a', None
    for _ in range(attempts):
        try:
            req = urllib.request.Request(stakeholder_url.rstrip('/') + '/freeze', data=b'{}',
                                         headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
            with opener(req, timeout=timeout) as r: frozen = json.loads(r.read())
            if frozen.get('frozen') and len(str(frozen.get('transcript_hash', ''))) == 64:
                return 'ok', frozen['transcript_hash']
        except Exception:
            pass
    return 'failed', None


def collect(repo, out, stakeholder_url, token, kill=kill_agent_processes, opener=urllib.request.urlopen,
            limit_bytes=LIMIT_BYTES, limit_members=LIMIT_MEMBERS):
    out = pathlib.Path(out)
    receipt = {'status': 'collection-failure', 'rc': 1, 'user': 'root', 'sha256': None, 'bytes': None, 'files': None,
               'freeze_status': 'n/a', 'transcript_hash': None}
    try:
        out.mkdir(mode=0o700, exist_ok=True); out.chmod(0o700)
        for p in out.iterdir():
            if p.is_file() or p.is_symlink(): p.unlink()
        kill()
        status, members, total = scan_tree(repo, limit_bytes, limit_members)
        receipt['status'] = status
        if status == 'ok':
            receipt.update(sha256=write_archive(repo, members, out / 'app.tar'), bytes=total, files=len(members), rc=0)
    except Exception as e:
        receipt['status'] = 'collection-failure'; receipt['rc'] = 1; receipt['error'] = type(e).__name__ + ': ' + str(e)
    finally:
        try:
            receipt['freeze_status'], receipt['transcript_hash'] = freeze(stakeholder_url, token, opener)
        except Exception as e:  # freeze must never prevent the receipt
            receipt['freeze_status'] = 'failed'; receipt['freeze_error'] = type(e).__name__ + ': ' + str(e)
        tmp = out / '.collect.status.tmp'; tmp.write_text(json.dumps(receipt)); os.replace(tmp, out / 'collect.status')
    return receipt


def main():
    repo = os.environ.get('SWESIM_REPO_DIR', '')
    url = os.environ.get('SWESIM_STAKEHOLDER_URL', ''); token = os.environ.get('SWESIM_SESSION_TOKEN', '')

    def missing_repo_dir():
        raise RuntimeError('SWESIM_REPO_DIR unset in collector environment')
    # A missing repo dir still produces a receipt (and a freeze) so the grader attributes a collection failure.
    collect(pathlib.Path(repo or '/nonexistent-swesim-repo'), OUT, url, token, kill=kill_agent_processes if repo else missing_repo_dir)


if __name__ == '__main__': main()
