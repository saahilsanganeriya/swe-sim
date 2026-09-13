"""Explainer (/explainer): the whole system on one page for an operator or reviewer.

Contract
- Content is data, not markup: ``SECTIONS`` (ten, in the order the brief fixes) hold ``Card`` rows with a title, one short
  paragraph, an optional ASCII diagram (rendered in a ``<pre class='code'>``), decision rows (what → why → cost if wrong),
  the sources each card is written from (repository paths; ``tests/test_dashboard_explainer.py`` checks they exist) and
  where to see the mechanism in the console. ``DECISIONS`` is the dated decisions log (brief §1 table + ledger rulings).
- Nothing here reads the record or the request: the page is identical for every identity and carries no evaluator
  material (no facts, redactions, judge rationales, hidden assets or spec hashes).
- Numbers appear only when the cited document states them; every rendered string passes through ``render.h``; layout uses
  the existing classes of ``static/app.css`` and the tokens of ``static/tokens.css`` (no colour literals, no inline styles).
- Diagrams are box-drawing ASCII, one idea each, at most ``DIAGRAM_MAX_COLUMNS`` wide so they never wrap in the card.
"""
from __future__ import annotations

from dataclasses import dataclass

from swesim.dashboard.render import Column, h, table

DIAGRAM_MAX_COLUMNS = 100
DIAGRAM_MAX_LINES = 24


def _flow(raw: str, col: int) -> str:
    """Align the branch arrows of a flow diagram: on a line containing ``⇢`` the text before it is padded with ``─`` so
    the text after it starts at column ``col``; ``⇥`` pads with spaces instead (a continuation line under the arrow)."""
    out = []
    for line in raw.strip(chr(10)).splitlines():
        if "⇥" in line:  # continuation: keep the left text, pad with spaces
            left, right = line.split("⇥", 1)
            left = left.rstrip()
            if len(left) >= col:
                raise ValueError(f"continuation too long for column {col}: {left!r}")
            out.append(left + " " * (col - len(left)) + right.strip())
            continue
        if "⇢" not in line:
            out.append(line)
            continue
        left, right = line.split("⇢", 1)
        left, right = left.rstrip(), right.strip()
        if not left.strip():
            out.append(" " * col + right)
            continue
        fill = col - len(left) - 2
        if fill < 1:
            raise ValueError(f"flow line too long for column {col}: {left!r}")
        out.append(f"{left} {'─' * fill} {right}")
    return chr(10) + chr(10).join(out) + chr(10)


# ----------------------------------------------------------------------------- content model

@dataclass(frozen=True)
class Source:
    """A document this card is written from. ``tracked=False`` only for the operator ledger, which git ignores."""
    path: str
    where: str
    tracked: bool = True


@dataclass(frozen=True)
class Console:
    """Where to see the mechanism in the console: a route pattern, what to look for, and a link that resolves."""
    page: str
    look_for: str
    href: str | None = None

    @property
    def link(self) -> str:
        return self.href or self.page


@dataclass(frozen=True)
class Decision:
    what: str
    why: str
    cost: str
    date: str = ""
    source: Source | None = None


@dataclass(frozen=True)
class Card:
    title: str
    text: str
    sources: tuple[Source, ...]
    console: Console
    diagram: str | None = None
    decisions: tuple[Decision, ...] = ()


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    lead: str
    cards: tuple[Card, ...]


# ----------------------------------------------------------------------------- sources (one object per document/section)

ARCH = "docs/architecture.md"
GRADING = "docs/grading.md"
OPS = "docs/ops.md"
BRIEF = "docs/superpowers/specs/2026-09-13-hosted-pipeline-brief.md"
INTERFACES = "docs/superpowers/specs/2026-09-13-interfaces.md"
CONSOLE_SPEC = "docs/superpowers/specs/2026-09-13-console-design.md"
CLAUDE_MD = "CLAUDE.md"
MODELS = "swesim/models.py"
GRADER = "swesim/verifier/grader.py"
POLICY = "swesim/verifier/policy.py"
JUDGE = "swesim/stages/judge.py"
DRAFT = "swesim/stages/draft_spec.py"
APPROVE = "swesim/stages/approve.py"
COMPILE = "swesim/stages/compile.py"
VALIDATE = "swesim/stages/validate.py"
DIFFICULTY = "swesim/stages/difficulty.py"
FRONTIER = "swesim/stages/frontier.py"
SCORE = "swesim/stages/score.py"
EXPORT = "swesim/stages/export.py"
RUN = "swesim/stages/run.py"
PROMPTS = "swesim/stakeholder/prompts.py"
SESSION = "swesim/stakeholder/session.py"
VIEW = "swesim/stakeholder/view.py"
GITHUB = "swesim/sources/github_repo.py"
RUBRIC = "swesim/audit/rubric.py"
LLM_AUDIT = "swesim/audit/llm_audit.py"
WORKFLOWS = "swesim/workflows/workflows.py"
CLOUDRUN = "swesim/workflows/launchers/cloudrun.py"
LAYOUT = "swesim/storage/layout.py"
LEDGER = Source(".superpowers/sdd/2026-09-13-hosted-pipeline/progress.md", "Ruling lines (operator ledger, not tracked in git)", tracked=False)


def S(path: str, where: str) -> Source:
    return Source(path, where)


# ----------------------------------------------------------------------------- 1. what swesim measures

ARMS = """
 original issue (natural brief)
        │
        ├────────────────────────────▶ FULL         natural brief + every stakeholder-visible fact
        │                                            nothing is missing; there is no one to ask
  withhold(): drop the spans a
  reporter plausibly omits; each
  removal = a ledger fact + a
  withholding operation
        │
        ▼
  withheld brief ───────────────────▶ HIDDEN       the same brief, no stakeholder
        │                                            lower bound: guess, or read the repository
        └───────────────────────────▶ INTERACTIVE  the same brief + ask-user / confirm-behavior
                                                     the treatment: what asking recovers

  full − hidden          what the withheld information is worth to this model
  interactive − hidden   how much of it the model recovers by asking the right questions
"""

TRIAL_IDENTITY = """
 trial_id = hash( spec_hash , condition , run_config_hash , attempt_index )
                      │           │             │                  │
                      │           │             │                  └ repeat index within one group
                      │           │             └ purpose · model · build · profile · scaffold
                      │           └ full | hidden | interactive
                      └ the frozen ScenarioSpec; a new version is a new hash, never pooled

 pass@k groups:  spec_hash | condition | brief_profile | run_config_hash
                 calibration, comparison, screen and demonstration never share a group
"""

SECTION_1 = Section("measures", "What swesim measures", (
    "swesim turns a merged pull request into a task whose brief is deliberately missing something a reporter would plausibly leave out, "
    "then runs the same model on three versions of that task. The measurement is the gap between the arms, not any one pass rate."), (
    Card("Three arms, one gap",
         "Full gives the natural issue plus every stakeholder-visible fact, so nothing is missing. Hidden gives the withheld brief and no one to ask. "
         "Interactive gives the same withheld brief plus a simulated reporter the agent can question. Hidden and interactive share the selected brief "
         "profile, so the only difference between them is the stakeholder; asking changes what the agent knows, never the tests or the reward weights.",
         (S(ARCH, "Conditions and stakeholder boundary"), S(CLAUDE_MD, "Contracts: brief profiles"), S(GRADING, "Reward and outcomes")),
         Console("/leaderboard", "the 'Pass rate by condition' chart and the per-scenario matrix; /trends splits accuracy, deltas and cost by condition and tier"),
         ARMS,
         (Decision("Hidden and interactive share the selected brief profile; full uses natural plus stakeholder-visible facts.",
                   "Only one thing may differ between the arms being compared, or the gap measures the brief rather than the asking.",
                   "A treatment effect that is really a brief-wording effect."),)),
    Card("What one trial is",
         "A trial is one attempt of one model on one condition of one frozen spec. Its id binds the spec hash, the condition, the effective run-configuration "
         "hash (purpose, actual model, compiled build, brief profile, scaffold identity) and the attempt index, and the frozen spec and configuration are saved "
         "next to the result. Calibration, comparison, screen and demonstration purposes are separate groups that are never pooled.",
         (S(ARCH, "Trial identity and recovery"), S(SCORE, "module docstring: grouping and denominators"), S(GRADING, "Metrics and grouping")),
         Console("/trials", "each row carries condition, purpose, brief profile, model and the workflow that launched it; click through to the episode",
                 "/trials"),
         TRIAL_IDENTITY,
         (Decision("Outcomes 1–4 count in n for pass@k and pass^k; only outcome 1 is a success; infrastructure outcomes 5–7 are excluded from n.",
                   "An agent-caused runtime failure is the agent's result; a harness failure is not evidence about the agent.",
                   "Pass rates would drift with infrastructure reliability instead of model behaviour.", "2026-09-12", S(SCORE, "Denominators")),)),
    Card("What is claimed today",
         "The website is a verified surface over preserved records; new discovery and experiments are deferred by user direction. The preserved record holds "
         "50 trial records, 19 of them on the current registered version-2 specs, all laptop/VM-era. No treatment effect or hosted model comparison is "
         "claimed from them: no cell has three attempts, and one scenario's hidden test was audited as unfair.",
         (S(GRADING, "Preserved inventory — 2026-09-13"), S(ARCH, "Acceptance boundary"), S(OPS, "deferred checks")),
         Console("/leaderboard", "the 'Gate' table at the bottom: every scenario with its validation, difficulty and task-sense checks and why it is on or off the board")),
))

