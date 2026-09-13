"""Temporal orchestration for swesim (thin).

- `workflows`: PipelineWorkflow (mine -> judge -> draft -> approve -> compile -> validate ->
  register -> trials -> score/export), RunMatrixWorkflow (serial TrialWorkflow children with
  REJECT_DUPLICATE) and TrialWorkflow (launch a named systemd user unit once, then poll).
- `activities`: thin wrappers over the stage modules; payloads carry ids, paths, hashes and
  model ids only. Also the per-trial runner program (`python -m swesim.workflows.activities
  run-trial <request.json>`) that a trial unit executes.
- `worker`: one process on task queue `swesim`, launched once by systemd and polling.
- `client`: start pipeline/matrix workflows and send the approval signal.
"""
