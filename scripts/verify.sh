#!/bin/bash
# Cumulative, offline acceptance. Never launches cloud jobs or servers.
set -euo pipefail
cd "$(dirname "$0")/.."
N=${1:-10}
case "$N" in 1|2|3|4|5|6|7|8|9|10) ;; *) echo 'Usage: bash scripts/verify.sh <1-10>' >&2; exit 2;; esac
FILES=(tests/test_models.py)
if [ "$N" -ge 2 ]; then FILES+=(tests/test_policy.py tests/test_parsers.py tests/test_grader.py); fi
if [ "$N" -ge 3 ]; then FILES+=(tests/test_compile.py tests/test_patch_agent.py); fi
if [ "$N" -ge 4 ]; then FILES+=(tests/test_swebench_source.py tests/test_tamper.py tests/test_validate.py); fi
if [ "$N" -ge 5 ]; then FILES+=(tests/test_session.py tests/test_service.py tests/test_run.py); fi
if [ "$N" -ge 6 ]; then FILES+=(tests/test_github_repo.py); fi
if [ "$N" -ge 7 ]; then FILES+=(tests/test_workflows.py tests/test_score.py); fi
if [ "$N" -ge 9 ]; then FILES+=(tests/test_dashboard.py); fi
uv run --frozen --offline python -m pytest -q "${FILES[@]}"
uv run --frozen --offline python - "$N" <<'PY'
import ast, hashlib, json, math, os, re, subprocess, sys, tomllib
from pathlib import Path
N=int(sys.argv[1]); ROOT=Path.cwd(); errors=[]
def check(ok, message):
    print(('  [ok] ' if ok else '  [FAIL] ')+message)
    if not ok: errors.append(message)
def read(path):
    p=Path(path)
    if not p.is_file(): raise ValueError('missing '+str(p))
    return json.loads(p.read_text())
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
WSROOT=Path('workspace').resolve()
def local(path):
    # Recorded paths are absolute on the machine that produced them (the VM writes /opt/swesim/workspace/...). When the
    # recorded path is absent here, re-root it onto this checkout's workspace (scripts/vm/pull.sh mirrors the record).
    p=Path(path)
    if p.exists(): return p
    s=str(p); i=s.find('/workspace/')
    if i>=0:
        q=WSROOT/s[i+len('/workspace/'):]
        if q.exists(): return q
    return p
def tests_by_id(report):
    rows=report.get('per_test',[])
    if len({r['id'] for r in rows})!=len(rows): raise ValueError('duplicate test evidence')
    return {r['id']:r['observed'] for r in rows}
def reward_ok(r):
    return all(type(v) in (int,float) and math.isfinite(v) for v in r.values()) and 0<=r.get('reward',-1)<=1 and r.get('success') in (0,1)
def evidence(path,callback,label):
    try: callback(Path(path)); check(True,label)
    except Exception as e: check(False,label+': '+str(e))

def check_validation(path):
    from swesim.models import ScenarioSpec
    from swesim.stages.validate import evaluate_controls
    spec=ScenarioSpec.model_validate_json((path.parent/'scenario.json').read_text())
    v=read(path); comp=read(path.parent/'compile.json'); approval=read(path.parent/'approval.json')
    assert v['status']=='valid' and v['spec_hash']==spec.spec_hash==approval['spec_hash']==comp['spec_hash']
    assert v['compiled_hash']==comp['compiled_hash'] and v['environment_provider']=='modal'
    for asset in spec.assets.values():
        p=(path.parent/asset.path).resolve()
        assert p.is_relative_to(path.parent.resolve()) and p.stat().st_size==asset.bytes and sha(p)==asset.sha256
    expected=set(spec.contract.fail_to_pass)|set(spec.contract.pass_to_pass)
    assert set(v['expected_base'])==set(v['expected_gold'])==expected
    assert all(v['expected_base'][t] in {'failed','error'} for t in spec.contract.fail_to_pass)
    assert all(v['expected_base'][t]=='passed' for t in spec.contract.pass_to_pass)
    assert all(v['expected_gold'][t]=='passed' for t in expected)
    ok,why=evaluate_controls(v['controls'],spec.controls.model_dump()); assert ok,why
    assert v['probe']['raw'] and not v['probe']['gold_hits']
    for name, td in v['trial_dirs'].items():
        td=local(td); raw=read(td/'result.json'); r=read(td/'verifier/reward.json'); report=read(td/'verifier/report.json')
        assert reward_ok(r) and r['outcome_code'] in {1,2,3,4}
        assert not raw.get('exception_info'),name+' did not execute cleanly'
        if name=='oracle': assert r['success']==r['reward']==1 and tests_by_id(report)==v['expected_gold']
        if name=='nop': assert r['reward']==0 and tests_by_id(report)==v['expected_base']
        # The job config, rather than folder spelling, establishes the environment provider.
        cfg=read(td/'config.json'); assert cfg.get('environment',{}).get('type')=='modal',name+' not Modal'
    for c in ('full','hidden','interactive'):
        task=local(comp['task_dirs'][c]); t=tomllib.loads((task/'task.toml').read_text())
        assert t['environment']['network_mode']=='allowlist' and t['agent']['network_mode']=='allowlist'
        assert t['verifier']['network_mode']=='no-network' and t['verifier']['environment_mode']=='separate'
        assert t['agent']['user']=='agent' and t['verifier']['collect'][0]['user']=='root'
        assert '/var/swesim-collect/app.tar' in t['artifacts'] and '/var/swesim-collect/collect.status' in t['artifacts']
    return spec,v