# ----------------------------------------------------------------------------- 2. hosted architecture

TOPOLOGY = """
 operator (browser, IAP login)                                 swesim CLI (laptop, API key)
           │                                                               │
           ▼                                                               ▼
 ┌────────────────────┐          ┌─────────────────────────────────────────────────────────────┐
 │ console            │  start · │ Temporal Cloud   namespace swesim.abcde                     │
 │ Cloud Run service  │ signal ·▶│ CreateTasksWorkflow ▸ one CandidateWorkflow per candidate   │
 │ behind IAP, read-  │◀ query · │ RunTasksWorkflow    ▸ one TrialWorkflow per attempt         │
 │ only bucket mount  │  status  │ four queues · retry classes · patched markers · search attrs │
 └─────────┬──────────┘          └──────┬────────────────┬──────────────┬──────────────┬───────┘
           │ reads    four task queues  │                │              │              │
           ▼                            ▼                ▼              ▼              ▼
 ┌────────────────────┐ upserts  ┌──────────────┐ ┌─────────────┐ ┌───────────┐ ┌─────────────┐
 │ Cloud SQL Postgres │◀─────────│ swesim-      │ │ swesim-     │ │ swesim-   │ │ swesim-     │
 │ rebuildable index  │          │ control      │ │ authoring   │ │ build     │ │ trials      │
 └────────────────────┘          │ index, costs │ │ LLM stages, │ │ probe,    │ │ launch,     │
                                 │ spend, audit │ │ discovery   │ │ compile,  │ │ poll,       │
                                 └──────┬───────┘ └──────┬──────┘ │ validate  │ │ cancel      │
                                        │ files          │        └─────┬─────┘ └──────┬──────┘
                                        ▼                ▼              ▼              │ creates
 ┌──────────────────────────────────────────────────────────────────────────┐   ┌──────▼───────────┐
 │ versioned GCS bucket mounted at /workspace   —   the record of truth     │   │ Job per trial    │
 │ scenarios/ trials/ tasks/ candidates/ probes/ jobs/ exports/             │◀──│ swesim-trial-<id>│
 │ calibration/ costs/ evidence/ registry.json                              │   │ Harbor ──▶ Modal │
 └──────────────────────────────────────────────────────────────────────────┘   └──────────────────┘
"""

TRIAL_RUNTIME = """
 TrialWorkflow (swesim-trials queue)
   │ launch_once: launch.json first, then create Job swesim-trial-<tid>; AlreadyExists = launched
   ▼
 Cloud Run Job ── run-trial …/trials/<tid>/request.json ── Harbor 0.23 on Modal, max-retries 0
   ▼
 ┌────────────────────────────────┐  HTTP + session token  ┌────────────────────────────────┐
 │ agent container  (user agent)  │ ◀────────────────────▶ │ stakeholder service  (Modal)   │
 │ mini-swe-agent + solver model  │  ask-user              │ one session per trial          │
 │ egress: provider API host only │  confirm-behavior      │ sees briefs, visible facts,    │
 │ repo at base commit, history   │  request-review*       │ requirements, persona, checks  │
 │ stripped                       │  *diff-only mode only  │ never oracle, hidden tests,    │
 └───────────────┬────────────────┘                        │ author-only facts              │
                 │ agent finishes or times out             └────────────────────────────────┘
                 ▼
 root collector  [[verifier.collect]]  cleared env, kills agent processes, bounds + archives the
                 tree, freezes the session in finally, writes a receipt + base-manifest digest
                 ▼
 ┌────────────────────────────────┐
 │ verifier container  (offline)  │  same base manifest required · diff vs untouched base · apply
 │ trusted launcher, user tester  │  allowed source changes · overlay hidden tests · pytest -I
 └───────────────┬────────────────┘
                 ▼
 reward.json {success, reward, outcome_code}  report.json  transcript.json  costs.json  record.json
   └ await_trial (poll) ─▶ score (pass@k) ─▶ export (episode.json) ─▶ index ─▶ screen, audit
"""

SECTION_2 = Section("architecture", "Hosted architecture", (
    "Every stage is a Temporal activity on one of four Cloud Run worker pools; every artifact is a file in a versioned bucket at a path only "
    "swesim/storage/layout.py knows; Postgres is a rebuildable projection of those files; each trial runs in its own Cloud Run Job so it outlives the workers."), (
    Card("What runs where",
         "The console (Cloud Run service behind Identity-Aware Proxy, read-only bucket mount) and the CLI both talk to Temporal Cloud namespace swesim.abcde. "
         "Four worker pools poll four task queues: swesim-authoring for LLM and discovery activities, swesim-build for probe, compile and control validation "
         "on Modal, swesim-trials for launching and polling trial Jobs, swesim-control for indexing, cost and audit hooks. All hosted workloads run one "
         "digest-pinned image built by Cloud Build; Secret Manager supplies secrets at runtime; Terraform supplies everything else.",
         (S(ARCH, "Hosted components"), S(BRIEF, "§2.1–§2.4 target architecture"), S(LAYOUT, "record layout")),
         Console("/workflows", "every workflow with its queue-level progress and a deep link into the Temporal UI; /health shows the pollers on each queue"),
         TOPOLOGY,
         (Decision("Cloud Run (worker pool per queue, Job per trial, console service), not GKE.",
                   "GKE adds a cluster, RBAC, kustomize and External Secrets for no capability the pipeline needs; the launcher contract is runtime-neutral.",
                   "A later GKE module if Cloud Run limits bite; the queue split and launcher protocol carry over.", "2026-09-13 07:50 UTC", S(BRIEF, "§1 Runtime")),
          Decision("GCS is the record of truth; Cloud SQL Postgres is only the query index.",
                   "Files stay authoritative and rebuildable; tables exist so the console never globs the bucket.",
                   "An index that drifts from the files, which `swesim index rebuild` repairs.", "2026-09-13", S(BRIEF, "§1 Database")),
          Decision("Cloud SQL has a public IP with no authorized networks and is reached only through Cloud Run's IAM-authenticated proxy socket.",
                   "Saves the VPC and private-services setup on the night of the build.",
                   "An instance with a public address that is still unreachable without IAM and a password.", "2026-09-13", LEDGER))),
    Card("One trial's runtime",
         "The trials pool writes launch.json and then creates the Job named after the trial id; AlreadyExists means an earlier start reached the API, so the "
         "trial counts as launched and nothing runs twice. Inside the Job, Harbor runs the agent sandbox on Modal with a strict egress allowlist and the "
         "stakeholder reachable over HTTP with a per-session token. When the agent stops, a root-owned collector archives the tree and freezes the session; "
         "a separate offline verifier grades the archive. Polling is retried; launching never is.",
         (S(CLOUDRUN, "module docstring: launch-once contract"), S(RUN, "module docstring"), S(COMPILE, "module docstring: images and hooks"),
          S(ARCH, "Build and grading trust")),
         Console("/trial/<id>", "the episode timeline: agent steps, stakeholder exchanges, the verifier row and the harness row, plus every file under Files",
                 "/trials"),
         TRIAL_RUNTIME,
         (Decision("launch.json is written before the launch call; an unknown runtime state is reconciled, never relaunched.",
                   "Workflow ids alone do not give exactly-once side effects; a lost acknowledgement must not start a second solver run.",
                   "An attempt recorded as unresolved that a human must look at, instead of a silently doubled trial.", "2026-09-13", S(ARCH, "Trial identity and recovery")),)),
    Card("Temporal design rules",
         "Two operator workflows fan out one child per unit of work: CreateTasksWorkflow → CandidateWorkflow per candidate, RunTasksWorkflow → TrialWorkflow per "
         "trial, with explicit id-reuse policies. Activities name their queue and retry class: transient provider, Modal and GitHub errors retry with backoff; "
         "gate rejections and schema failures are non-retryable. Every change to a workflow's command sequence ships behind a workflow.patched marker until no "
         "pre-change execution remains; six swesim_* search attributes are set on every start and status change; long fan-outs continue-as-new every 100 children; "
         "payloads carry paths and ids, never bodies.",
         (S(BRIEF, "§2.8 Temporal design"), S(WORKFLOWS, "module docstring and PATCH_* markers"), S(ARCH, "Trial identity and recovery")),
         Console("/workflows/<id>", "Parameters, Progress and Children of a run; the Temporal UI link opens the event history (never duplicated here)",
                 "/workflows"),
         None,
         (Decision("Deploy while a CreateTasksWorkflow was open, with OVERRIDE_OPEN_EXECUTIONS=1.",
                   "Every command-sequence change since the running image sat behind workflow.patched markers and the open workflow's remaining commands were unchanged in shape.",
                   "One non-determinism failure on that workflow, which would be re-run; the override is recorded in evidence/cloud.", "2026-09-13 13:05 PDT", LEDGER),)),
))

