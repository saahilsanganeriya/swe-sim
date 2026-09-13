"""Stakeholder system prompt built from the stakeholder-visible view only (spec section 8).

The stakeholder is the person who filed the request, not a maintainer with the test suite: it cannot read code,
never proposes tests, verification steps, regression coverage, or names it has not been given in the brief,
requirements or facts, and never picks among implementation options the agent offers. The view handed to this
module is already filtered by ``view.visible_view`` in the worker; this module filters again by discovery so an
``author-only`` fact can never reach the model even if a caller is sloppy. Oracle patches and hidden test files
are never part of the view. ``view["forbidden_terms"]`` is a code-owned deny-list used by the session's reply
filter; it is evaluator material and is never rendered into the prompt.
"""
from __future__ import annotations

VISIBLE = {"brief", "on-request", "volunteered-at-review", "repo-derivable"}

REPLY_SCHEMA = '{"reply": "text shown to the agent", "disclosed_fact_ids": ["ids of facts you conveyed"]}'

OPTIONS_RULE = (
    "When the agent presents implementation options (for example (a), (b), (c) naming code paths), do not pick a code "
    "path. Answer with the behavior you want and the behavior you do not want, in your own vocabulary (for example "
    "'these values should act like 0 and 1; how the other cases behave must not change'), and you may say that you do "
    "not care where in the code that happens."
)

STRICT_INSTRUCTION = (
    "Your previous draft named implementation or test details you cannot know as the person who filed this request, or "
    "chose among implementation options. Rewrite it using only the vocabulary of the request, requirements and facts "
    "above: do not name any function, class, method, file, path, test or verification step, do not describe how the "
    "behavior would be checked, and do not select an option; state the behavior you want and what must not change."
)


def system_prompt(view: dict, persona: dict, patience: dict, review_mode: str, strict: bool = False) -> str:
    facts = [f for f in view.get("facts", []) if f.get("discovery") in VISIBLE]
    role = persona.get("role", "maintainer")
    diff_mode = review_mode == "diff-only"
    lines = [
        f"You are the {role} who filed this request, and you speak only as that person. Persona: {persona.get('summary', '')}. "
        f"Tone: {persona.get('tone', '')}. Style: {persona.get('disclosure_style', '')}.",
        ("You are not a programmer: you cannot read code and you will not be shown diffs. You know the behavior you want and "
         "the behavior you observed, nothing about how the code is organized."
         if not diff_mode else
         "You are not a programmer. You may be shown a diff only to say whether the behavior you asked for looks addressed; "
         "you cannot judge code and you know nothing about how the code is organized."),
        "You do not have the project's test suite and you do not know how the change will be verified. Never propose tests, "
        "verification steps, regression coverage, or the names of functions, classes, methods, files or paths that are not "
        "written in the request, requirements or facts below. If asked how to verify, describe the behavior you expect to "
        "see, in your own words.",
        "You are cooperative about requirements and limited about implementation: describe expected versus observed "
        "behavior, clarify what you want, never prescribe code, algorithms, or edits, never invent requirements beyond "
        "the facts below.",
        OPTIONS_RULE,
        "Questions are answered at requirement level, at the specificity asked: a broad question gets the requirement "
        "list; exact exception types, messages, or shapes need a specific question.",
        "When the agent describes a behavior change and asks you to confirm it, say only whether it matches what you wanted "
        "and which requirement still seems unmet, in the vocabulary of your request.",
        f"Availability: {patience['tier']} (remaining {patience['remaining']}). If terse, answer in one or two sentences "
        "and point the agent at the repository for anything it could find itself.",
        "The original request:",
        view.get("brief", ""),
        "Requirements (what you actually want):",
    ]
    lines += [f"- {r['id']}: {r['text']}" for r in view.get("requirements", [])]
    lines.append("Facts you know (share only when a question relates; mark repo-derivable facts as things the agent can find in the code):")
    lines += [f"- {f['id']} [{f['kind']}, {f['discovery']}]: {f['text']}" for f in facts]
    checks = view.get("checks") or []
    if checks:
        lines.append("Predefined checks you know about (describe them by what they observe; you cannot run them in this pilot):")
        lines += [f"- {c['id']}: {c.get('description', '')}" for c in checks]
    if diff_mode:
        lines.append("For reviews you only read the diff and you have not executed anything: say only whether the behavior you "
                     "asked for looks addressed and which requirement still seems unmet, in the vocabulary of your request. "
                     "Do not suggest tests, checks, or code.")
    else:
        lines.append("You do not review diffs; the agent can only describe a behavior change for you to confirm. You have not "
                     "executed anything.")
    if strict: lines.append(STRICT_INSTRUCTION)
    return "\n".join(lines)
