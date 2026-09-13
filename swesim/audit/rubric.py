"""The task-sense rubric as data, the label models of interfaces §9.1, and the mechanical labels computed without an LLM.

Contract
- ``RUBRIC_VERSION`` pins the label definitions (``RUBRIC``) and the mechanical rules in this module; a change to either
  bumps it and ``swesim.audit.run`` re-audits every record whose stored ``rubric_version`` differs.
- Every label is a boolean or ``None`` (unknown: the evidence needed to decide it was unavailable), never a string; the
  models reject extra keys. ``reason`` is one sentence naming the evidence.
- The mechanical functions are pure. They read the stakeholder service's frozen ``transcript.json`` (message events with
  ``role``/``exchange``/``content``/``disclosed_fact_ids``/``patience_remaining``/``tier``), Harbor's ATIF
  ``agent/trajectory.json`` (steps with ``source``/``timestamp``/``tool_calls``) and spec texts. They favour precision:
  ``redundant_with_brief`` fires only when a brief sentence is restated, ``is_edit_command`` ignores scratch writes under
  ``/tmp`` and ``/dev``, ``test_or_identifier_disclosed`` applies the reply filter's own whole-token term semantics so the
  audit measures exactly what the filter would have caught.
"""
from __future__ import annotations

import builtins
import json
import re
import shlex
from datetime import datetime

from pydantic import ConfigDict, Field

from swesim.models import Strict
from swesim.stages.export import shim_invocation
from swesim.stakeholder.session import REFUSAL, find_forbidden
from swesim.stakeholder.view import FIXED_FORBIDDEN

RUBRIC_VERSION = "2026-09-13.1"

RUBRIC: dict[str, str] = {
    # scenario
    "coherent": "The natural brief plus the stakeholder-visible facts describe one intelligible ask: a symptom or wanted "
                "behaviour, a context in which it occurs, and an outcome a developer could aim at, without contradictions.",
    "fair_tests": "Every hidden FAIL_TO_PASS test checks a consequence of the brief and the stakeholder-visible facts; a test "
                  "that pins the oracle's incidental choices (private names, exact wording nobody stated, a structure only the "
                  "merged patch has) is unfair.",
    "plausible_withholding": "Each span removed from the natural brief is something a reporter would plausibly leave out and a "
                             "developer would plausibly ask about; withholding the symptom itself or the title is not plausible.",
    # questions
    "specific": "Asks about one identifiable decision with enough context that a one- or two-sentence answer settles it.",
    "decision_oriented": "The answer changes what the agent builds (behaviour on an input, scope, error type or message, API "
                         "shape, default), rather than asking for repository facts, general advice or reassurance.",
    "before_implementation": "The question was asked before the agent's first edit or patch command in its trajectory.",
    "redundant": "The brief the agent received already answers the question.",
    "fishing": "Asks the stakeholder to supply the solution, the tests, the verification steps, or asks an open 'anything "
               "else / list everything' question instead of a decision.",
    # replies
    "targeted": "Every fact the stakeholder disclosed answers what the agent asked in this exchange.",
    "volunteered": "Ids of facts disclosed in this reply that the agent's question did not ask about.",
    "test_or_identifier_disclosed": "The reply names a test, a verification step, a function, class, file or path outside the "
                                    "brief, or a behaviour that only the hidden tests pin.",
    "behaviour_level": "The reply speaks in terms of what the software should do and not do; it does not pick among the "
                       "agent's implementation options, prescribe code paths, or describe how the change will be checked.",
    "ungrounded": "Ids of requirements that have no grounding fact (they rest on the hidden tests alone) whose content the "
                  "reply conveys: a test-derived expectation reaching the agent through the requirement list.",
    "patience_sensible": "No exchange was refused for exhausted patience and no question repeated an earlier one.",
}