# ----------------------------------------------------------------------------- 3. creating a task

DISCOVERY = _flow("""
 swesim create --source https://github.com/<org>/<repo> --pr-kind bugfix|feature
   │
   ▼  discover_activity — GitHub only; gh outage ⇒ retry the attempt, never a reject
 listed PRs ─▶ merged, after --since? ⇢ no ─▶ not-merged · before-since · not-found
      │
      ▼
 linked issue (Fixes #n) older than the PR? ⇢ no ─▶ no-linked-issue · issue-not-before-pr
      │ ⇥ linked-target-is-pr · linked-target-missing
      ▼
 changes tests AND source files? ⇢ no ─▶ no-test-changes · no-source-changes
      │
      ▼
 band: source diff lines and thread size ⇢ out ─▶ diff-too-small · diff-too-large
      │ ⇥ thin-thread · feature-source-file-band
      │ ⇥ feature-label-missing
      │   bugfix   20–600 source lines, ≥ 4 comments
      │   feature  200–1,500 lines, ≥ 8 comments, 3–30 source files, a feature-like label
      ▼
 retained ─▶ one CandidateWorkflow per PR  (id create:<scenario_id>)
             every terminal decision leaves a receipt in the candidates table
""", col=48)

PIPELINE = _flow("""
 judge      LLM scores the gaps; code-owned accept() ⇢ not kept ─▶ rejected
   ▼
 probe      base and gold run on Modal → F2P and P2P lists ⇢ not reproducible ─▶ rejected
   ▼
 draft      the lossy spec (§4), one correction round ⇢ invalid ScenarioSpec ─▶ rejected
   ▼
 approve    eight code-owned gates, or hold for review ⇢ fails ─▶ re-draft once, then reject
   ▼
 compile    three Harbor tasks: full · hidden · interactive ⇢ asset or manifest mismatch ─▶ error
   ▼
 validate   seven executed controls on Modal (§6) ⇢ wrong reward ─▶ invalid
   ▼
 register   advisory lock → registry.json + scenarios row ⇢ then the screen (§8) and the audit
            every rejection records its gate and reasons → candidate:<gate> in the reject histogram
""", col=62)

GATES = """
 gate                            rejects the draft when …
 ──────────────────────────────  ─────────────────────────────────────────────────────────────────
 schema                          not a strict ScenarioSpec: unknown field, bad id, weights ≠ 1 …
 assets                          a declared asset's bytes or sha256 differ from the file on disk
 judge-accept                    accept() is false: no consequential, non-derivable, resolved gap
 on-request-fact-linked-to-f2p   no on-request fact links a FAIL_TO_PASS test: nothing to ask about
 requirements-grounded           a requirement has no visible fact and no verbatim human statement
 withholding-leak                a removed excerpt is still readable in the withheld brief
 withheld-reconstructs           natural − recorded excerpts ≠ withheld brief (unrecorded change)
 controls                        missing oracle, plausible-wrong or incorrect-exploit control

 hold mode: same pre-checks, then a hash-bound reviewer decision from the console's Review queue
 drafts are never edited by hand to pass a gate: re-draft once, or take the next candidate
"""

SECTION_3 = Section("creation", "Creating a task from a repository URL", (
    "The primary objective: a repository link in, registered tasks out, with every lost pull request accounted for by the gate that lost it. "
    "Diff size is a discovery filter, never a measure of difficulty; difficulty is measured afterwards (§8)."), (
    Card("Discovery: which pull requests survive",
         "Discovery is GitHub-only and records a decision for every listed pull request: not merged, before the window, no linked issue, an issue newer than "
         "the PR, no test or source change, outside the band, or a thin thread. The bugfix band defaults to 20–600 changed source lines and at least four "
         "comments; the feature band needs 200–1,500 lines, at least eight comments, 3–30 source files and a feature-like label. Docs, changelogs and lockfiles "
         "never count toward the band. A gh outage retries the attempt instead of counting PRs as rejected.",
         (S(GITHUB, "discover() and reject_reason(); FEATURE_BAND"), S(ARCH, "Creation and evidence"), S(BRIEF, "§1 Primary objective")),
         Console("/candidates", "the 'Source funnels' chart per repository and band, the reject histogram (discovery:<reason> vs candidate:<gate>) and 'Every PR decision'"),
         DISCOVERY,
         (Decision("Creating tasks from new repositories given as links is the main goal; the 109 SWE-bench instances are the calibration set, not the product.",
                   "A benchmark that only re-labels SWE-bench measures nothing new; a repository funnel scales and tells you where PRs are lost.",
                   "Fewer tasks by morning than mining a curated pool would give.", "2026-09-13 08:20 UTC", S(BRIEF, "§1 Primary objective")),
          Decision("Diff size and thread size are discovery filters, never measured difficulty.",
                   "Line counts select for a kind of change; whether a task is hard is decided later by the screen on a real model.",
                   "Some hard small fixes are never listed; the band is tuned from the reject histogram.", "2026-09-13", S(CLAUDE_MD, "Diff size is a filter")))),
    Card("From candidate to registered scenario",
         "Each retained PR runs one CandidateWorkflow: the judge decides whether a real question is hidden in it (§7), the probe measures the test contract on "
         "Modal, the drafter writes the lossy spec (§4) with one correction round, eight gates approve or reject it, the compiler renders the three conditions, "
         "validation executes the controls (§6) and only a valid scenario is registered. Every terminal decision, including infrastructure errors, leaves a "
         "receipt with its gate and reasons, so the Candidates page can account for every PR.",
         (S(WORKFLOWS, "CandidateWorkflow and candidate_receipt"), S(ARCH, "Creation and evidence"), S(BRIEF, "§2.8 Temporal design")),
         Console("/candidates", "'Every candidate' with stage, gate and reasons; 'Creation funnel' on Home counts mined → judged → … → registered"),
         PIPELINE,
         (Decision("Discovery and probing are separate activities; every control validation is its own activity.",
                   "One infrastructure failure then retries one control (minutes) instead of the whole 10–25 minute batch, and the console shows per-control progress.",
                   "More, smaller Temporal activities to version.", "2026-09-13", S(BRIEF, "§2.8 item 3")),)),
    Card("Probe: the contract is measured, not copied",
         "Before drafting, the candidate's environment is built at the base commit and the PR's tests are run before and after the gold patch, on Modal, as the "
         "grading identity tester, collecting exact test ids in definition order. FAIL_TO_PASS is what the fix makes pass; PASS_TO_PASS is what must keep passing. "
         "A probe's job name carries the hash of its inputs; cached evidence is reused only when that binding and the tester identity match, so root-run or "
         "unbound legacy evidence is measured again.",
         (S(ARCH, "Creation and evidence: probe binding"), S(GRADING, "Controls and authoring gates: probe binding"), S(GITHUB, "probe_binding, GRADING_USER")),
         Console("/scenario/<id>", "the header rows 'repository @ base commit' and 'contract' (fail-to-pass · pass-to-pass · protected paths); probe "
                 "evidence is browsable under /artifacts?path=probes", "/scenarios")),
    Card("Approve: eight code-owned gates",
         "Unattended runs are approved by code, recorded as approved_by: auto-validators with review_needed: true. Hold mode routes the draft to the console's "
         "Review queue for a decision bound to the draft's hash. Either way the gates are the same list and a failing gate triggers at most one re-draft.",
         (S(APPROVE, "AUTO_GATES and withheld_reconstruction_failures"), S(CLAUDE_MD, "Contracts: approval"), S(GRADING, "Controls and authoring gates")),
         Console("/review", "drafts awaiting a decision with their pre-check results; /scenario/<id> shows the recorded approval and gates"),
         GATES,
         (Decision("Approval is unattended by default; a human decision exists (hold mode) but is not used overnight.",
                   "No step may wait for a person during the run; the gates are the same checks a reviewer would apply, and the record says which mode decided.",
                   "A gate that a human would have caught passes; the review_needed flag marks every such scenario for post-hoc review.", "2026-09-13", S(CLAUDE_MD, "Contracts: approval")),)),
    Card("Compile and validate: three tasks, seven executed controls",
         "The approved spec compiles into three Harbor 0.23 task directories (full, hidden, interactive) that share one Dockerfile asset, a pre-baked "
         "mini-swe-agent toolchain, the ask-user / confirm-behavior / request-review shims and a separate offline verifier image. The build hash covers the "
         "compiler inputs, the render configuration and the manifest of every rendered file. Validation then executes nop, oracle, the plausible-wrong patch, "
         "three tamper patches and the reachability probe on Modal; a scenario is valid only when every control earns exactly its expected outcome and no gold "
         "object or gold patch line is reachable from inside the agent image.",
         (S(COMPILE, "module docstring; CONDITIONS, SHIM_FILES, timeouts"), S(VALIDATE, "evaluate_controls"), S(BRIEF, "§7 Grading integrity: seven executed controls"),
          S(GRADING, "Build, collector and verifier")),
         Console("/scenario/<id>", "'Controls and validation': one row per control with its expected and observed reward, each linked to validation.json",
                 "/scenarios")),
))

# ----------------------------------------------------------------------------- 4. the lossy spec

