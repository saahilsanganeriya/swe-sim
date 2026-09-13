"""Temporal orchestration for swesim (thin).

- `workflows`        : CreateTasksWorkflow -> CandidateWorkflow children; RunTasksWorkflow -> TrialWorkflow children;
                       PipelineWorkflow and RunMatrixWorkflow (the original compositions). `act()` routes every activity
                       to its queue with its retry class.
- `activities`       : thin wrappers over the stage modules; payloads carry ids, paths, hashes and model ids only. Also
                       the per-trial runner program (`python -m swesim.workflows.activities run-trial <request.json>`).
- `launchers`        : the trial launcher protocol (launch once, read evidence, decide) with systemd, Cloud Run Job
                       and subprocess implementations.
- `queues`, `retries`, `search_attributes`: the four task queues and the activity -> queue map, the retry classes,
                       the six Keyword search attributes.
- `worker`           : one Worker per queue in `Settings.task_queues`, activities partitioned by queue.
- `client`           : start workflows, signal approvals, list and cancel; connection settings from `Settings`.
"""