MIN_REDUNDANT_TOKENS = 4        # a question with fewer distinctive words is never called redundant by the mechanical rule
REDUNDANT_COVERAGE = 0.8        # share of the question's distinctive words one brief sentence (or two adjacent ones) must contain
MIN_SHARED_TOKENS = 2           # distinctive words a disclosed fact must share with the question to count as targeted
UNDECLARED_COVERAGE = 0.5       # share of a fact's distinctive words a reply must contain to count as conveyed but undeclared
MIN_UNDECLARED_TOKENS = 3
SCRATCH_PREFIXES = ("/tmp/", "/tmp", "/dev/", "/var/tmp/", "/private/tmp/", "~/.cache/")
SHELL_TOOLS = {None, "", "bash", "shell", "sh", "run_command", "execute_command", "execute_bash", "terminal", "run_shell_command"}
EDITOR_TOOLS = {"str_replace_editor", "str_replace_based_edit_tool", "text_editor", "editor"}
EDITOR_WRITE_SUBCOMMANDS = {"create", "str_replace", "insert", "undo_edit", "write", "edit"}
WRITE_TOOLS = {"edit_file", "write_file", "create_file", "write_to_file", "replace_in_file", "apply_patch", "apply_diff",
               "insert_edit_into_file", "multi_edit", "edit", "write", "create"}

_STOP = frozenset("""
a an the and or of to in on for with is are was were be been being it its this that these those there here what which who
whom whose when where why how do does did should would could can may might must shall will not no yes if then else than as
at by from into onto over under about above below between both each either neither also just only very so such any all
some more most less least much many few other another same own too i me my we our you your he she they them their his her
etc e.g i.e vs via per up down out off again further once has have had having please want wants like use using used make
makes made get gets got let lets say says said mean means meant confirm confirming right correct correctly exactly still
really actually indeed currently now already ok okay thanks thank hi hello
""".split())
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_]+|[0-9]+(?:\.[0-9]+)+|[0-9]{2,}")
_SEGMENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_SHELL_SPLIT = re.compile(r"&&|\|\||;|\||\n")
# A redirect target sits on the same line (PDF `<< ... >>` dictionaries in heredocs are not redirects), is not preceded by
# `-` (`-> Scale:` annotations) and looks like a path or file name (`x > 320` comparisons do not).
_REDIRECT = re.compile(r"(?<![<>&\d-])>{1,2}[ \t]*([~./A-Za-z_][\w./~+-]*)(?![^\s;&|<>)])")
_PY_OPEN_WRITE = re.compile(r"open\(\s*([^,()]+?)\s*,\s*(?:mode\s*=\s*)?['\"](?:[wa]|r\+|w\+|a\+|wb|ab|wt|at|wb\+|ab\+)['\"]")
_PY_WRITE_METHOD = re.compile(r"\.write_(?:text|bytes)\(|shutil\.(?:copy|copy2|copyfile|move)\(|os\.(?:replace|rename)\(")
_PATH_LITERAL = re.compile(r"""['"]([^'"\s]*(?:/[^'"\s]*|\.[A-Za-z0-9]{1,5}))['"]""")
_SED_INPLACE = re.compile(r"^(?:-[a-zA-Z]*i(?:[a-zA-Z]*|\.\S*)?|--in-place(?:=\S*)?)$")
_PERL_INPLACE = re.compile(r"^-[a-zA-Z]*i(?:\S*)?$")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WRAPPERS = {"sudo", "env", "time", "nohup", "command", "exec"}


# ---------------------------------------------------------------- label models (interfaces §9.1)

class Label(Strict):
    """Strict in both senses: no extra keys and no type coercion, so a model's "yes" never becomes True."""
    model_config = ConfigDict(extra="forbid", strict=True)


class QuestionLabel(Label):
    seq: int
    specific: bool | None
    decision_oriented: bool | None
    before_implementation: bool | None
    redundant: bool | None
    fishing: bool | None
    reason: str


class ReplyLabel(Label):
    seq: int
    targeted: bool | None
    volunteered: list[str]
    test_or_identifier_disclosed: bool | None
    behaviour_level: bool | None
    reason: str
    hits: list[str] = Field(default_factory=list)        # forbidden terms the reply filter would have caught (mechanical evidence)
    undeclared: list[str] = Field(default_factory=list)  # visible facts the reply conveys without declaring them in disclosed_fact_ids
    ungrounded: list[str] = Field(default_factory=list)  # requirements without a grounding fact (test-derived by construction) the reply conveys