SPEC_TREE = """
 scenario.json   ScenarioSpec — strict (unknown fields rejected); spec_hash = canonical hash
 ├─ environment    repo_url · base_commit · gold_commit · image or Dockerfile asset · test_command
 ├─ briefs         natural  = the original issue, verbatim             provenance original-issue
 │                 withheld = natural minus the recorded excerpts      provenance withheld
 ├─ operations     one withholding per removed span → its target fact holds the verbatim excerpt
 ├─ ledger (facts) id · text · kind · provenance · source_excerpt + url | authored_rationale · tests
 │     kind        behavior · interface · constraint · reproduction · context
 │     provenance  original-issue · issue-thread · pr-thread · pr-review · annotator-note · authored
 │     discovery   brief                  already said in the brief
 │                 on-request             only when a question targets it; linked to the F2P it pins
 │                 volunteered-at-review  may be offered when the agent asks to confirm a behavior
 │                 repo-derivable         the agent can find it in the code; the reporter says so
 │                 author-only            never disclosed; cannot link tests or ground a requirement
 ├─ requirements   id · text · fact_id (a visible fact) or a verbatim human statement · tests
 ├─ persona        role reporter | maintainer | product-owner · summary · tone · disclosure_style
 ├─ contract       fail_to_pass · pass_to_pass · test_assets (sha256) · protected_paths · runner
 ├─ checks         user-level reproducers, disjoint from the contract tests
 ├─ controls       oracle · plausible_wrong[] · alternative_correct[] · tamper[]
 │                 tamper cls: incorrect-exploit | benign-edit
 ├─ groups         partition of fail_to_pass; weights in (0, 1] summing to 1 → partial reward (§6)
 └─ difficulty     annotated · judged · measured — labels only; never edited to pass a gate
"""

WITHHOLDING = """
 natural brief (the original issue)                  withheld brief
 ┌────────────────────────────────────┐                ┌────────────────────────────────────┐
 │ Loader fails on an empty file      │  title kept    │ Loader fails on an empty file      │
 │ Steps: run `pkg load` on an empty  │ ─────────────▶ │ Steps: run `pkg load` on an empty  │
 │ file.                              │                │ file.                              │
 │ ▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒ │  removed span  │                                    │
 │ ▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒ │  (a traceback) │                                    │
 │ Expected: an empty result, not a   │                │ Expected: an empty result, not a   │
 │ crash.                             │                │ crash.                             │
 └────────────────────────────────────┘                └────────────────────────────────────┘
        │ for each removed span                                       ▲
        ▼                                                             │ gate withheld-reconstructs:
 fact       withheld-001   kind reproduction   discovery on-request   │ reconstruct_withheld(
            source_excerpt = the removed text, verbatim               │ natural, excerpts) == brief
            tests = the FAIL_TO_PASS ids that pin what it tells       │ character for character
 operation  {type: withholding, target: withheld-001, rationale}  ────┘
"""

SECTION_4 = Section("spec", "The lossy spec", (
    "The drafter does not invent a task; it records what the thread already decided and marks what the reporter would plausibly not have said. "
    "Everything the stakeholder may say, and when, is data in scenario.json, so it can be gated, audited and diffed between versions."), (
    Card("scenario.json, field by field",
         "The natural brief is the original issue verbatim; the withheld brief is the natural text minus recorded excerpts. Every fact quotes its source (or "
         "carries an authored rationale) and declares how it may be discovered. Requirements are grounded to a visible fact or to a verbatim human statement; "
         "code rejects anything else. The contract names the exact test ids and the assets that carry them; requirement groups partition FAIL_TO_PASS with "
         "weights that sum to one. Author-only facts can never link tests or support a public requirement.",
         (S(MODELS, "ScenarioSpec, Fact, Discovery, Requirement, Controls, Group and _integrity"), S(DRAFT, "module docstring and SYSTEM prompt"),
          S(ARCH, "Creation and evidence: drafter rules")),
         Console("/scenario/<id>", "'Briefs' (natural and withheld side by side) and 'Ledger' with each fact's kind and discovery; 'Requirement groups' "
                 "with weights and per-test results are on the episode page", "/scenarios"),
         SPEC_TREE,
         (Decision("A requirement must link to a visible fact or quote human issue/thread text; author-only facts cannot support public requirements.",
                   "A requirement that only the hidden tests justify is a test leaking into the stakeholder's mouth.",
                   "Some real maintainer decisions are dropped because nobody wrote them down.", "2026-09-13", S(ARCH, "Creation and evidence")),
          Decision("Forbidden terms are code identifiers from the hidden tests absent from the natural issue, plus fixed test vocabulary; builtins, keywords, comments and string prose are excluded.",
                   "The reporter did not write the tests, so their names are not the reporter's vocabulary; but `ValueError` or `None` is language, not a test author's choice.",
                   "A paraphrase of a hidden assertion can still pass the filter; the disclosure audit measures that residual.", "2026-09-13", S(VIEW, "module docstring (A-P1-2 ruling)")))),
    Card("Withholding: the loss is recorded, not just performed",
         "withhold() asks the drafter model for spans of the natural issue a reporter would plausibly omit and extends them to whole segments so tracebacks and "
         "paragraphs survive intact. If it selects nothing while the issue visibly carries a fenced block or traceback, it is nudged once; then zero removals "
         "stands. Each removed occurrence becomes its own original-issue fact with a withholding operation, and the approval gate recomputes the withheld brief "
         "from those excerpts alone; any difference, in either direction, rejects the draft.",
         (S(DRAFT, "withhold(), WITHHOLD_NUDGE, reconstruct_withheld()"), S(APPROVE, "withheld_reconstruction_failures"), S(MODELS, "ScenarioSpec._integrity: withheld rules"),
          S(CLAUDE_MD, "Contracts: brief profiles")),
         Console("/scenario/<id>", "'Briefs': the withheld profile beside the natural one; the ledger rows whose id starts with withheld- are the removals", "/scenarios"),
         WITHHOLDING,
         (Decision("Each withheld occurrence has its own excerpt and operation; reconstruction consumes one occurrence per entry.",
                   "One ledger record could otherwise account for several identical deleted sentences and hide an extra deletion.",
                   "More ledger rows to read on a scenario page.", "2026-09-13", S(ARCH, "Creation and evidence")),)),
))

# ----------------------------------------------------------------------------- 5. the stakeholder

EXCHANGE = """
 agent                                        stakeholder service   (one session per trial; Modal)
   │  ask-user "Should the loader raise, or return an empty result, on an empty file?"
   │ ────────────────────────────────────────────────────────────────────────────────────▶ │
   │                              request id bound to the body; patience 6 → 5 (question_cost 1)
   │                              prompt = persona + brief + requirements + visible facts
   │                                       (author-only facts, oracle and hidden tests: absent)
   │                              the model answers as the person who filed the request
   │                              filter: a forbidden term, or a pick among the agent's options?
   │                                 yes ─▶ regenerate once under the strict instruction
   │                                        still? ─▶ redact that sentence to "[withheld]"
   │ ◀──────────────────────────────────────────────────────────────────────────────────── │
   │  {reply: "An empty file should give an empty result; nothing should crash.",
   │   patience_remaining: 5}              evaluator-only, on the transcript event:
   │                                       tier · disclosed_fact_ids · redacted_terms · regenerated
   │
   │  confirm-behavior "Empty input now returns an empty result; other inputs are unchanged."
   │ ────────────────────────────────────────────────────────────────────────────────────▶ │  −2 → 3
   │ ◀── "Yes, that is what I asked for."  — or it names the requirement that still seems unmet
   │  code-shaped text (def/class/import, fences, diff headers) → 422 before any model call
   │  request-review (a diff): only under review_mode diff-only, labelled unverified; otherwise the
   │  shim answers "use confirm-behavior"
"""

PATIENCE = """
 patience  6 ─ask (−1)─▶ 5 ─ask (−1)─▶ 4 ─confirm (−2)─▶ 2 ─ask (−1)─▶ 1 ─ask (−1)─▶ 0
 tier      full          full          full              terse (< 3)   terse         refuse (≤ 0)
                                                         one or two sentences;        fixed reply,
                                                         "look in the repository"     no model call
 the agent sees only the number; the tier is an evaluator-only annotation on the transcript event
"""

VERSIONS = """
 v1  maintainer who reviews diffs; request-review was the default channel
     ─▶ audit finding: replies exposed how the hidden tests were built, including expected behavior
 v2  reporter stance + code-owned reply filter (regenerate once, then redact to "[withheld]");
     confirm-behavior replaces diff review by default; speaks only in the request's vocabulary
 v3  the v2 prompt on claude-opus-5  (RunConfig.stakeholder.model_id changes, prompt_version "v3")
     ─▶ v3 trials form their own groups and never pool with v1 or v2 records
"""