valid=[]
if N>=2:
    allowed=set(sys.stdlib_module_names)|{'policy','parsers'}
    for p in Path('swesim/verifier').glob('*.py'):
        imports=[]
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node,ast.Import): imports += [x.name.split('.')[0] for x in node.names]
            elif isinstance(node,ast.ImportFrom) and node.level==0 and node.module: imports.append(node.module.split('.')[0])
        extra=set(imports)-allowed
        if p.name=='run_pytest.py': extra.discard('pytest')
        check(not extra,'stdlib controller imports: '+str(p)+(' '+str(extra) if extra else ''))
if N>=4:
    for path in Path('workspace/scenarios').glob('*/validation.json'):
        if read(path).get('status')!='valid': continue
        try:
            spec,v=check_validation(path); valid.append((spec,v,path.parent)); check(True,'validated '+spec.id)
        except Exception as e: check(False,'invalid validation evidence '+str(path)+': '+str(e))
    check(any(s.source.value=='swebench-annotated' for s,_,_ in valid),'SWE-bench task passes executed controls')

records=[]
for path in Path('workspace/trials').glob('*/record.json'):
    try: records.append((read(path),path.parent))
    except Exception as e: check(False,str(e))
def check_interactive(r,d):
    from swesim.models import RunConfig,trial_id
    rc=RunConfig.model_validate(read(d/'run_config.json'))
    assert rc.agent=='mini-swe-agent' and rc.condition=='interactive'
    assert r['trial_id']==trial_id(r['spec_hash'],r['condition'],rc.run_config_hash,r['attempt_index'])
    assert r['run_config_hash']==rc.run_config_hash and r['compiled_hash']==rc.compiled_hash and r['model']==rc.model
    t=read(d/'transcript.json'); rep=read(d/'report.json'); reward=read(d/'reward.json')
    assert reward_ok(reward) and reward['outcome_code'] in {1,2,3,4}
    h=hashlib.sha256(json.dumps(t['events'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
    assert t.get('frozen') and t['transcript_hash']==rep.get('transcript_hash')==h and rep.get('transcript_binding_valid') is True
    qs=[e for e in t['events'] if e.get('kind')=='message' and e.get('role')=='user' and e.get('exchange')=='ask']
    replies=[e for e in t['events'] if e.get('kind')=='message' and e.get('role')=='assistant']
    assert qs and any(a.get('request_id')==q.get('request_id') and a.get('content') for a in replies for q in qs)
    trajectory=read(local(r['trial_dir'])/'agent/trajectory.json'); assert trajectory.get('steps')
    assert r['status']=='completed'
if N>=5:
    matches=[]
    for r,d in records:
        if r.get('condition')!='interactive':continue
        try: check_interactive(r,d); matches.append(r)
        except Exception: pass  # Failed experiments may remain; at least one complete demonstration is required.
    check(bool(matches),'real interactive episode with paired exchange and bound transcript')
if N>=6:
    github=[(s,v,d) for s,v,d in valid if s.source.value=='github-repo']
    check(bool(github),'validated task from GitHub source')
    candidates=[]; rejects=list(Path('workspace/candidates').glob('github-repo--*.rejects.json'))
    for p in Path('workspace/candidates').glob('github-repo--*.jsonl'):
        if '.judged.' not in p.name: candidates += [json.loads(l) for l in p.read_text().splitlines() if l]
    check(bool(rejects),'GitHub rejection histogram exists')
    for s,v,d in github:
        source=[c for c in candidates if c['instance_id']==s.source_ref]
        check(any(c.get('discovery',{}).get('repo_only') is True for c in source),'repo-only discovery lineage for '+s.id)
        check(any(r.get('spec_hash')==s.spec_hash and r.get('status')=='completed' and reward_ok(r.get('reward',{})) for r,_ in records),'GitHub task executed: '+s.id)
if N>=7:
    from swesim.stages.score import summarize, load_trial_records
    from swesim.paths import Workspace
    reg=read('registry.json')
    for spec,validation,sdir in valid:
        # Registry contract (register.py): one entry per scenario id carrying the current version and spec hash.
        key=spec.id; entry=reg.get('scenarios',{}).get(key,{})
        check(entry.get('spec_hash')==spec.spec_hash and entry.get('version')==spec.version and entry.get('validation_status')=='valid','registry version binding '+key)
        evidence(sdir/'scores.json',lambda p: read(p) or (_ for _ in ()).throw(ValueError('empty score table')),'nonempty scores '+key)
        rs=[r for r,_ in records if r.get('scenario_id')==spec.id]
        scored=read(sdir/'scores.json')
        if scored:
            # Same loader as `swesim score` (merges the sibling run_config.json/spec.json), so the table is reproducible.
            k=next(iter(scored.values()))['requested_k']; loaded=load_trial_records(Workspace('workspace'),spec.id)
            check(scored==summarize(loaded,k),'scores recompute from attempts '+key)
        for condition in ('full','hidden','interactive'):
            check(any(r.get('condition')==condition and r.get('spec_hash')==spec.spec_hash for r in rs),'recorded '+condition+' attempt '+key)
        exports=list(Path('workspace/exports').glob('*.episode.json'))
        check(any(read(e).get('lineage',{}).get('spec_hash')==spec.spec_hash for e in exports),'exported episode '+key)
    check(Path('tests/integration/test_temporal.py').is_file(),'explicit Temporal integration test exists')
    paired=False
    for spec,_,_ in valid:
        profiles={r.get('brief_profile') for r,_ in records if r.get('spec_hash')==spec.spec_hash and r.get('condition')=='hidden' and r.get('status')=='completed'}
        if {'natural','withheld'}<=profiles: paired=True
    check(paired,'hidden runs on both natural and withheld profiles for one scenario')
if N>=8:
    def ops(p):
        d=read(p); assert d['provider']=='gce'
        off=d['laptop_off']; restart=d['worker_restart']
        assert off['passed'] is True and restart['passed'] is True and restart['launch_count']==1
        for k in ('history_path','record_path','export_path'): assert Path(off[k]).is_file()
        for k in ('journal_path','record_path'): assert Path(restart[k]).is_file()
        assert read(restart['record_path'])['trial_id']==restart['trial_id']
        assert Path(restart['journal_path']).stat().st_size>0
        assert read(off['record_path'])['status']=='completed'
        assert off['workflow_id'] and restart['unit']=='swesim-trial-'+restart['trial_id']
    evidence('workspace/evidence/ops.json',ops,'laptop-off and worker-restart evidence')
    for name in ('create.sh','setup.sh','tunnel.sh','swesim-temporal.service','swesim-worker.service','swesim-dashboard.service'):
        check(Path('scripts/vm',name).is_file(),'VM file '+name)
if N>=10:
    tracked=set(subprocess.check_output(['git','ls-files','-z']).decode().split('\0'))
    for f in ('README.md','docs/grading.md','docs/architecture.md','docs/ops.md','examples/README.md','registry.json'):
        check(f in tracked and Path(f).is_file() and Path(f).stat().st_size>100,'tracked nonempty '+f)
    manifests=list(Path('examples').glob('*/manifest.json')); check(bool(manifests),'tracked example manifest')
    for m in manifests:
        d=read(m)
        for rel,digest in d['files'].items():
            p=m.parent/rel
            check(p.as_posix() in tracked and p.is_file() and sha(p)==digest,'example file '+p.as_posix())
        for c in ('full','hidden','interactive'):
            check((m.parent/('task__'+c)/'task.toml').is_file(),'example condition '+c)
        check(bool(list((m.parent/'runs').glob('*/verifier/submission.diff'))),'example submitted patch')
    # A bounded scan of tracked text, plus exact known credentials when available; no secrets are printed.
    known=[v for k,v in os.environ.items() if len(v)>=16 and any(t in k for t in ('API_KEY','TOKEN','SECRET'))]
    secret_pattern=re.compile(rb'(?:sk-(?:ant|proj)-|gh[pousr]_|github_pat_)[A-Za-z0-9_-]{12,}')
    for f in tracked:
        p=Path(f)
        if not p.is_file() or p.stat().st_size>10*1024*1024:continue
        data=p.read_bytes()
        check(not secret_pattern.search(data) and not any(v.encode() in data for v in known),'credential scan '+f)
if errors: raise SystemExit('VERIFY FAILED: '+str(len(errors))+' checks')
print('VERIFY OK through M'+str(N))
PY
if [ "$N" -eq 10 ]; then uv run --frozen --offline python -m pytest -q tests --ignore=tests/integration; fi