class PatienceSummary(Label):
    start: int
    end: int
    spent: int
    exchanges: list[dict]           # {seq, exchange, cost, remaining_after, tier}
    refusals: int
    duplicates: int
    sensible: bool


class ScenarioAudit(Label):
    scenario_id: str
    spec_hash: str | None
    coherent: bool | None
    fair_tests: bool | None
    plausible_withholding: bool | None
    reasons: dict[str, str]
    unfair_tests: list[str] = Field(default_factory=list)
    model: str | None
    audited_at: str
    rubric_version: str
    inputs_hash: str


class TrialAudit(Label):
    trial_id: str
    scenario_id: str | None
    spec_hash: str | None
    stakeholder_version: str | None     # "v1" | "v2" | None (no exchange to tell from)
    legacy: bool                        # stakeholder v1: labelled, never pooled with v2, not read by the model
    questions: list[QuestionLabel]
    replies: list[ReplyLabel]
    asked_before_implementing: bool | None
    useful_question_rate: float | None
    patience: PatienceSummary | None
    trajectory_source: str | None
    model: str | None
    audited_at: str
    rubric_version: str
    inputs_hash: str


class DisclosureCheck(Label):
    targeted: bool | None
    volunteered: list[str]
    undeclared: list[str]


def rubric_text() -> str:
    """The rubric rendered for prompts and docs: one line per label, in definition order."""
    return "\n".join(f"- {label}: {definition}" for label, definition in RUBRIC.items())


# ---------------------------------------------------------------- text helpers

def _stem(tok: str) -> str:
    if not tok.isalpha():
        return tok
    if tok.endswith("ies") and len(tok) > 4:
        return tok[:-3] + "y"
    if tok.endswith("ing") and len(tok) > 5:
        return tok[:-3]
    if tok.endswith("ed") and len(tok) > 4:
        return tok[:-2]
    if tok.endswith(("sses", "xes", "zes", "ches", "shes")) and len(tok) > 4:
        return tok[:-2]                       # classes -> class, fixes -> fix, matches -> match
    if tok.endswith("s") and len(tok) > 3 and not tok.endswith("ss"):
        return tok[:-1]                       # raises -> raise, floats -> float
    return tok


def content_tokens(text: str) -> set[str]:
    """Casefolded, lightly stemmed distinctive words of a text: no stop words, no one- or two-letter words."""
    out: set[str] = set()
    for tok in _TOKEN.findall(text or ""):
        low = tok.lower()
        if low in _STOP or (len(low) < 3 and not low.isdigit()):
            continue
        out.add(_stem(low))
    return out


def _segments(text: str) -> list[str]:
    return [s.strip() for s in _SEGMENT_SPLIT.split(text or "") if s and s.strip()]


def _messages(transcript: dict | None) -> list[dict]:
    events = (transcript or {}).get("events") or []
    msgs = [e for e in events if isinstance(e, dict) and e.get("kind") == "message"]
    return sorted(msgs, key=lambda e: e.get("seq", 0))


def _asks(transcript: dict | None) -> list[dict]:
    return [e for e in _messages(transcript) if e.get("role") == "user" and e.get("exchange") == "ask"]


# ---------------------------------------------------------------- edit detection

def _is_scratch(path: str) -> bool:
    p = path.strip("'\"")
    return p.startswith(SCRATCH_PREFIXES) or p in ("/dev/null", "&1", "&2")


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:  # unbalanced quotes inside a heredoc body: fall back to whitespace splitting
        return segment.split()


def _command_words(segment: str) -> list[str]:
    words = _tokens(segment)
    while words and (words[0] in _WRAPPERS or _ASSIGNMENT.match(words[0])):
        words = words[1:]
    return words