SECTION_5 = Section("stakeholder", "The stakeholder", (
    "The stakeholder is the person who filed the request, not a maintainer with the test suite. It knows the behavior it wants and saw, answers at the "
    "specificity asked, and pays out a patience budget; the service enforces with code what the prompt asks for in words, and records the residual for audit."), (
    Card("One exchange, end to end",
         "The worker builds the stakeholder-visible view (briefs, non-author-only facts, requirement texts, persona, checks) and a separate code-owned "
         "deny-list of hidden-test identifiers; the view never references assets, controls or the contract, so nothing downstream can leak them by accident. "
         "Each reply is generated as the reporter, then filtered: a forbidden term or a recognized option pick triggers one regeneration under a stricter "
         "instruction, and any sentence still offending is redacted. Replies are persisted before they are returned; exchange and freeze share a lock; a "
         "request id is bound to its body so a retry cannot replay a different question.",
         (S(PROMPTS, "system_prompt, OPTIONS_RULE, STRICT_INSTRUCTION"), S(SESSION, "module docstring: channels, filter, public reply shape"), S(VIEW, "module docstring"),
          S(ARCH, "Conditions and stakeholder boundary")),
         Console("/trial/<id>", "the conversation in the episode timeline; the evaluator switch reveals patience tier, disclosed facts and redactions per reply", "/trials"),
         EXCHANGE,
         (Decision("The agent sees only the reply text and patience_remaining; tier, fact ids, redactions and usage are evaluator-only.",
                   "The availability signal is part of the design; the annotations are how the evaluator audits disclosure, and would steer the agent if shown.",
                   "A policy cannot learn from the annotations, which is intended.", "2026-09-13", S(SESSION, "Public reply shape (A-P1-3 ruling)")),
          Decision("confirm-behavior (plain-language description, confirmed or an unmet requirement named) replaces diff review by default; request-review exists only under review_mode: diff-only and is unverified.",
                   "A reporter cannot read code, and v1 diff review exposed how hidden tests were constructed.",
                   "Agents lose a channel some scaffolds expect; what the model does with a prose description remains a prompt rule the audit checks.",
                   "2026-09-13", S(GRADING, "Historical task-sense and disclosure audit")))),
    Card("Patience: a budget, not a timer",
         "Every reply spends patience: a question costs 1, a confirmation or review costs 2, from a default start of 6. Below 3 the reporter turns terse and "
         "points the agent at the repository; at 0 it refuses with a fixed sentence and no model call. Refusals are recorded like any exchange. Asking changes "
         "what the agent knows; it never changes the tests or the reward.",
         (S(MODELS, "PatienceProfile defaults"), S(SESSION, "module docstring: patience tiers"), S(CLAUDE_MD, "Contracts: stakeholder")),
         Console("/trial/<id>", "the patience meter on each stakeholder reply in the timeline; questions and confirmations per cell are under "
                 "'Scores and interaction details' on /leaderboard", "/trials"),
         PATIENCE),
    Card("v1 → v2 → v3, and why",
         "v1 spoke as a maintainer and reviewed diffs by default; the historical audit found those reviews exposed test construction, including expected "
         "hidden-test behavior. v2 adopted the reporter stance and the code-owned reply filter (regenerate once, then redact) and made behavioral confirmation "
         "the default channel. v3 is the v2 prompt on claude-opus-5, chosen because pipeline roles must be very well-aligned frontier models; the model id is "
         "part of the run configuration, so v3 trials form their own groups. The preserved records still carry prompt_version v1 in their run configs while "
         "their transcripts show v2 behavior; that discrepancy is kept, not rewritten.",
         (S(GRADING, "Historical task-sense and disclosure audit; Preserved inventory"), S(BRIEF, "§3.1 Pipeline roles"), S(MODELS, "StakeholderConfig")),
         Console("/db/<table>", "the trials table's run_config column carries stakeholder.model_id and prompt_version for every attempt", "/db/trials"),
         VERSIONS,
         (Decision("Judge and stakeholder run on claude-opus-5 with gpt-5.6-sol as fallback; the drafter differs from the judge.",
                   "Very well-aligned frontier models only for roles that author or role-play a task; the Anthropic path (adaptive thinking, fallback on rejection) was already exercised.",
                   "A fallback model that behaves differently in a role; every trial records the resolved id so the groups stay separate.", "2026-09-13", S(BRIEF, "§3.1 Pipeline roles")),)),
))

# ----------------------------------------------------------------------------- 6. verifier and reward

VERIFIER = _flow("""
 collector receipt   root-owned, /var/swesim-collect; /logs/artifacts is never a trust anchor
   │ receipt missing or invalid, not root, archive digest ≠ receipt ⇢ ▶ 5 collection-failure
   │ agent image base-manifest digest ≠ the verifier's own ⇢ ▶ 7 evaluator-failure
   ▼
 safe extract        symlinks, hard links, device files, escaping members: refused and recorded
   │ oversized or invalid tree ⇢ ▶ 3 agent-invalid-submission
   ▼
 diff against the untouched base → apply only allowed source changes to /testbed or /app
   │ protected paths dropped and recorded: contract tests, conftest, pytest config, lockfiles, CI
   │ nothing applied (a no-op still runs the tests for base evidence) ⇢ ▶ 3 agent-invalid-submission
   ▼
 write the submission diff → overlay the hidden assertions → chown the tree to tester
   ▼
 run   the task's own interpreter with -I; trusted launcher imports pytest before submitted roots
   │   four signals must agree:  collected ids = contract · JUnit lists each id once
   │                             terminal summary = JUnit · exit status = any failure
   │ disagreement ─▶ observations unknown; reruns are diagnostic only ⇢ ▶ 4 agent-runtime-failure
   ▼
 credit
   success = 1  iff every FAIL_TO_PASS and PASS_TO_PASS test passed ⇢ ▶ 1 valid-correct
   reward  = Σ weight(g) · [ every test of group g passed ] ⇢ ▶ 2 valid-incorrect else
   any PASS_TO_PASS failure (a regression)  ⇒  reward = 0
""", col=72)

CONTROLS = """
 control                 must earn                              what it proves
 ─────────────────────── ────────────────────────────────────── ─────────────────────────────────
 nop (nothing applied)   reward 0; base F2P fail, P2P hold      the tests fail before the fix
 oracle (the merged fix) success 1, reward 1                    the contract is satisfiable
 plausible-wrong         success 0, reward ≤ its declared max   a wrong fix cannot farm credit
 alternative-correct     success 1, reward 1 (when required)    tests judge behavior, not shape
 tamper ×3 (exploits)    exactly 0, without an infra failure    known forgery paths stay closed
 reachability probe      no gold object, no gold patch line     the answer is not in the sandbox
"""

SECTION_6 = Section("verifier", "The verifier and reward", (
    "Grading happens in a separate offline container that trusts only a root-written receipt and its own copy of the base tree. Success is the whole "
    "contract; reward is partial credit by requirement group; a regression zeroes it; disagreement between test signals earns nothing."), (
    Card("From archive to reward.json",
         "The verifier reads the collector's receipt, requires the agent image's base-manifest digest to equal its own, extracts the archive safely, diffs it "
         "against the pristine base and applies only allowed source changes, dropping and recording edits to protected paths. It writes the submission diff, "
         "overlays the hidden assertions and runs pytest through a trusted launcher as tester. Outcomes are keyed on success; reward is computed only when the "
         "four test signals agree. Failures yield zero; diagnostic reruns never restore credit.",
         (S(GRADER, "grade(): steps 0–7 and OUTCOMES"), S(POLICY, "DEFAULT_PROTECTED, safe_extract, change_set"), S(GRADING, "Reward and outcomes; Test evidence"),
          S(CLAUDE_MD, "Contracts: grading")),
         Console("/trial/<id>", "the verifier row of the timeline (outcome chip), 'Cost', and report.json / reward.json under Files with per-test and per-group rows",
                 "/trials"),
         VERIFIER,
         (Decision("success is whole-contract correctness; reward is partial credit by fixed positive-weight requirement groups; any regression sets reward to zero.",
                   "A partially right fix that breaks something else is worse than no fix; group weights are authored once and never depend on what the agent asked.",
                   "A model that fixes most of a task but regresses one test scores like a model that did nothing.", "2026-09-12", S(CLAUDE_MD, "Contracts: reward")),
          Decision("Four signals (collected ids, JUnit, terminal summary, exit status) must agree; disagreement is outcome 4 with reward 0 and reruns are diagnostics.",
                   "Submitted code runs inside pytest and can forge any one report; forging all four consistently is harder and a mismatch is caught.",
                   "A coordinated forgery of all four is not caught by these checks (acknowledged residual).", "2026-09-13", S(GRADING, "Test evidence and diagnostic budget")))),
    Card("Budgets, outcomes and the acknowledged residual",
         "The main run has a compiled 600-second budget; diagnostic reruns get a separate 600 seconds across at most ten FAIL_TO_PASS ids; the verifier as a whole "
         "has 1,800 seconds. An output flood kills the run and skips diagnostics. The seven outcome codes separate agent results (1–4) from collection (5), "
         "simulator (6) and evaluator (7) failures; an infrastructure error is never counted as a defeated exploit. The residual is stated: the submission "
         "shares pytest's process and manifest parity, isolation flags and consistency checks do not authenticate every possible in-process report.",
         (S(GRADING, "Test evidence and diagnostic budget; Reward and outcomes"), S(COMPILE, "VERIFIER_TIMEOUT_SEC, TEST_TIMEOUT_SEC"), S(ARCH, "Build and grading trust"),
          S(CLAUDE_MD, "Known residual")),
         Console("/trials", "filter by outcome; unresolved, runner-error and launch-failed attempts are listed with the completed ones, never hidden")),
    Card("Controls: every registered task earned its reward scale",
         "Validation executes the controls on the compiled task and compares recorded rewards with the spec's expectations. A control counts only when the "
         "pipeline itself worked (outcomes 1–4); a collection, simulator or evaluator failure is not a passing control. The reachability probe pairs distinctive "
         "added lines with their hunk's removed lines so a stale base copy is not mistaken for a post-fix leak; it is a heuristic, not a proof.",
         (S(VALIDATE, "evaluate_controls and the reachability probe"), S(GRADING, "Controls and authoring gates"), S(BRIEF, "§7 Grading integrity")),
         Console("/scenario/<id>", "'Controls and validation': expected vs observed per control, 'as expected' chips, linked to validation.json", "/scenarios"),
         CONTROLS,
         (Decision("Incorrect-exploit tamper controls must apply and earn exactly zero; an infrastructure error does not count as a defeated attack.",
                   "The question is whether the grader resists the attack, which an unrelated failure does not answer.",
                   "A scenario stays invalid until the control runs cleanly, even when the attack would have failed anyway.", "2026-09-12", S(CLAUDE_MD, "Contracts: controls")),)),
))

