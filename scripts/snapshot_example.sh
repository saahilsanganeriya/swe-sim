#!/bin/bash
# Snapshot one validated scenario and one or more completed trials from the ignored workspace into the tracked
# examples/ package. Copies exactly: scenario.json, assets/, validation.json, compile.json, approval.json, scores.json,
# the three compiled task directories as task__{full,hidden,interactive}, and per trial runs/<trial_id>/ with
# record.json, reward.json, report.json, transcript.json, episode.json, agent/trajectory.json and the verifier/ files
# (runner_stdout.txt and test-stdout.txt over 200 KB are skipped and listed under "skipped" in the manifest).
# Every copied file is text; on the way in, every SWESIM_SESSION_TOKEN value, Authorization / x-api-key header value,
# bearer token and known credential value (the trial's session token, *KEY*/*TOKEN*/*SECRET* values from the ignored
# env file and the environment) is replaced by <redacted>, and absolute host paths become package- or repo-relative.
# manifest.json records scenario_id, spec_hash, snapshot_at, a sha256 per copied file, the skipped files, the redaction
# and rewrite bookkeeping and a per-run summary. An independent grep then confirms no credential-like string remains.
# The script never overwrites an existing example and never prints a secret value.
#
# Usage: bash scripts/snapshot_example.sh <scenario_id> <trial_id> [<trial_id>...]
# Env:   SWESIM_WORKSPACE    workspace root (default: workspace)
#        SWESIM_EXAMPLES_DIR destination root (default: examples); the only way to write a second copy
#        SWESIM_ENV_FILE     ignored env file whose credential values are redacted if seen (default: ./.env)
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "$#" -lt 2 ]; then
  echo 'Usage: bash scripts/snapshot_example.sh <scenario_id> <trial_id> [<trial_id>...]' >&2
  exit 2
fi
DEST="${SWESIM_EXAMPLES_DIR:-examples}/$1"

uv run --frozen --offline python - "$@" <<'PYCODE'
import hashlib, json, os, re, shutil, sys, tempfile, tomllib
from datetime import datetime, timezone
from pathlib import Path
from dotenv import dotenv_values

REDACTED = '<redacted>'
CONDITIONS = ('full', 'hidden', 'interactive')
SKIPPABLE = {'runner_stdout.txt', 'test-stdout.txt'}
SKIP_OVER = 200 * 1024
EXCLUDED_NAMES = {'.DS_Store'}
EXCLUDED_DIRS = {'__pycache__'}
# A value is only redacted when it is a literal of credential length: shell/format references ($VAR, {var}, f"...",
# \" escapes) and empty strings are left alone so the shims, collector and compiled task.toml keep their meaning.
NOT_A_VALUE = r'(?!\$|\{|\\|<redacted>)'
VALUE = r'([^"\'\s,}&\\]{8,})'
RULES = [
    ('session-token', re.compile(r'(?i)(SWESIM_SESSION_TOKEN["\']?\s*[=:]\s*["\']?)' + NOT_A_VALUE + VALUE)),
    ('authorization-scheme', re.compile(r'(?i)(authorization["\']?\s*[:=]\s*["\']?\s*(?:bearer|basic|token)\s+)' + NOT_A_VALUE + VALUE)),
    ('authorization', re.compile(r'(?i)(authorization["\']?\s*[:=]\s*["\']?)(?!\$|\{|\\|<redacted>|(?:bearer|basic|token)\b)' + VALUE)),
    ('x-api-key', re.compile(r'(?i)(x-api-key["\']?\s*[:=]\s*["\']?)' + NOT_A_VALUE + VALUE)),
    ('bearer', re.compile(r'(?i)\b(bearer\s+)' + NOT_A_VALUE + r'([A-Za-z0-9._~+/=-]{16,})')),
]
CREDENTIAL = re.compile(r'(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}')


def fail(message):
    raise SystemExit('snapshot_example: ' + message)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


sid, *tids = sys.argv[1:]
if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{2,63}', sid):
    fail('invalid scenario id')
for tid in tids:
    if not re.fullmatch(r'[a-f0-9]{24}', tid):
        fail('invalid trial id ' + tid)
if len(set(tids)) != len(tids):
    fail('duplicate trial id')

root = Path.cwd().resolve()
ws = Path(os.environ.get('SWESIM_WORKSPACE', 'workspace')).resolve()
examples = Path(os.environ.get('SWESIM_EXAMPLES_DIR', 'examples'))
src = ws / 'scenarios' / sid
dst = examples / sid
if dst.exists():
    fail(str(dst) + ' exists; snapshots are never overwritten (remove it deliberately, or set SWESIM_EXAMPLES_DIR to a new root)')