def _segment_evidence(segment: str) -> str | None:
    words = _command_words(segment)
    if not words:
        return None
    head, args = words[0].rsplit("/", 1)[-1], words[1:]
    if head == "sed":
        return "sed in-place" if any(_SED_INPLACE.match(a) for a in args) else None
    if head == "perl":
        flags = [a for a in args if a.startswith("-") and not a.startswith("--")]
        return "perl in-place" if any(_PERL_INPLACE.match(a) and a not in ("-e", "-E", "-n", "-p", "-l") for a in flags) else None
    if head == "patch":
        return "patch"
    if head == "git":
        sub = next((a for a in args if not a.startswith("-")), "")
        if sub in {"apply", "am", "cherry-pick", "revert", "merge", "restore"} or (sub == "checkout" and "--" in args):
            return f"git {sub}"
        return None
    if head == "tee":
        targets = [a for a in args if not a.startswith("-") and not _is_scratch(a)]
        return f"tee {targets[0]}" if targets else None
    if head in {"cp", "mv", "install", "rsync"}:
        targets = [a for a in args if not a.startswith("-")]
        return f"{head} to {targets[-1]}" if targets and not _is_scratch(targets[-1]) else None
    if head == "truncate":
        targets = [a for a in args if not a.startswith("-") and not _is_scratch(a)]
        return f"truncate {targets[0]}" if targets else None
    return None


PATH_AGNOSTIC_EVIDENCE = ("patch", "git apply", "git am", "git cherry-pick", "git revert", "git merge", "git restore", "git checkout")


def edit_evidence(command: str) -> str | None:
    """The rule under which a shell command writes to a non-scratch file, or None: ``>``/``>>`` redirection to a non-scratch
    path, Python code that opens a non-scratch path for writing (heredoc bodies included, so a fix script written to /tmp
    that rewrites a repository file counts), in-place sed/perl, patch/git apply, cp/mv/tee into the tree. Reads, greps, test
    runs, ``git diff`` and writes under /tmp or /dev are not edits."""
    text = command or ""
    for m in _REDIRECT.finditer(text):
        if not _is_scratch(m.group(1)):
            return f"redirect to {m.group(1)}"
    for m in _PY_OPEN_WRITE.finditer(text):
        target = m.group(1).strip()
        literal = target[1:-1] if len(target) >= 2 and target[0] in "'\"" and target[-1] == target[0] else None
        if literal is None or not _is_scratch(literal):
            return f"python open({target}) for writing"
    if _PY_WRITE_METHOD.search(text):
        literals = [p for p in _PATH_LITERAL.findall(text) if p]
        if not literals or any(not _is_scratch(p) for p in literals):
            return "python write_text/copy"
    for seg in _SHELL_SPLIT.split(text):
        if seg.strip():
            ev = _segment_evidence(seg)
            if ev:
                return ev
    return None


def is_edit_command(command: str) -> bool:
    """True when ``edit_evidence`` finds a write to a non-scratch file."""
    return edit_evidence(command) is not None


def _mentions_path(text: str, paths: list[str]) -> bool:
    for p in paths:
        base = p.rsplit("/", 1)[-1]
        if p in text or (base and re.search(r"(?<![\w/])" + re.escape(base) + r"(?![\w])", text)):
            return True
    return False


def _call_name(call: dict) -> str | None:
    name = call.get("function_name") or call.get("name")
    if name is None and isinstance(call.get("function"), dict):
        name = call["function"].get("name")
    return name


def _call_text(call: dict) -> str:
    args = call.get("arguments")
    if isinstance(args, dict) and isinstance(args.get("command"), str):
        return args["command"]
    if isinstance(args, str):
        return args
    return json.dumps(args or {})