# ----------------------------------------------------------------------------- 7. the judge

JUDGE_FLOW = """
 candidate row:  issue text · thread · merged patch · test patch · FAIL_TO_PASS ids
        │
        ├─ code  issue_contract_delta(): exceptions the tests expect, literals they assert, public
        │        names the fix introduces — none of which the issue text mentions  (candidate gaps)
        ▼
 judge model  (a different model from the drafter, so their errors are not correlated)
   classifies each candidate gap and proposes more; every gap carries
     evidence     verbatim from test_patch or patch — a gap with non-verbatim evidence is dropped
     question     what a developer would have to ask to settle it
     derivable_from_repo · consequential · resolved_by thread | tests | none · category
   verified_rubric_score  0 well specified · 1 minor blanks · 2 several valid · 3 near impossible
        │
        ▼
 accept()  — code-owned; the model only scores
   self_contained is true  AND  some gap has  derivable_from_repo == false
                                          AND consequential == true
                                          AND resolved_by in {thread, tests}
        │ true                                        │ false
        ▼                                             ▼
   kept → probe → draft  (judge.json travels along)   rejected at gate judge, reason recorded
"""

CALIBRATION = """
 positive = verified_rubric_score ≥ 2  (judge)   vs   underspecified ≥ 2  (human ensembled label)
 sample: 150 requested, 145 judged, 5 oversized inputs excluded; prompt judge-v3

                     gpt-5.5                                  claude-opus-5
                     human +    human −                       human +    human −
   judge +           tp  29     fp  16                        tp  29     fp  19
   judge −           fn  34     tn  66                        fn  34     tn  63
   agreement 0.655 · precision 0.644 · recall 0.460           0.634 · 0.604 · 0.460
"""

SECTION_7 = Section("judge", "The judge", (
    "The judge decides whether a merged PR hides a real question: something the tests pin that the issue never says and the repository cannot settle. "
    "The model scores and grounds; code decides."), (
    Card("Rubric, gaps and the code-owned accept()",
         "Code first extracts candidate gaps mechanically. The judge model then classifies them and proposes more, each grounded by a verbatim excerpt of the "
         "test or patch, and scores the issue on the SWE-bench Verified rubric from 0 (well specified) to 3 (nearly impossible without more information). "
         "The task is kept only if accept() holds: the task is self-contained and at least one gap is explicitly non-derivable from the repository, "
         "consequential, and resolved either by a statement in the thread or by the merged tests. Gaps are phrased as user-visible behavior, never as test construction.",
         (S(JUDGE, "SYSTEM, SCHEMA, issue_contract_delta(), accept()"), S(ARCH, "Creation and evidence"), S(BRIEF, "§3.1 Pipeline roles")),
         Console("/candidates", "every judge rejection with gate judge and its reasons; the 'Judge' block on /scenario/<id> (score, complexity, gaps, rationale) "
                 "is evaluator-only"),
         JUDGE_FLOW,
         (Decision("The judge never sees the annotators' notes; the judge and draft models must differ.",
                   "Calibration must measure agreement with an independent label, and correlated errors between judge and drafter would go unnoticed.",
                   "A collision in the fallback chains advances the draft chain and records the reasoning.", "2026-09-13", S(CLAUDE_MD, "Unattended-run rules")),)),
    Card("Calibration against human labels",
         "The metric is agreement with the SWE-bench Verified annotators, with a positive defined as verified_rubric_score ≥ 2 on both sides, not the narrower "
         "creation keep decision. The preserved gpt-5.5 summary reports agreement 0.655, precision 0.644 and recall 0.460 on 145 judged instances but has no "
         "per-instance predictions. The Opus 5 run on the same rows recorded 0.634 / 0.604 / 0.460 at a cost of $10.81. New calibration artifacts carry "
         "per-instance scores, confusion class, errors, seed, model and prompt identity and usage; a fresh run in that shape is deferred with the experiments.",
         (S(GRADING, "Judge calibration"), S(JUDGE, "calibrate(), CALIBRATION_POSITIVE"), LEDGER),
         Console("/artifacts", "calibration/<judge-model>/<day>.json holds the per-instance rows of any new run; the legacy summary is judge_calibration.json",
                 "/artifacts?path=calibration"),
         CALIBRATION,
         (Decision("The judge stays on claude-opus-5 although gpt-5.5 scored about two points higher on agreement.",
                   "The user's alignment directive: roles that author or role-play a task run on very well-aligned frontier models; both numbers are reported.",
                   "About two points of agreement on the calibration set.", "2026-09-13 02:10 PDT", LEDGER),
          Decision("Positive class is rubric score ≥ 2 versus human underspecified ≥ 2, with per-instance predictions saved.",
                   "The keep decision mixes in self-containment and resolution, which the humans did not label; a summary without rows cannot be reproduced.",
                   "Older summaries are not comparable and are labelled as such.", "2026-09-13", S(GRADING, "Judge calibration")))),
))

# ----------------------------------------------------------------------------- 8. difficulty and task sense

SCREEN = """
 after validation: purpose screen, model openrouter/deepseek/deepseek-v3.2, hidden k=2, full k=1
 three attributable, consistent results, distinct trial ids; infrastructure cannot label a task
        │
        ▼
 full 0/1 ?  ── yes ──▶  BROKEN       brief plus every fact still fails: suspect the task
        │ no
        ▼
 hidden 2/2 ? ─ yes ──▶  SATURATED    too easy for the screen model: off the board by default
        │ no
        ▼
                         INFORMATIVE  hidden ≤ 1/2: the task discriminates

 leaderboard  =  validation valid  ∧  difficulty informative  ∧  task-sense coherent ∧ fair_tests
                 (saturated only with the toggle; broken and unaudited never)
"""

TASK_SENSE = """
 scenario   coherent · fair_tests · plausible_withholding
 question   specific · decision_oriented · before_implementation · redundant · fishing
 reply      targeted · volunteered[] · test_or_identifier_disclosed · behaviour_level · ungrounded[]
 trial      asked_before_implementing · useful_question_rate · patience_sensible

 every label is true, false or unknown, never a string; rubric version 2026-09-13.1
 mechanical labels from transcript + trajectory; the reading pass runs on claude-opus-5
"""

SECTION_8 = Section("difficulty", "Difficulty screen and task-sense audit", (
    "Hard enough is measured, never asserted from diff size: a cheap screen model labels every valid scenario, and a reading pass asks whether the task "
    "makes sense, whether the hidden tests are fair, and whether the conversation disclosed the right things at the right time."), (
    Card("The difficulty screen",
         "After validation the screen runs the withheld brief twice without a stakeholder and the full brief once, on one frozen model and build, as purpose "
         "screen so the attempts never pool with comparisons. Full failing wins over hidden saturation. Labels live in difficulty.json and on the scenario row; "
         "the leaderboard shows every valid, audited scenario whatever its label (the label is a chip and a filter, not an exclusion), and the Trends page reads difficulty as a capability gradient across solver tiers instead.",
         (S(DIFFICULTY, "label(), SCREEN_MODEL, SCREEN_COUNTS"), S(BRIEF, "§5 Difficulty"), S(INTERFACES, "§9.1 difficulty.json and the leaderboard gate")),
         Console("/trends", "the capability gradient (accuracy by tier A→E, per-scenario gradient chips) and 'Screen labels' with one link per label; /scenario/<id> 'Difficulty' shows the three screen trials"),
         SCREEN,
         (Decision("Difficulty comes from selection and measurement; a scenario is never edited after a gate to make it harder.",
                   "Hand-tuned tests would measure the tuner, not the model, and would break the provenance of every fact and requirement.",
                   "Some easy tasks are registered and then excluded as saturated instead of being fixed.", "2026-09-13", S(BRIEF, "§5 Difficulty: Not done")),)),
    Card("The task-sense audit",
         "Mechanical labels are computed from the frozen transcript and trajectory (was the question asked before the first edit, does the brief already answer "
         "it, did the reply use a term the filter would catch). The labels that need reading are made by claude-opus-5 from evaluator-only inputs: both briefs, "
         "the removed spans, the visible facts, the requirements with their grounding, the hidden test source and the oracle diff or transcript. Labels are "
         "stored in audit.json per scenario and per trial, projected into the index, and gate the leaderboard through coherent and fair_tests.",
         (S(RUBRIC, "RUBRIC, RUBRIC_VERSION"), S(LLM_AUDIT, "module docstring"), S(INTERFACES, "§9.1 audit.json")),
         Console("/scenario/<id>", "the 'Task sense' block with the three scenario labels; per-reply labels appear on the episode page for evaluators", "/scenarios"),
         TASK_SENSE),
    Card("What the audit found on the preserved record",
         "On the three registered version-2 scenarios: seaborn is coherent with fair tests; sklearn's withholding removed its only error description, so it is "
         "labelled not plausible; pypdf's hidden test calls an oracle-introduced private helper and pins an exact warning string, so its tests are unfair and it "
         "stays off the board. Across 12 current-spec interactive trials, ten asked at least once; of 22 replies, eight volunteered information and six disclosed "
         "a test, identifier or ungrounded requirement; five trials edited protected test files, which grading dropped. These are audit labels, not adjudicated training labels.",
         (S(GRADING, "Historical task-sense and disclosure audit"),),
         Console("/leaderboard", "the 'Gate' table: pypdf fails task sense; /artifacts evidence/audit/2026-09-13-task-sense.json is the report itself")),
))