def shown(p):
    p = Path(p)
    if not p.is_relative_to(root):
        return str(p)
    rel = p.relative_to(root).as_posix()
    return '<repo>' if rel == '.' else '<repo>/' + rel


def load(p):
    if not p.is_file():
        fail('missing ' + shown(p))
    return json.loads(p.read_text())


# Scenario-level inputs and their agreement.
compile_info = load(src / 'compile.json')
approval = load(src / 'approval.json')
validation = load(src / 'validation.json')
scenario = load(src / 'scenario.json')
scores = load(src / 'scores.json')
spec_hash = compile_info['spec_hash']
if approval.get('spec_hash') != spec_hash or validation.get('spec_hash') != spec_hash:
    fail('spec_hash disagrees between compile.json, approval.json and validation.json')
if validation.get('status') != 'valid':
    fail('scenario validation status is ' + str(validation.get('status')) + ', not valid')
if scenario.get('id') != sid:
    fail('scenario.json id does not match ' + sid)
if not any(p.is_file() for p in (src / 'assets').rglob('*')):
    fail('missing assets under ' + shown(src / 'assets'))
for condition in CONDITIONS:
    base = ws / 'tasks' / (sid + '__' + condition)
    if not (base / 'task.toml').is_file():
        fail('missing compiled condition ' + shown(base))
    meta = tomllib.loads((base / 'task.toml').read_text()).get('metadata', {})
    if meta.get('spec_hash') != spec_hash or meta.get('compiled_hash') != compile_info.get('compiled_hash'):
        fail('compiled task ' + condition + ' does not match compile.json (stale task directory?)')

# Trial-level inputs.
runs = {}
for tid in tids:
    tdir = ws / 'trials' / tid
    record = load(tdir / 'record.json')
    if record.get('scenario_id') != sid:
        fail('trial ' + tid + ' belongs to scenario ' + str(record.get('scenario_id')))
    if record.get('spec_hash') != spec_hash:
        fail('trial ' + tid + ' ran on spec ' + str(record.get('spec_hash')) + ', package spec is ' + spec_hash)
    if record.get('status') != 'completed':
        fail('trial ' + tid + ' status is ' + str(record.get('status')) + ', not completed')
    for name in ('reward.json', 'report.json', 'transcript.json'):
        if not (tdir / name).is_file():
            fail('missing ' + shown(tdir / name))
    export = ws / 'exports' / (tid + '.episode.json')
    if not export.is_file():
        fail('missing ' + shown(export) + ' (run: swesim export ' + sid + ')')
    harbor = Path(record['trial_dir'])
    if not harbor.exists():  # recorded on the VM (/opt/swesim/workspace/...); re-root onto this workspace (scripts/vm/pull.sh mirror)
        s_ = str(harbor); i_ = s_.find('/workspace/')
        if i_ >= 0:
            harbor = ws / s_[i_ + len('/workspace/'):]
    for rel in ('agent/trajectory.json', 'verifier/submission.diff'):
        if not (harbor / rel).is_file():
            fail('missing ' + shown(harbor / rel))
    verifier_reward = harbor / 'verifier' / 'reward.json'
    if verifier_reward.is_file() and json.loads(verifier_reward.read_text()) != json.loads((tdir / 'reward.json').read_text()):
        fail('trial ' + tid + ': verifier/reward.json and reward.json disagree')
    token = ''
    task_toml = tdir / 'task' / 'task.toml'
    if task_toml.is_file():
        token = tomllib.loads(task_toml.read_text()).get('environment', {}).get('env', {}).get('SWESIM_SESSION_TOKEN') or ''
    runs[tid] = dict(record=record, dir=tdir, harbor=harbor, export=export, token=token)
listed = {t for bucket in scores.values() for t in bucket.get('trial_ids', [])}
for tid in tids:
    if tid not in listed:
        fail('trial ' + tid + ' is not in scores.json (run: swesim score ' + sid + ')')

