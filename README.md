# swesim

A pipeline that turns underspecified software issues into multi-turn, gradable
SWE simulations, packaged as [Harbor](https://harborframework.com) tasks, with a
simulated stakeholder the agent can talk to.

**Thesis.** SWE-bench, Terminal-Bench and DeepSWE grade one shot against a fully
specified instruction. Real issues arrive underspecified, and the discussion
thread is where the maintainer resolved what they wanted. SWE-bench Verified
filtered those issues out as noise. We treat them as the signal: the original
issue is the brief, the resolution is held by a simulated stakeholder grounded
in a provenance-tagged fact ledger, and the PR's tests are the contract.

**Status.** Design spec complete; implementation starting 2026-09-12. See
[`docs/superpowers/specs/2026-09-12-swe-simulator-design.md`](docs/superpowers/specs/2026-09-12-swe-simulator-design.md).

## Pipeline

```
mine -> judge -> spec -> compile -> validate -> register -> run -> score -> view
```

Every source (SWE-bench instances rejected from Verified for underspecification,
a GitHub repo plus PR, DeepSWE, and later others) produces one normalized
`ScenarioSpec` (`swesim/models.py`); every later stage consumes only that.

## Layout

```
swesim/            CLI and pipeline stages
swesim/sources/    one module per source producing ScenarioSpec
swesim/stakeholder/ hosted stakeholder service, review sandbox, shell shims
swesim/verifier/   grader, overlays, integrity signals, report parsing
swesim/templates/  Harbor task templates
swesim/dashboard/  run explainer and pipeline dashboard
data/annotations/  SWE-bench Verified human annotations and candidate pools
references/        DeepSWE task example and Harbor swebench adapter template
docs/              spec, grading and architecture documents
workspace/         local pipeline outputs (gitignored)
```

## Quickstart (target interface; stages land over the next day)

```bash
uv sync
uv run swesim mine --source swebench-annotated --pool A
uv run swesim judge && uv run swesim spec <candidate-id>
uv run swesim compile <scenario-id> && uv run swesim validate <scenario-id>
uv run swesim run <task-id> --agent mini-swe-agent --model anthropic/claude-sonnet-5 --condition interactive
uv run swesim score && uv run swesim view
```

## Attribution

Grading follows the DeepSWE v1.1 pattern (separate verifier, collected patch,
stripped git history). Nearest prior work on interaction: Ambig-SWE
(Vijayvargiya et al., 2026) and "Ask or Assume?" (Edwards and Schuster, 2026);
see the spec's prior-art table for what they did and did not do.