# ----------------------------------------------------------------------------- 9. frontier gate

FRONTIER_GATE = """
 run-tasks names a Tier D or E model
   │   D: claude-sonnet-5, gpt-5.6-sol   E: gpt-6-astra, claude-fable-5-1, claude-opus-5, gpt-5.5
   ├─ 15 qualified scenarios?  valid ∧ audited ∧ screened; for each of the six core baselines ≥ 2
   │                           outcomes per arm (full, hidden, interactive) from purpose comparison,
   │                           current spec + build, origin cloudrun, same protocol/scaffold per arm
   ├─ full > hidden on a majority of them?  per scenario, pooled over the six baselines, strict
   ├─ targets: saturated excluded unless include_saturated; broken, unaudited, unscreened never
   ├─ Tier E at k > 1 only after that model has a completed k=1 trial
   ├─ a positive spend cap in force  (the run's cap or the global cap, whichever is lower)
   ├─ spend accounting complete: no unpriced rows, no unknown usage treated as zero
   └─ forecast ≤ remaining cap  (max observed baseline tokens priced for the model × trials × 1.25)
        │ all hold                                  │ any fails
        ▼                                           ▼
   decision written to evidence/sweep/          refused, every failing reason listed;
   frontier-<identity>.json (identity = hash    nothing launched, no model substituted
   of the policy request); plan_trials re-reads
   and verifies it against its own request
"""

SECTION_9 = Section("frontier", "Frontier admission gate", (
    "Frontier models are expensive and their trajectories are the ones people will read, so they run only once the cheap tiers prove the tasks separate "
    "models and the spend forecast fits. The rule of brief §3.2, as implemented."), (
    Card("The rule as code",
         "Tier A–C requests pass through. A request naming a Tier D or E model is evaluated from frozen evidence: fifteen current, audited, screened scenarios "
         "with two comparison outcomes per arm for each of the six core baselines, the full-over-hidden ordering on a majority of them, eligible targets, "
         "k=1 first for Tier E, a positive cap, complete spend accounting and a forecast under the remaining cap. The decision is written to the evidence "
         "directory and every later planning step verifies the recorded decision against its own request instead of trusting a flag.",
         (S(FRONTIER, "module docstring, evaluate(), verify_admission()"), S(BRIEF, "§3.2 Evaluated agents: gating rule"), S(GRADING, "Deferred experimental evidence")),
         Console("/workflows/<id>", "a refused frontier run fails at plan_trials with every reason in the failure text; /run lists Tier D and E as gated; "
                 "the recorded decision is browsable under /artifacts?path=evidence/sweep", "/workflows"),
         FRONTIER_GATE,
         (Decision("The full > hidden ordering is judged per scenario, pooled over the six core baselines, not per model.",
                   "With two attempts per arm, requiring every model to order strictly would open the gate only by luck even when the effect is large; a gate that can never open is a defect.",
                   "A scenario where one baseline is inverted can count as ordered; the majority-of-fifteen rule and per-model coverage still stand.", "2026-09-13", S(FRONTIER, "_coverage() comment")),
          Decision("Admission is a durable decision file that plan_trials re-reads and verifies, not a trusted parameter.",
                   "Forging it would require writing into the same evidence directory the gate evaluates; tests and production share one path.",
                   "One extra file read per plan.", "2026-09-13", S(FRONTIER, "verify_admission()")),
          Decision("Frontier tiers only after the path is proven on cheap models; spend tracked per run and capped.",
                   "About $1,500 is acceptable, but a leaderboard on a bad task is worthless and frontier trials cannot be re-run cheaply.",
                   "A smaller leaderboard by the deadline.", "2026-09-13", S(BRIEF, "§1 Spend; §1 Evaluated agents")))),
))

# ----------------------------------------------------------------------------- 10. decisions log

