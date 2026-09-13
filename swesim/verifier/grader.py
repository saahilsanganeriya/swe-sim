"""Grader entrypoint: receipt -> extract -> diff -> apply -> overlay -> run -> reconcile -> reward/report.

Standard library only. Runs as a script inside the verifier container
(``swesim-python -I /tests/grader.py --config /tests/grader_config.json``), importing
``policy`` and ``parsers`` as siblings from ``/tests``; imports as ``swesim.verifier.grader``
for unit tests.

reward.json holds finite numbers only (Harbor rejects anything else); everything descriptive
goes to report.json. Outcomes are keyed on ``success``; ``reward`` is partial credit by
requirement group, and any PASS_TO_PASS failure forces reward 0.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, fields, replace
from pathlib import Path

if __package__:
    from . import parsers, policy
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import parsers  # type: ignore
    import policy  # type: ignore

OUTCOMES = {'valid-correct': 1, 'valid-incorrect': 2, 'agent-invalid-submission': 3, 'agent-runtime-failure': 4,
            'collection-failure': 5, 'simulator-failure': 6, 'evaluator-failure': 7}
PATH_FIELDS = ('artifacts_dir', 'pristine_dir', 'work_dir', 'verifier_out', 'target_dir', 'contract_patch')
LAUNCH_FAILURE = 'launch-failure: '   # _run_tests error prefix for failures that cannot be the submission's doing
MAX_RERUNS = 10
MAX_DIFF_FILE_BYTES = 5 * 1024 * 1024
STDOUT_KEEP = 1_000_000


@dataclass
class GradeConfig:
    artifacts_dir: Path
    pristine_dir: Path
    work_dir: Path
    expected_tests: dict          # nodeid -> "f2p" | "p2p"
    groups: list                  # [{id, weight, tests}]
    protected: list               # globs; DEFAULT_PROTECTED is always added
    runner: dict                  # Runner fields: kind, command, single_command, test_files, nodeid_map
    test_user: str | None
    verifier_out: Path
    test_timeout_sec: int = 1500
    target_dir: Path | None = None    # /testbed or /app: preserve editable-install paths
    contract_patch: Path | None = None


@dataclass
class GradeResult:
    reward: dict
    report: dict


def sha256_file(p):
    return policy._hash(p)


def _coerce(cfg):
    kw = {}
    for f in fields(GradeConfig):
        v = getattr(cfg, f.name)
        if f.name in PATH_FIELDS and v is not None and not isinstance(v, Path):
            v = Path(v)
        kw[f.name] = v
    return GradeConfig(**kw)


def _atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.' + path.name + '.')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _write(out_dir, reward, report):
    out_dir = Path(out_dir)
    # report first, so the presence of reward.json implies report.json exists
    _atomic_write(out_dir / 'report.json', json.dumps(report, indent=2, allow_nan=False, default=str))
    _atomic_write(out_dir / 'reward.json', json.dumps(reward, indent=2, allow_nan=False))
    return GradeResult(reward, report)


def _base_reward(outcome, **kw):
    r = dict(reward=0.0, success=0, f2p_passed=0, f2p_expected=0, p2p_passed=0, p2p_expected=0,
             groups_passed=0, groups_total=0, integrity_signal_count=0, outcome_code=OUTCOMES[outcome], patch_applied=0)
    r.update(kw)
    return {k: (float(v) if k == 'reward' else int(v)) for k, v in r.items()}


def _empty_report():
    return {'outcome': None, 'outcome_code': None, 'per_test': [], 'per_group': [],
            'patch_sha256': None, 'submission_sha256': None, 'transcript_hash': None, 'reviewed_diff_sha256': None,
            'dropped_paths': [], 'rejected_members': [], 'integrity_signals': []}


def _config_problem(cfg):
    """Problems independent of the submission; they are evaluator failures, never the agent's."""
    if not isinstance(cfg.expected_tests, dict) or not isinstance(cfg.groups, list) or not isinstance(cfg.runner, dict):
        return 'malformed config: expected_tests/groups/runner'
    bad = [t for t, k in cfg.expected_tests.items() if k not in ('f2p', 'p2p')]
    if bad:
        return 'expected_tests kinds must be f2p or p2p: %r' % bad[:3]
    mapping = cfg.runner.get('nodeid_map') or {}
    if not isinstance(mapping, dict):
        return 'runner.nodeid_map must be an object'
    actual = [mapping.get(t, t) for t in cfg.expected_tests]
    if len(set(actual)) != len(actual):
        return 'runner.nodeid_map is not a bijection onto collected ids'
    for t in actual:
        try:
            parsers.junit_key(t)
        except parsers.EvidenceError as e:
            return str(e)
    if not isinstance(cfg.runner.get('command'), str) or '{junit_path}' not in cfg.runner['command']:
        return 'runner.command must contain {junit_path}'
    for g in cfg.groups:
        if not isinstance(g, dict) or not g.get('tests') or not all(t in cfg.expected_tests for t in g['tests']):
            return 'group %r references tests outside the contract' % (g.get('id') if isinstance(g, dict) else g)
        w = g.get('weight')
        if isinstance(w, bool) or not isinstance(w, (int, float)) or not (0 < w <= 1):
            return 'group %r weight out of range' % g.get('id')
    if not cfg.expected_tests:
        return 'expected_tests is empty: nothing to grade'
    if not cfg.groups:
        return 'groups is empty: no requirement groups to credit'
    if not cfg.pristine_dir.is_dir():
        return 'pristine_dir missing: %s' % cfg.pristine_dir
    if cfg.target_dir is not None and cfg.target_dir.resolve() == cfg.pristine_dir.resolve():
        return 'target_dir must differ from pristine_dir (the base stays untouched)'
    return None


