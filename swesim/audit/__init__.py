"""Task-sense audit: does the task make sense, does the stakeholder disclose the right thing at the right time, do agents ask
the right questions (working model §7). Evaluator-only; its labels gate the leaderboard (interfaces §9.1).

Modules: ``rubric`` (the rubric as data, label models, mechanical labels), ``record`` (reading scenarios and trials from the
record), ``llm_audit`` (the reading pass on ``anthropic/claude-opus-5``), ``run`` (audit one scenario or trial, write
``audit.json`` idempotently), ``activities`` (Temporal activities on the control queue), ``report`` (aggregates over the
record), ``cli`` (evaluator commands).
"""