DECISIONS: tuple[Decision, ...] = (
    Decision("Infra first: Temporal Cloud, worker pools, IAP console and Cloud SQL live before any sweep; the sweep runs only on the new stack.",
             "Anything measured on the old VM path would have to be redone; the hosted stack is the deliverable.",
             "Fewer results by morning if the infrastructure takes the night.", "2026-09-13", S(BRIEF, "§1 Priority")),
    Decision("Temporal Cloud namespace swesim.abcde (gcp-us-central1) with API-key auth, six swesim_* search attributes.",
             "Durable history, retries, signals and cancellation without operating a server; the client already supported it.",
             "A vendor dependency for orchestration.", "2026-09-13 07:32 UTC", S(BRIEF, "§1 Temporal Cloud")),
    Decision("Cloud Run rather than GKE: a worker pool per queue, a Job per trial, the console as a service.",
             "GKE adds a cluster, RBAC and secrets machinery for no needed capability; the launcher protocol is runtime-neutral.",
             "A later GKE module if Cloud Run limits are reached.", "2026-09-13 07:50 UTC", S(BRIEF, "§1 Compute; §1 Runtime")),
    Decision("GCS bucket as the record of truth; Cloud SQL Postgres as a rebuildable index.",
             "Files stay authoritative; tables serve the console and score tables without globbing the bucket.",
             "An index that lags the files until rebuilt.", "2026-09-13", S(BRIEF, "§1 Database")),
    Decision("Cloud SQL: public IP, no authorized networks, reached only through Cloud Run's IAM-authenticated proxy socket.",
             "Saves the VPC connector and private-services setup for the night.",
             "An instance with a public address that is still unreachable without IAM and a password.", "2026-09-13", LEDGER),
    Decision("Console IAP through Cloud Run's native integration (no load balancer, NEG or certificate) when the API accepts it.",
             "Fewer moving parts than the brief's load-balancer path; the brief's path remains the fallback.",
             "One Terraform module rewritten.", "2026-09-13", LEDGER),
    Decision("The console data interface is an IndexReader protocol in swesim/index/reads.py owned by storage and consumed by the console.",
             "Keeps dashboard/** wholly owned by the console stream and index/** by storage.",
             "An import path.", "2026-09-13", LEDGER),
    Decision("swesim/settings.py is written by the integrator before the streams start; other Phase 0 stubs come from the frozen interfaces spec.",
             "Everything depends on typed settings; parallel streams cannot start on a moving target.",
             "Not recorded in the ledger.", "2026-09-13", LEDGER),
    Decision("scripts/cloud/import_vm.sh is the one file under scripts/cloud owned by storage; infra owns the rest.",
             "File-level disjointness between parallel streams holds.",
             "Not recorded in the ledger.", "2026-09-13", LEDGER),
    Decision("Deploy main with two known red tests to exercise the deploy path and start the smoke.",
             "Both failures were test expectations under rewrite, not worker or console faults.",
             "One more deploy.", "2026-09-13 02:20 PDT", LEDGER),
    Decision("The judge stays on claude-opus-5 (agreement 0.634) rather than gpt-5.5 (0.655); both numbers are reported.",
             "The user's alignment directive for pipeline roles.",
             "About two points of agreement on the calibration set.", "2026-09-13 02:10 PDT", LEDGER),
    Decision("The Codex takeover replaces the exhausted implementers with the session's model; three streams continue in their worktrees and no inherited change is discarded.",
             "The overnight run was cut off by an API limit; the scope remained required.",
             "Not recorded in the ledger.", "2026-09-13 16:19 UTC", LEDGER),
    Decision("Deploy with OVERRIDE_OPEN_EXECUTIONS=1 while a CreateTasksWorkflow was open.",
             "Every command-sequence change sat behind workflow.patched markers and the open workflow's remaining commands were unchanged in shape.",
             "One non-determinism failure on that workflow, which would be re-run.", "2026-09-13 13:05 PDT", LEDGER),
    Decision("Creation scale: SWE-bench pool A (109 instances) plus ten repositories in the bugfix band.",
             "Pool A calibrates the judge; the repositories are the product.",
             "Not recorded in the brief.", "2026-09-13", S(BRIEF, "§1 Creation scale")),
    Decision("About $1,500 or more is acceptable, focused on cheap models; frontier models only once the path is proven with trends.",
             "Frontier trials are the expensive, visible ones; a leaderboard on a bad task is worthless.",
             "A smaller leaderboard.", "2026-09-13", S(BRIEF, "§1 Spend")),
    Decision("Console access through HTTPS load balancer + Identity-Aware Proxy, the user's Google account only.",
             "The website is the way the pipeline is operated, so it must be authenticated at the edge.",
             "Not recorded in the brief.", "2026-09-13", S(BRIEF, "§1 Console access")),
    Decision("Pipeline LLMs (judge, stakeholder, drafter) are very well-aligned frontier models: claude-opus-5 primary, gpt-5.6-sol fallback.",
             "These roles author and role-play tasks; misalignment there corrupts every downstream measurement.",
             "Higher authoring cost per task.", "2026-09-13", S(BRIEF, "§1 Pipeline LLMs; §3.1")),
    Decision("Evaluated agents in tiers: older and newer DeepSeek and GLM first, then other cheap open and small closed models, frontier last and gated.",
             "Cheap tiers establish the floor and the 'asks nothing' baseline before expensive trajectories are bought.",
             "Not recorded in the brief.", "2026-09-13", S(BRIEF, "§1 Evaluated agents; §3.2")),
    Decision("Tasks must be hard enough: difficulty is measured on every scenario by the screen, never asserted from diff size.",
             "Diff size is a filter; a saturated toy task is not evidence of model separation.",
             "Some registered tasks are excluded from the board as saturated.", "2026-09-13", S(BRIEF, "§1 Tasks; §5")),
    Decision("Creating tasks from new repositories given as links is the main goal; pool A is the calibration set, not the product.",
             "Re-labelling SWE-bench measures nothing new.",
             "Fewer tasks by morning than mining a curated pool.", "2026-09-13 08:20 UTC", S(BRIEF, "§1 Primary objective")),
    Decision("Reusable by a stranger, cloud-only: a fresh clone, a GCP project, a Temporal namespace and an env file reach a running pipeline via docs/setup.md.",
             "No author-machine path, host or account may be load-bearing.",
             "Not recorded in the brief.", "2026-09-13 08:35 UTC", S(BRIEF, "§1 Reusability")),
    Decision("The leaderboard is gated by the controls, the difficulty screen and the task-sense audit; setting everything up correctly matters more than its size.",
             "A leaderboard on a bad task is worthless.",
             "A smaller leaderboard.", "2026-09-13 08:50 UTC", S(BRIEF, "§1 Leaderboard")),
    Decision("The console shows the entire database, every bucket artifact and the complete history: every workflow, trial (including failed and unresolved), candidate, reject and version.",
             "The website is the way we interact with the pipeline.",
             "Not recorded in the brief.", "2026-09-13 08:10 UTC", S(BRIEF, "§1 Console scope")),
    Decision("Outcomes 1–4 count in the pass@k denominator; only outcome 1 is a success; 5–7 are reported separately.",
             "An agent-caused failure is the agent's result; a harness failure is not evidence about the agent.",
             "Pass rates would otherwise track infrastructure reliability.", "2026-09-12", S(SCORE, "module docstring: Denominators")),
    Decision("The frontier gate's full > hidden ordering is pooled over the six core baselines per scenario.",
             "A per-model strict rule at n=2 would open the gate only by luck; a gate that can never open is a defect.",
             "One inverted baseline can count as ordered; the majority-of-fifteen rule still stands.", "2026-09-13", S(FRONTIER, "_coverage() comment")),
    Decision("Task creation and graded runs go through Temporal only; stage commands refuse to run without SWESIM_DIRECT=1.",
             "One durable path with retries, receipts and search attributes; direct stage runs leave no workflow history.",
             "Debugging a single stage takes an explicit opt-in.", "2026-09-13", S(CLAUDE_MD, "Execution rule (user directive, 2026-09-13)")),
)

SECTION_10 = Section("decisions", "Decisions log", (
    "Every decision with its reason and its cost if wrong, from the brief's §1 table, the ledger's Ruling lines and the rulings recorded in code. "
    "Where a source states no cost, the row says so rather than inventing one."), (
    Card("Decisions, reasons, costs",
         "Sorted as recorded; the source column names the document and section. Rows from the operator ledger cite a file that git ignores, so they are "
         "labelled as such; everything else is a tracked repository path.",
         (S(BRIEF, "§1 Decisions taken with the user"), LEDGER, S(CLAUDE_MD, "Contracts; Execution rule"), S(SCORE, "Denominators"), S(FRONTIER, "module docstring")),
         Console("/db/<table>", "operator_actions records every console mutation with actor, params and result; the decisions above are design-time, this table is run-time",
                 "/db/operator_actions")),
))

SECTIONS: tuple[Section, ...] = (SECTION_1, SECTION_2, SECTION_3, SECTION_4, SECTION_5, SECTION_6, SECTION_7, SECTION_8, SECTION_9, SECTION_10)


def cited_paths() -> list[str]:
    """Every tracked repository path the page cites (cards and decisions), sorted; tests check each exists."""
    paths = set()
    for section in SECTIONS:
        for card in section.cards:
            paths.update(s.path for s in card.sources if s.tracked)
            paths.update(d.source.path for d in card.decisions if d.source and d.source.tracked)
    paths.update(d.source.path for d in DECISIONS if d.source and d.source.tracked)
    return sorted(paths)


# ----------------------------------------------------------------------------- rendering

def _sources(sources: tuple[Source, ...]) -> str:
    parts = []
    for s in sources:
        note = "" if s.tracked else " (operator ledger, not tracked)"
        parts.append(f"<code>{h(s.path)}</code> <span class='muted'>{h(s.where)}{h(note)}</span>")
    return "<p class='small'><strong>Sources:</strong> " + " · ".join(parts) + "</p>"


def _console(c: Console) -> str:
    return (f"<p class='small'><strong>Where to see it in the console:</strong> <a href='{h(c.link)}'>{h(c.page)}</a>"
            f" <span class='muted'>— {h(c.look_for)}</span></p>")


def _decisions(rows: tuple[Decision, ...]) -> str:
    if not rows:
        return ""
    items = []
    for d in rows:
        when = f" <span class='muted small'>{h(d.date)}</span>" if d.date else ""
        src = f" <span class='muted small'>({h(d.source.path)})</span>" if d.source else ""
        items.append(f"<dt>{h(d.what)}{when}</dt><dd><span class='muted'>because</span> {h(d.why)}<br>"
                     f"<span class='muted'>cost if wrong</span> {h(d.cost)}{src}</dd>")
    return "<dl class='kv'>" + "".join(items) + "</dl>"


def _card(card: Card) -> str:
    out = [f"<article class='card'><h3>{h(card.title)}</h3><p>{h(card.text)}</p>"]
    if card.diagram:
        out.append(f"<pre class='code diagram'>{h(card.diagram.strip(chr(10)))}</pre>")  # .diagram: hook for a no-wrap rule in app.css
    out.append(_decisions(card.decisions))
    out.append(_console(card.console))
    out.append(_sources(card.sources))
    out.append("</article>")
    return "".join(out)


def _decisions_table() -> str:
    cols = (Column("what", "decision", filterable=True), Column("why", "reason"), Column("cost", "cost if wrong"),
            Column("date", "recorded"), Column("source", "source", "mono", filterable=True))
    rows = [{"key": str(i), "what": d.what, "why": d.why, "cost": d.cost, "date": d.date,
             "source": f"{d.source.path} · {d.source.where}" if d.source else ""} for i, d in enumerate(DECISIONS)]
    return table("decisions-log", cols, rows, key="key", empty="no decisions recorded", export=True, dense=True,
                 caption="Decisions with reason, cost if wrong, date and source")


def _toc() -> str:
    links = "".join(f"<li><a href='#{h(s.key)}'>{i}. {h(s.title)}</a></li>" for i, s in enumerate(SECTIONS, 1))
    return f"<nav class='card' aria-label='sections'><ol class='small'>{links}</ol></nav>"


def _section(i: int, s: Section) -> str:
    cards = "".join(_card(c) for c in s.cards)
    extra = _decisions_table() if s.key == "decisions" else ""
    return f"<section id='{h(s.key)}'><h2>{i}. {h(s.title)}</h2><p class='lead'>{h(s.lead)}</p>{cards}{extra}</section>"


def body() -> str:
    intro = ("<h1>Explainer</h1><p class='lead'>What swesim measures, how a repository URL becomes a graded task, how a model's run is judged, and "
             "why each part is built the way it is. Written from the repository's documents and code; every card says where it comes from and where "
             "to see it in this console. Nothing on this page reads the record, so it is the same for every identity.</p>")
    return intro + _toc() + "".join(_section(i, s) for i, s in enumerate(SECTIONS, 1))
