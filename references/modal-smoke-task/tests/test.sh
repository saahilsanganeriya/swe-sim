#!/bin/bash
mkdir -p /logs/verifier
{
echo "== verifier whoami: $(id -un)"
echo "== /logs/artifacts:"; ls -la /logs/artifacts 2>&1
echo "== collect.status:"; cat /logs/artifacts/collect.status 2>&1
echo "== tar listing (types):"; python3 - <<'PY'
import tarfile
t = tarfile.open('/logs/artifacts/app.tar')
for m in t.getmembers():
    kind = 'dir' if m.isdir() else 'sym' if m.issym() else 'file' if m.isfile() else 'other'
    print(f"  {kind:5} uid={m.uid:<5} {m.name}" + (f" -> {m.linkname}" if m.issym() else ""))
PY
echo "== safe extract with filter=data (symlinks rejected?):"; python3 - <<'PY'
import tarfile, os
t = tarfile.open('/logs/artifacts/app.tar'); os.makedirs('/work', exist_ok=True)
ok=0; bad=[]
for m in t.getmembers():
    try:
        t.extract(m, '/work', filter='data'); ok+=1
    except Exception as e:
        bad.append((m.name, type(e).__name__))
print("extracted", ok, "rejected", bad)
PY
echo "== /work tree:"; find /work -mindepth 1 -printf "%y %u %p\n" | sort
echo "== verifier egress: $(curl -s -o /dev/null -m 5 -w '%{http_code}' https://example.com || echo blocked)"
} | tee /logs/verifier/probe.txt
if [ "$(cat /work/hello.txt 2>/dev/null)" = "hello" ]; then r=1; else r=0; fi
printf '{"reward": %d, "f2p_passed": %d, "f2p_expected": 1, "outcome_code": 1}\n' "$r" "$r" > /logs/verifier/reward.json