def edit_call_evidence(call: dict, paths: list[str] | None = None) -> str | None:
    """Why an ATIF tool call counts as an implementation edit, or None. With ``paths`` (the submission's own files from
    ``report.applied_paths``/``dropped_paths``/``refused_paths``) a write must touch one of them, except patch/apply commands;
    scratch scripts and reproduction files the agent writes elsewhere are then not implementation. Without ``paths`` any
    non-scratch write counts."""
    name = _call_name(call)
    args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
    if name in SHELL_TOOLS:
        text = _call_text(call)
        ev = edit_evidence(text)
        if ev is None:
            return None
        if paths and not ev.startswith(PATH_AGNOSTIC_EVIDENCE) and not _mentions_path(text, paths):
            return None
        return ev
    if name in EDITOR_TOOLS:
        if args.get("command") not in EDITOR_WRITE_SUBCOMMANDS:
            return None
        target = str(args.get("path") or "")
        if paths and not _mentions_path(target, paths):
            return None
        return f"{name} {args.get('command')} {target}".strip()
    if name in WRITE_TOOLS:
        target = str(args.get("path") or args.get("file_path") or args.get("filename") or "")
        if paths and target and not _mentions_path(target, paths):
            return None
        return f"{name} {target}".strip()
    return None


def is_edit_call(call: dict, paths: list[str] | None = None) -> bool:
    return edit_call_evidence(call, paths) is not None


def _agent_steps(trajectory: dict | None) -> list[tuple[int, dict]]:
    steps = (trajectory or {}).get("steps") or []
    return [(i, s) for i, s in enumerate(steps) if isinstance(s, dict) and s.get("source", "agent") == "agent"]


def first_edit(trajectory: dict | None, paths: list[str] | None = None) -> tuple[int, str] | None:
    """(step index, evidence) of the agent's first implementation edit; None when there is none."""
    for i, s in _agent_steps(trajectory):
        for c in s.get("tool_calls") or []:
            if isinstance(c, dict):
                ev = edit_call_evidence(c, paths)
                if ev is not None:
                    return i, ev
    return None


def first_edit_step(trajectory: dict | None, paths: list[str] | None = None) -> int | None:
    """Index (position in ``steps``) of the first agent step with an editing tool call; None when there is none."""
    found = first_edit(trajectory, paths)
    return found[0] if found else None


def ask_steps(trajectory: dict | None) -> list[tuple[int, str]]:
    """(step index, command) of every ``ask-user`` invocation the agent actually made, in order."""
    out = []
    for i, s in _agent_steps(trajectory):
        for c in s.get("tool_calls") or []:
            if isinstance(c, dict) and _call_name(c) in SHELL_TOOLS:
                text = _call_text(c)
                if shim_invocation(text) == "ask-user":
                    out.append((i, text))
    return out


def first_ask_step(trajectory: dict | None) -> int | None:
    steps = ask_steps(trajectory)
    return steps[0][0] if steps else None


def _step_epoch(step: dict) -> float | None:
    ts = step.get("timestamp")
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _question_step(question: dict, asks: list[tuple[int, str]], ordinal: int) -> int | None:
    """Match a transcript question to its ask-user invocation: by text (the shim's argument is the question), else by order."""
    content = _norm_ws(question.get("content", ""))
    probe = content[:80]
    if probe:
        for i, text in asks:
            if probe in _norm_ws(text):
                return i
    if ordinal < len(asks):
        return asks[ordinal][0]
    return None


def questions_before_implementation(transcript: dict | None, trajectory: dict | None,
                                    paths: list[str] | None = None) -> dict[int, bool | None]:
    """Per question (keyed by the user event's ``seq``): was it asked before the agent's first implementation edit (see
    ``edit_call_evidence`` for ``paths``)? None without a trajectory or when the question cannot be placed in it."""
    asks = _asks(transcript)
    if not trajectory or not trajectory.get("steps"):
        return {int(q.get("seq", 0)): None for q in asks}
    edit = first_edit_step(trajectory, paths)
    edit_ts = _step_epoch(trajectory["steps"][edit]) if edit is not None else None
    invocations = ask_steps(trajectory)
    out: dict[int, bool | None] = {}
    for n, q in enumerate(asks):
        seq = int(q.get("seq", 0))
        step = _question_step(q, invocations, n)
        if step is not None:
            out[seq] = edit is None or step < edit
        elif edit is None:
            out[seq] = True
        elif isinstance(q.get("ts"), (int, float)) and edit_ts is not None:
            out[seq] = float(q["ts"]) < edit_ts
        else:
            out[seq] = None
    return out