# Known credential values: never printed, replaced wherever they appear.
secrets = set()
env_file = os.environ.get('SWESIM_ENV_FILE', '.env')
if Path(env_file).is_file():
    for key, value in dotenv_values(env_file).items():
        if value and len(value) >= 8 and any(x in key.upper() for x in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD')):
            secrets.add(value)
for key, value in os.environ.items():
    if value and len(value) >= 16 and any(x in key.upper() for x in ('API_KEY', 'TOKEN', 'SECRET')):
        secrets.add(value)
for run in runs.values():
    if run['token']:
        secrets.add(run['token'])
secrets = sorted(secrets, key=len, reverse=True)

# Host paths become package-relative where the target is in the package, repo-relative otherwise; longest first.
rewrites = []
for condition in CONDITIONS:
    rewrites.append((str(ws / 'tasks' / (sid + '__' + condition)), 'task__' + condition))
for tid, run in runs.items():
    rewrites.append((str(run['harbor']), 'runs/' + tid))
    rewrites.append((str(run['export']), 'runs/' + tid + '/episode.json'))
    for name in ('record.json', 'reward.json', 'report.json', 'transcript.json'):
        rewrites.append((str(run['dir'] / name), 'runs/' + tid + '/' + name))
rewrites.append((str(src) + '/', ''))
rewrites.append((str(src), '.'))
rewrites.append((str(ws) + '/', 'workspace/'))
rewrites.append((str(root) + '/', ''))
rewrites.sort(key=lambda kv: len(kv[0]), reverse=True)

# The copy plan: (source, package-relative destination).
plan = []
for name in ('scenario.json', 'validation.json', 'compile.json', 'approval.json', 'scores.json'):
    plan.append((src / name, name))


def walk(base, prefix):
    for p in sorted(base.rglob('*')):
        if p.is_file() and p.name not in EXCLUDED_NAMES and not (EXCLUDED_DIRS & set(p.relative_to(base).parts[:-1])):
            plan.append((p, prefix + '/' + p.relative_to(base).as_posix()))


walk(src / 'assets', 'assets')
for condition in CONDITIONS:
    walk(ws / 'tasks' / (sid + '__' + condition), 'task__' + condition)
for tid, run in runs.items():
    for name in ('record.json', 'reward.json', 'report.json', 'transcript.json'):
        plan.append((run['dir'] / name, 'runs/' + tid + '/' + name))
    plan.append((run['export'], 'runs/' + tid + '/episode.json'))
    plan.append((run['harbor'] / 'agent' / 'trajectory.json', 'runs/' + tid + '/agent/trajectory.json'))
    for p in sorted((run['harbor'] / 'verifier').iterdir()):
        if p.is_file() and p.name not in EXCLUDED_NAMES:
            plan.append((p, 'runs/' + tid + '/verifier/' + p.name))
if len({rel for _, rel in plan}) != len(plan):
    fail('internal error: duplicate destination in copy plan')

examples.mkdir(parents=True, exist_ok=True)
tmp = Path(tempfile.mkdtemp(prefix='.' + sid + '.', dir=examples))
skipped, redacted_files, rule_hits, rewritten_files, files, total = [], [], {}, [], {}, 0
try:
    for source, rel in plan:
        size = source.stat().st_size
        if source.name in SKIPPABLE and size > SKIP_OVER:
            skipped.append({'path': rel, 'reason': 'larger than %d KB' % (SKIP_OVER // 1024), 'bytes': size,
                            'source_sha256': sha256(source.read_bytes())})
            continue
        try:
            text = source.read_bytes().decode('utf-8')
        except UnicodeDecodeError:
            fail(rel + ': binary content cannot be scanned for credentials; not copied')
        original = text
        for value in secrets:
            if value in text:
                text = text.replace(value, REDACTED)
                rule_hits['known-value'] = rule_hits.get('known-value', 0) + 1
        for name, pattern in RULES:
            text, n = pattern.subn(lambda m: m.group(1) + REDACTED, text)
            if n:
                rule_hits[name] = rule_hits.get(name, 0) + n
        if text != original:
            redacted_files.append(rel)
        before = text
        for frm, to in rewrites:
            if frm in text:
                text = text.replace(frm, to)
        if text != before:
            rewritten_files.append(rel)
        if CREDENTIAL.search(text):
            fail(rel + ': a credential-like value remains after redaction; nothing written')
        data = text.encode('utf-8')
        out = tmp / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        files[rel] = sha256(data)
        total += len(data)

    # Per-run summary with the integrity facts a reviewer will want to re-check.
    run_summary = {}
    for tid, run in runs.items():
        record, report = run['record'], json.loads((tmp / 'runs' / tid / 'report.json').read_text())
        transcript = json.loads((tmp / 'runs' / tid / 'transcript.json').read_text())
        recomputed = None
        if transcript.get('events') is not None:
            digest = sha256(json.dumps(transcript['events'], sort_keys=True, separators=(',', ':')).encode())
            recomputed = digest == transcript.get('transcript_hash')
        run_summary[tid] = {
            'condition': record.get('condition'), 'brief_profile': record.get('brief_profile'), 'model': record.get('model'),
            'purpose': record.get('purpose'), 'run_config_hash': record.get('run_config_hash'),
            'compiled_hash': record.get('compiled_hash'),
            'compiled_hash_matches_package': record.get('compiled_hash') == compile_info.get('compiled_hash'),
            'reward': (record.get('reward') or {}).get('reward'), 'success': (record.get('reward') or {}).get('success'),
            'outcome': report.get('outcome'), 'outcome_code': report.get('outcome_code'),
            'regression': report.get('regression'), 'integrity_signals': len(report.get('integrity_signals') or []),
            'transcript_frozen': bool(transcript.get('frozen')), 'transcript_hash': transcript.get('transcript_hash'),
            'transcript_hash_recomputed': recomputed,
            'transcript_binding_valid': report.get('transcript_binding_valid'),
            'transcript_modified_by_redaction': 'runs/' + tid + '/transcript.json' in redacted_files,
            'started_at': record.get('started_at'), 'finished_at': record.get('finished_at'),
        }
        if recomputed is False:
            fail('trial ' + tid + ': transcript hash does not recompute from the copied events')

    manifest = {
        'scenario_id': sid,
        'spec_hash': spec_hash,
        'snapshot_at': datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z'),
        'compiled_hash': compile_info.get('compiled_hash'),
        'image_digest': compile_info.get('image_digest'),
        'source': validation.get('source'),
        'source_ref': validation.get('source_ref'),
        'validation_status': validation.get('status'),
        'sanitized': True,
        'redaction': {'replacement': REDACTED, 'rules': [name for name, _ in RULES] + ['known-value'],
                      'hits': dict(sorted(rule_hits.items())), 'files_changed': sorted(redacted_files)},
        'path_rewrites': [{'from': shown(frm) if not frm.endswith('/') else shown(frm[:-1]) + '/', 'to': to}
                          for frm, to in rewrites],
        'path_rewrite_files': sorted(rewritten_files),
        'runs': run_summary,
        'skipped': skipped,
        'files': dict(sorted(files.items())),
    }
    (tmp / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    os.rename(tmp, dst)
except BaseException:
    shutil.rmtree(tmp, ignore_errors=True)
    raise

print('snapshot written: ' + str(dst))
print('  scenario %s  spec_hash %s  compiled_hash %s' % (sid, spec_hash[:16], str(compile_info.get('compiled_hash'))[:16]))
print('  files: %d copied, %d bytes (%.2f MB) plus manifest.json' % (len(files), total, total / 1e6))
print('  skipped: ' + (', '.join('%s (%d bytes)' % (s['path'], s['bytes']) for s in skipped) or 'none'))
print('  redacted: %d files, rule hits %s' % (len(redacted_files), json.dumps(dict(sorted(rule_hits.items()))) or '{}'))
print('  host paths rewritten in %d files' % len(rewritten_files))
for tid, s in run_summary.items():
    print('  run %s: %s/%s %s reward=%s outcome=%s transcript_bound=%s compiled_hash_matches_package=%s' % (
        tid, s['condition'], s['brief_profile'], s['model'], s['reward'], s['outcome'],
        s['transcript_binding_valid'], s['compiled_hash_matches_package']))
PYCODE

# Independent verification: no credential-like string, bearer token or literal session token remains anywhere.
PATTERN='(^|[^A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}|Bearer [A-Za-z0-9._~+/=-]{20,}|SWESIM_SESSION_TOKEN *[=:] *"[^"$<\\][^"]{7,}"'
set +e
hits=$(grep -r -l -E "$PATTERN" "$DEST")
rc=$?
set -e
if [ "$rc" -eq 0 ]; then
  echo "snapshot_example: credential-like strings remain; removing $DEST. Files:" >&2
  echo "$hits" >&2
  rm -rf "$DEST"
  exit 1
elif [ "$rc" -ne 1 ]; then
  echo "snapshot_example: grep failed with status $rc; removing $DEST" >&2
  rm -rf "$DEST"
  exit 1
fi
echo "  credential scan: clean ($(find "$DEST" -type f | wc -l | tr -d ' ') files grepped)"
