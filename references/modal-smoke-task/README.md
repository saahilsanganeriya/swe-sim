# Verified Modal smoke task (2026-09-12)

This task directory is the recipe every compiled swesim task builds on. Each
element was verified by a Harbor 0.23 run on Modal (job dirs were under
/tmp/swesim/jobs; results summarized here):

- `[agent] user = "agent"`: agent runs unprivileged (uid 1001); it cannot remove
  a root-owned non-empty directory under agent-owned /app.
- `[environment] network_mode = "allowlist"` (strict: model API + stakeholder
  host) is the baseline and governs setup and collection; `[agent]` uses the
  same strict list. api.anthropic.com reachable (401), github.com and
  example.com blocked. A Modal web endpoint hostname in the allowlist is
  reachable from the sandbox (HTTP 200) while github stays blocked.
- Agent toolchain is pre-baked (system-wide uv, `uv python install 3.12`,
  `uv tool install mini-swe-agent --with litellm --with orjson --with fastapi`
  as user agent, then `chown -R agent:agent /home/agent`) with
  `UV_OFFLINE=1 UV_NO_PROGRESS=1 UV_CACHE_DIR=/tmp/uv-cache` in
  `[environment.env]`, so Harbor's mini-swe-agent setup succeeds offline.
  Without pre-baking, setup fails with NetworkConnectionError even with
  package mirrors allowlisted (apt over HTTP is blocked on Modal).
- Collection: `[[verifier.collect]]` runs root-owned `tar` of /app (excluding
  .git and .venv) to `/logs/artifacts/app.tar` plus a `collect.status`
  receipt. `/logs/artifacts` transfers reliably to the separate verifier.
  Harbor's directory artifacts (`artifacts=[{source="/app", exclude=[...]}]`)
  are NOT used: the host-side extraction aborts on absolute symlinks and
  silently truncates the tree.
- Separate verifier (`environment_mode = "separate"`, tests/Dockerfile) runs
  as root, offline; extracts app.tar member-by-member with
  `tarfile.extract(filter="data")` (absolute symlinks rejected); writes a
  numeric-only `reward.json` (multi-key accepted by Harbor).
- mini-swe-agent 2.4.6 + anthropic/claude-haiku-4-5-20251001 completed the
  task end to end: reward 1, ATIF trajectory at agent/trajectory.json, 1m29s.

Run (API keys exported in the shell first):

    harbor run -p references/modal-smoke-task -a mini-swe-agent -m anthropic/claude-haiku-4-5-20251001 -e modal --jobs-dir jobs --yes
    harbor run -p references/modal-smoke-task -a oracle -e modal --jobs-dir jobs --yes

`stakeholder_probe_app.py` is the Modal web endpoint used for the host
reachability probe (`modal deploy stakeholder_probe_app.py`).