def asked_before_implementing(transcript: dict | None, trajectory: dict | None, paths: list[str] | None = None) -> bool | None:
    """False when the agent asked nothing; otherwise whether its first ``ask`` preceded its first implementation edit or
    patch command in the trajectory (shim invocation order first, the stakeholder's timestamps against step timestamps when
    the shim call is missing from the trajectory); None when no trajectory is available. ``paths`` restricts edits to the
    submission's own files (``edit_call_evidence``)."""
    asks = _asks(transcript)
    if not asks:
        return False
    if not trajectory or not trajectory.get("steps"):
        return None
    edit = first_edit_step(trajectory, paths)
    ask_step = first_ask_step(trajectory)
    if ask_step is not None:
        return edit is None or ask_step < edit
    if edit is None:
        return True
    edit_ts = _step_epoch(trajectory["steps"][edit])
    ask_ts = [float(e["ts"]) for e in asks if isinstance(e.get("ts"), (int, float))]
    if edit_ts is None or not ask_ts:
        return None
    return min(ask_ts) < edit_ts


# ---------------------------------------------------------------- redundancy and disclosure

def redundant_with_brief(question: str, brief_text: str) -> bool:
    """True only when one brief sentence (or two adjacent ones) already contains at least ``REDUNDANT_COVERAGE`` of the
    question's distinctive words and the question has at least ``MIN_REDUNDANT_TOKENS`` of them: the agent asked the brief
    back. Whether the brief *answers* a differently worded question is the model's ``redundant`` judgement."""
    qt = content_tokens(question)
    if len(qt) < MIN_REDUNDANT_TOKENS:
        return False
    segs = _segments(brief_text)
    windows = segs + [f"{a} {b}" for a, b in zip(segs, segs[1:])]
    return any(len(qt & content_tokens(w)) / len(qt) >= REDUNDANT_COVERAGE for w in windows)


def disclosure_targeted(reply: str, question: str, disclosed_fact_ids: list[str], facts: list[dict]) -> DisclosureCheck:
    """Which disclosed facts the question asked about. A fact is targeted when it shares at least ``MIN_SHARED_TOKENS``
    distinctive words with the question; the rest (and unknown ids) are volunteered. ``targeted`` is None when nothing was
    disclosed. ``undeclared`` lists facts the reply conveys (>= ``UNDECLARED_COVERAGE`` of their words) without declaring."""
    by_id = {f.get("id"): f for f in facts if isinstance(f, dict)}
    qt, rt = content_tokens(question), content_tokens(reply)
    volunteered: list[str] = []
    for fid in disclosed_fact_ids or []:
        fact = by_id.get(fid)
        if fact is None or len(content_tokens(fact.get("text", "")) & qt) < MIN_SHARED_TOKENS:
            volunteered.append(fid)
    declared = set(disclosed_fact_ids or [])
    undeclared = []
    for fid, fact in by_id.items():
        if fid in declared or fid is None:
            continue
        ft = content_tokens(fact.get("text", ""))
        if len(ft) >= MIN_UNDECLARED_TOKENS and len(ft & rt) / len(ft) >= UNDECLARED_COVERAGE:
            undeclared.append(fid)
    targeted = None if not disclosed_fact_ids else not volunteered
    return DisclosureCheck(targeted=targeted, volunteered=volunteered, undeclared=undeclared)


def conveyed_requirements(reply: str, requirements: list[dict], *, ungrounded_only: bool = False) -> list[str]:
    """Ids of requirements whose distinctive words the reply covers (>= ``UNDECLARED_COVERAGE``); with ``ungrounded_only``
    just those with ``fact_id`` None, i.e. expectations that rest on the hidden tests alone."""
    rt = content_tokens(reply)
    out = []
    for r in requirements or []:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        if ungrounded_only and r.get("fact_id") is not None:
            continue
        toks = content_tokens(r.get("text", ""))
        if len(toks) >= MIN_UNDECLARED_TOKENS and len(toks & rt) / len(toks) >= UNDECLARED_COVERAGE:
            out.append(r["id"])
    return out