def _unquote_git_path(raw):
    """Undo git's C-style path quoting ("a/caf\\303\\251.py"); plain paths are returned unchanged."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        import codecs
        return codecs.escape_decode(raw[1:-1].encode('utf-8'))[0].decode('utf-8', 'replace')
    return raw.split('\t')[0].strip()


def _patch_paths(patch):
    """Files a contract patch touches; they are protected even without a matching glob."""
    if patch is None:
        return []
    try:
        text = Path(patch).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return []
    out = set()
    for line in text.splitlines():
        for marker in ('--- ', '+++ ', 'rename from ', 'rename to ', 'copy from ', 'copy to '):
            if not line.startswith(marker):
                continue
            p = _unquote_git_path(line[len(marker):])
            if marker in ('--- ', '+++ '):
                if p == '/dev/null' or '/' not in p:
                    continue
                p = p.split('/', 1)[1]   # git apply -p1 strips the a/ or b/ component
            if p:
                out.add(p)
    return sorted(out)


def _apply_contract_patch(patch, target):
    """Returns (True, '') on success, (None, 'empty') for an empty patch, (False, message) on failure."""
    try:
        text = Path(patch).read_bytes()
    except OSError as e:
        return False, str(e)
    if not text.strip():
        return None, 'empty'
    git = '/usr/bin/git' if os.path.exists('/usr/bin/git') else shutil.which('git')
    if not git:
        return False, 'git not found'
    # safe.directory=*: an in-place /testbed may be owned by a different uid than the grader
    proc = subprocess.run([git, '-c', 'core.hooksPath=/dev/null', '-c', 'safe.directory=*', 'apply', '--whitespace=nowarn', str(patch)],
                          cwd=str(target), capture_output=True, text=True, errors='replace')
    if proc.returncode:
        return False, (proc.stderr or proc.stdout)[-2000:]
    return True, ''


def _current_user():
    try:
        import pwd
        return pwd.getpwuid(os.geteuid()).pw_name
    except (ImportError, KeyError):
        return str(os.geteuid())


def _chown_tree(path, user):
    import pwd
    pw = pwd.getpwnam(user)
    uid, gid = pw.pw_uid, pw.pw_gid
    os.lchown(path, uid, gid)
    for dirpath, dirnames, filenames in os.walk(path):
        for name in dirnames + filenames:
            try:
                os.lchown(os.path.join(dirpath, name), uid, gid)
            except FileNotFoundError:
                pass
    return uid, gid


def _kill_group(p):
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _kill_user_processes(user):
    """Best-effort SIGKILL of every process owned by user via /proc (no procps needed). Linux only."""
    import pwd
    try:
        uid = pwd.getpwnam(user).pw_uid
    except KeyError:
        return 0
    proc = Path('/proc')
    if not proc.is_dir():
        return 0
    killed = 0
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / 'status').read_text()
            line = next(ln for ln in status.splitlines() if ln.startswith("Uid:"))
            if int(line.split()[1]) != uid:
                continue
            os.kill(int(entry.name), signal.SIGKILL)
            killed += 1
        except (OSError, StopIteration, ValueError, IndexError):
            continue
    return killed


def _run_tests(cfg, repo, expected):
    """Run cfg.runner['command'] in repo as the test user. Returns (statuses, rc, stdout, error).

    expected: exact nodeids the run must collect, or None to accept whatever was collected
    (the authoring probe). Evidence must reconcile completely or no statuses are returned.
    """
    work = Path(cfg.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(prefix='results-', dir=str(work)))   # fresh output paths for every run
    d.chmod(0o755)
    if cfg.test_user:
        try:
            _chown_tree(str(d), cfg.test_user)
        except (KeyError, OSError) as e:
            return {}, -1, '', LAUNCH_FAILURE + 'test user %r: %s' % (cfg.test_user, e)
    junit, ids = d / 'junit.xml', d / 'nodeids.json'
    cmd = cfg.runner['command'].replace('{junit_path}', shlex.quote(str(junit)))
    # Minimal environment: no inherited PYTHON*/PYTEST_* options reach the test interpreter.
    env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(d), 'LANG': 'C.UTF-8', 'COLUMNS': '200',
           'SWESIM_NODEIDS': str(ids)}
    argv = ['/bin/bash', '--noprofile', '--norc', '-c', cmd]
    if cfg.test_user:
        runuser = shutil.which('runuser', path='/usr/sbin:/usr/bin:/sbin:/bin')
        if not runuser:
            return {}, -1, '', LAUNCH_FAILURE + 'runuser not found'
        argv = [runuser, '-u', cfg.test_user, '--', *argv]
    try:
        p = subprocess.Popen(argv, cwd=str(repo), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                             start_new_session=True, encoding='utf-8', errors='replace')
    except OSError as e:
        return {}, -1, '', LAUNCH_FAILURE + str(e)
    try:
        out, _ = p.communicate(timeout=cfg.test_timeout_sec)
    except subprocess.TimeoutExpired:
        _kill_group(p)
        if cfg.test_user:
            _kill_user_processes(cfg.test_user)
        try:
            out, _ = p.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()
            out = ''
        return {}, -9, out or '', 'timeout'
    finally:
        if cfg.test_user:
            _kill_user_processes(cfg.test_user)   # nothing started by one run may survive into the next
    rc = p.returncode
    try:
        collected = json.loads(ids.read_text(encoding='utf-8'))
        if not isinstance(collected, list) or not all(isinstance(x, str) for x in collected):
            raise parsers.EvidenceError('malformed collection manifest')
        if len(collected) != len(set(collected)):
            raise parsers.EvidenceError('duplicate collection ids')
        if expected is not None and set(collected) != set(expected):
            raise parsers.EvidenceError('collection differs from manifest')
        statuses = parsers.reconcile(out, junit, collected, rc)
    except (OSError, ValueError) as e:
        return {}, rc, out, '%s: %s' % (type(e).__name__, e)
    return statuses, rc, out, None


def _diagnostic_reruns(cfg, target, actual, report):
    """Per-test reruns of FAIL_TO_PASS ids after an evidence failure. Diagnostics only; never credit."""
    single = cfg.runner.get('single_command') or ''
    if not single:
        return
    deadline = time.monotonic() + cfg.test_timeout_sec
    n = 0
    for t, kind in cfg.expected_tests.items():
        if kind != 'f2p':
            continue
        remaining = deadline - time.monotonic()
        if n >= MAX_RERUNS or remaining < 5:
            report['reruns_truncated'] = True
            break
        nodeid = actual[t]
        sub = replace(cfg, runner=dict(cfg.runner, command=single.replace('{nodeid}', shlex.quote(nodeid))),
                      test_timeout_sec=max(5, int(remaining)))
        st, sr, so, se = _run_tests(sub, target, [nodeid])
        report['reruns'].append({'id': t, 'statuses': st, 'exit_code': sr, 'error': se, 'tail': so[-1000:]})
        n += 1


def _write_submission_diff(cfg, target, applied_paths, report):
    lines = []
    for rel in applied_paths:
        a, b = cfg.pristine_dir / rel, target / rel
        if (a.is_file() and a.stat().st_size > MAX_DIFF_FILE_BYTES) or (b.is_file() and b.stat().st_size > MAX_DIFF_FILE_BYTES):
            report.setdefault('large_changes', []).append(rel)
            continue
        try:
            old = a.read_text(encoding='utf-8').splitlines(keepends=True) if a.is_file() else []
            new = b.read_text(encoding='utf-8').splitlines(keepends=True) if b.is_file() else []
        except UnicodeDecodeError:
            report.setdefault('binary_changes', []).append(rel)
            continue
        diff = difflib.unified_diff(old, new, fromfile='a/' + rel if a.is_file() else '/dev/null',
                                    tofile='b/' + rel if b.is_file() else '/dev/null')
        for ln in diff:
            if ln.endswith('\n'):
                lines.append(ln)
            else:   # only a file's last line lacks a newline; mark it the way diff(1) does
                lines.append(ln + '\n')
                lines.append('\\ No newline at end of file\n')
    _atomic_write(cfg.verifier_out / 'submission.diff', ''.join(lines))


def grade(cfg):
    t0 = time.monotonic()
    cfg = _coerce(cfg)
    report = _empty_report()
    expected_ok = isinstance(cfg.expected_tests, dict)
    f2p_expected = sum(k == 'f2p' for k in cfg.expected_tests.values()) if expected_ok else 0
    p2p_expected = sum(k == 'p2p' for k in cfg.expected_tests.values()) if expected_ok else 0
    groups_total = len(cfg.groups) if isinstance(cfg.groups, list) else 0

    def finish(outcome, **kw):
        report['outcome'] = outcome
        report['outcome_code'] = OUTCOMES[outcome]
        report['elapsed_sec'] = round(time.monotonic() - t0, 3)
        reward = _base_reward(outcome, f2p_expected=f2p_expected, p2p_expected=p2p_expected, groups_total=groups_total,
                              integrity_signal_count=len(report['integrity_signals']), **kw)
        return _write(cfg.verifier_out, reward, report)

    # 0. Configuration problems are independent of the submission.
    problem = _config_problem(cfg)
    if problem:
        report['error'] = problem
        return finish('evaluator-failure')
    cfg.work_dir.mkdir(parents=True, exist_ok=True)

    # 1. Receipt from the root-owned collector. /logs/artifacts is never a trust anchor.
    try:
        receipt = json.loads((cfg.artifacts_dir / 'collect.status').read_text(encoding='utf-8'))
        if not isinstance(receipt, dict):
            raise ValueError('receipt is not a JSON object')
    except (OSError, ValueError) as e:
        report['error'] = 'receipt: %s' % e
        return finish('collection-failure')
    report['receipt'] = receipt
    report['transcript_hash'] = receipt.get('transcript_hash')
    report['reviewed_diff_sha256'] = receipt.get('reviewed_diff_sha256')
    report['freeze_status'] = receipt.get('freeze_status')
    if receipt.get('freeze_status') == 'failed':
        report['integrity_signals'].append({'kind': 'freeze-failed'})   # simulator validity is the worker's call
    if receipt.get('status') in ('oversized', 'invalid-tree'):
        return finish('agent-invalid-submission')
    tar = cfg.artifacts_dir / 'app.tar'
    if receipt.get('status') != 'ok' or receipt.get('rc') != 0 or receipt.get('user') != 'root' or not tar.is_file():
        report['error'] = 'receipt status/rc/user or archive missing'
        return finish('collection-failure')
    if receipt.get('sha256') != sha256_file(tar):
        report['error'] = 'archive digest differs from receipt'
        return finish('collection-failure')

    # 2. Extract, diff against the untouched base, apply allowed source changes to the runtime tree.
    tree = cfg.work_dir / 'tree'
    if cfg.target_dir is not None:
        target, in_place = cfg.target_dir, True
        if not target.is_dir():
            report['error'] = 'target_dir missing: %s' % target
            return finish('evaluator-failure')
    else:
        target, in_place = cfg.work_dir / 'repo', False
    report['target_mode'] = 'in-place' if in_place else 'copy'
    protected = list(cfg.protected) + policy.DEFAULT_PROTECTED + _patch_paths(cfg.contract_patch)
    # InvalidArchive is the submission's doing (3); any other OSError here (ENOSPC, EACCES) is ours (7).
    try:
        if tree.exists():
            shutil.rmtree(tree)
        ext = policy.safe_extract(tar, tree)
    except policy.InvalidArchive as e:
        report['invalid_tree'] = '%s: %s' % (type(e).__name__, e)
        return finish('agent-invalid-submission')
    except OSError as e:
        report['error'] = 'extracting submission: %s' % e
        return finish('evaluator-failure')
    if not in_place:
        try:
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(cfg.pristine_dir, target, symlinks=True)
        except OSError as e:
            report['error'] = 'preparing target: %s' % e
            return finish('evaluator-failure')
    try:
        cs = policy.change_set(cfg.pristine_dir, tree)
        report['submission_sha256'] = policy.change_hash(cfg.pristine_dir, tree, cs)
        applied = policy.apply_change_set(target, tree, cs, protected)
    except policy.InvalidArchive as e:
        report['invalid_tree'] = '%s: %s' % (type(e).__name__, e)
        return finish('agent-invalid-submission')
    except (OSError, ValueError) as e:
        report['error'] = 'applying submission: %s' % e
        return finish('evaluator-failure')
    report['rejected_members'] = [list(x) for x in ext.rejected]
    report['dropped_paths'] = applied.dropped
    report['refused_paths'] = [list(x) for x in applied.refused]
    report['applied_paths'] = applied.applied
    report['change_counts'] = {'added': len(cs.added), 'modified': len(cs.modified), 'deleted': len(cs.deleted)}
    refused = {p for p, _ in applied.refused}
    report['integrity_signals'] += [{'kind': 'rejected-archive-member', 'path': n, 'reason': r} for n, r in ext.rejected]
    report['integrity_signals'] += [{'kind': 'symlink-traversal', 'path': p, 'reason': r} for p, r in applied.refused]
    report['integrity_signals'] += [{'kind': 'protected-path-edit', 'path': x} for x in applied.dropped if x not in refused]
    kept = set(applied.applied)
    applied_cs = policy.ChangeSet(added=[p for p in cs.added if p in kept], modified=[p for p in cs.modified if p in kept],
                                  deleted=[p for p in cs.deleted if p in kept])
    report['patch_sha256'] = policy.change_hash(cfg.pristine_dir, target, applied_cs)
    patch_applied = int(bool(applied.applied))

    # 3. Submission diff against the untouched base, BEFORE the hidden-test overlay.
    _write_submission_diff(cfg, target, applied.applied, report)

    # 4. Overlay hidden assertions.
    report['contract_patch_applied'] = 0
    if cfg.contract_patch is not None:
        ok, msg = _apply_contract_patch(cfg.contract_patch, target)
        if ok is False:
            report['error'] = 'contract patch: ' + msg
            return finish('evaluator-failure', patch_applied=patch_applied)
        report['contract_patch_applied'] = int(bool(ok))

    # 5. The test user owns the runtime tree; pristine stays read-only.
    if cfg.test_user:
        try:
            _chown_tree(str(target), cfg.test_user)
        except (KeyError, OSError) as e:
            report['error'] = 'chown %s: %s' % (cfg.test_user, e)
            return finish('evaluator-failure', patch_applied=patch_applied)

    # 6. Run and reconcile. The runner must collect exactly the contract ids.
    mapping = cfg.runner.get('nodeid_map') or {}
    actual = {t: mapping.get(t, t) for t in cfg.expected_tests}
    report['runner_command'] = cfg.runner['command']
    report['tests_ran_as'] = cfg.test_user or _current_user()
    if not cfg.test_user:   # tests share the grader's identity; validation must see this
        report['integrity_signals'].append({'kind': 'tests-ran-as-grader', 'user': report['tests_ran_as']})
    statuses, rc, out, error = _run_tests(cfg, target, list(actual.values()))
    report.update(runner_exit_code=rc, runner_tail=out[-4000:], runner_error=error)
    _atomic_write(cfg.verifier_out / 'runner_stdout.txt', out[-STDOUT_KEEP:])
    if error:
        if error.startswith(LAUNCH_FAILURE):
            report['error'] = error
            return finish('evaluator-failure', patch_applied=patch_applied)
        # Inconsistent aggregate evidence never becomes credit. Record what was expected as unknown,
        # write the outcome now, then add per-test reruns as diagnostics within a bounded budget.
        report['per_test'] = [dict(id=t, kind=k, expected='passed', observed='unknown') for t, k in cfg.expected_tests.items()]
        report['per_group'] = [dict(id=g['id'], weight=g['weight'], passed=False) for g in cfg.groups]
        report['reruns'] = []
        finish('agent-runtime-failure', patch_applied=patch_applied)
        _diagnostic_reruns(cfg, target, actual, report)
        return finish('agent-runtime-failure', patch_applied=patch_applied)

    # 7. Credit: success is whole-contract correctness; reward is partial by group; any P2P failure zeroes it.
    observed = {t: statuses.get(actual[t], 'missing') for t in cfg.expected_tests}
    report['per_test'] = [dict(id=t, kind=k, expected='passed', observed=observed[t]) for t, k in cfg.expected_tests.items()]
    f2p_passed = sum(observed[t] == 'passed' for t, k in cfg.expected_tests.items() if k == 'f2p')
    p2p_passed = sum(observed[t] == 'passed' for t, k in cfg.expected_tests.items() if k == 'p2p')
    regression = any(observed[t] != 'passed' for t, k in cfg.expected_tests.items() if k == 'p2p')
    reward, groups_passed = 0.0, 0
    for g in cfg.groups:
        ok = all(observed[t] == 'passed' for t in g['tests'])
        groups_passed += ok
        report['per_group'].append(dict(id=g['id'], weight=g['weight'], passed=bool(ok)))
        reward += float(g['weight']) * ok
    reward = 0.0 if regression else min(1.0, round(reward, 8))
    success = int(bool(observed) and all(v == 'passed' for v in observed.values()))
    outcome = 'valid-correct' if success else 'valid-incorrect'
    if not applied.applied:
        # A no-op still ran the tests for base evidence; it cannot pass a validated FAIL_TO_PASS contract.
        outcome, success, reward = 'agent-invalid-submission', 0, 0.0
    report['regression'] = bool(regression)
    return finish(outcome, reward=reward, success=success, f2p_passed=f2p_passed, p2p_passed=p2p_passed,
                  groups_passed=groups_passed, patch_applied=patch_applied)


def load_config(path):
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
    known = {f.name for f in fields(GradeConfig)}
    kw = {k: v for k, v in raw.items() if k in known}
    for k in PATH_FIELDS:
        if kw.get(k) is not None:
            kw[k] = Path(kw[k])
    return GradeConfig(**kw)


def main(argv=None):
    ap = argparse.ArgumentParser(description='swesim offline grader')
    ap.add_argument('--config', required=True)
    a = ap.parse_args(argv)
    out_dir = Path('/logs/verifier')
    try:
        try:
            out_dir = Path(json.loads(Path(a.config).read_text(encoding='utf-8')).get('verifier_out') or out_dir)
        except Exception:
            pass
        res = grade(load_config(a.config))
        print('swesim grader: outcome=%s reward=%s success=%s' % (res.report['outcome'], res.reward['reward'], res.reward['success']))
        return 0
    except Exception as e:  # anything unexpected is an evaluator failure, never credit and never the agent's fault
        report = _empty_report()
        report.update(outcome='evaluator-failure', outcome_code=OUTCOMES['evaluator-failure'], error=repr(e),
                      traceback=traceback.format_exc()[-4000:])
        try:
            _write(out_dir, _base_reward('evaluator-failure'), report)
            print('swesim grader: evaluator-failure: %r' % (e,))
            return 0
        except Exception:
            traceback.print_exc()
            return 1


if __name__ == '__main__':
    sys.exit(main())