BUILTIN_NAMES = frozenset(dir(builtins))          # ValueError, TypeError, all, print...: generic names, never test-derived
_CODE_LIKE = re.compile(r"[_./()]|[a-z][A-Z]|^[A-Z][A-Za-z0-9]*[A-Z]")   # snake_case, dotted, paths, camelCase, CapWords


def test_or_identifier_disclosed(reply_text: str, forbidden_terms: list[str]) -> list[str]:
    """Forbidden terms present in the reply under the reply filter's own matching rules (whole identifiers, ``test_`` as a
    prefix, multi-word test vocabulary case-insensitively), except Python builtin names: a generic exception or type name
    is not a test-derived disclosure whoever says it. Empty when nothing the filter recognises is present."""
    terms = [t for t in forbidden_terms or [] if isinstance(t, str) and t and t not in BUILTIN_NAMES]
    return find_forbidden(reply_text or "", terms)


def code_like_hits(hits: list[str]) -> list[str]:
    """The hits that are unambiguously code or test vocabulary: identifiers with ``_``, ``.``, ``/`` or ``()``, camelCase or
    CapWords names, and the filter's fixed test vocabulary. Plain lowercase words that merely occur in a test file
    (``bounded``, ``reading``) are left for the reading pass to judge."""
    return [h for h in hits if h in FIXED_FORBIDDEN or _CODE_LIKE.search(h)]


# ---------------------------------------------------------------- patience

def patience_spent(transcript: dict | None) -> PatienceSummary:
    """Reconstruct how patience was spent from the frozen transcript: per reply the cost (drop in ``patience_remaining``),
    refusals (tier ``refused`` or the fixed refusal text) and repeated questions. ``sensible`` is the rubric's
    ``patience_sensible``: no refusal and no duplicate."""
    msgs = _messages(transcript)
    start = int((transcript or {}).get("patience_start") or 0)
    remaining = start
    exchanges: list[dict] = []
    seen: set[tuple[str, str]] = set()
    refusals = duplicates = 0
    users = {e.get("request_id"): e for e in msgs if e.get("role") == "user"}
    for e in msgs:
        if e.get("role") != "assistant":
            continue
        after = e.get("patience_remaining")
        after = int(after) if isinstance(after, int) else remaining
        cost = max(0, remaining - after)
        remaining = after
        refused = e.get("tier") == "refused" or e.get("content") == REFUSAL
        refusals += int(refused)
        user = users.get(e.get("request_id")) or {}
        key = (str(e.get("exchange")), _norm_ws(user.get("content", "")))
        if key[1]:
            if key in seen:
                duplicates += 1
            seen.add(key)
        exchanges.append({"seq": e.get("seq"), "exchange": e.get("exchange"), "cost": cost, "remaining_after": after,
                          "tier": e.get("tier")})
    end = (transcript or {}).get("patience_end")
    end = int(end) if isinstance(end, int) else remaining
    return PatienceSummary(start=start, end=end, spent=max(0, start - end), exchanges=exchanges, refusals=refusals,
                           duplicates=duplicates, sensible=(refusals == 0 and duplicates == 0))


# ---------------------------------------------------------------- aggregates

def useful(q: QuestionLabel) -> bool | None:
    """Specific, decision-oriented, not fishing, not redundant; None when any of those is unknown."""
    parts = (q.specific, q.decision_oriented, q.fishing, q.redundant)
    if any(p is None for p in parts):
        return None
    return bool(q.specific and q.decision_oriented and not q.fishing and not q.redundant)


def useful_question_rate(questions: list[QuestionLabel]) -> float | None:
    """Share of fully labelled questions that are useful; None when no question is fully labelled."""
    known = [u for u in (useful(q) for q in questions) if u is not None]
    return (sum(known) / len(known)) if known else None
