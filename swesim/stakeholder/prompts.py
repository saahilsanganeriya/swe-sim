"""Stakeholder system prompt built from the stakeholder-visible view only (spec section 8).

The view handed to this module is already filtered by ``view.visible_view`` in the worker; this module
filters again by discovery so an ``author-only`` fact can never reach the model even if a caller is sloppy.
Oracle patches and hidden test files are never part of the view, so they cannot appear here.
"""
from __future__ import annotations

VISIBLE = {"brief", "on-request", "volunteered-at-review", "repo-derivable"}

REPLY_SCHEMA = '{"reply": "text shown to the agent", "disclosed_fact_ids": ["ids of facts you conveyed"]}'


def system_prompt(view: dict, persona: dict, patience: dict, review_mode: str) -> str:
    facts = [f for f in view.get("facts", []) if f.get("discovery") in VISIBLE]
    role = persona.get("role", "maintainer")
    lines = [
        f"You are the {role} who owns this request. Persona: {persona.get('summary', '')}. "
        f"Tone: {persona.get('tone', '')}. Style: {persona.get('disclosure_style', '')}.",
        "You are cooperative about requirements and limited about implementation: describe expected versus observed "
        "behavior, clarify what you want, never prescribe code, algorithms, or edits, never invent requirements beyond "
        "the facts below.",
        "Answer at the level of specificity asked: a broad question gets the requirement list; exact exception types, "
        "messages, or shapes need a specific question.",
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
    if review_mode == "diff-only":
        lines.append("For reviews you only read the diff; say explicitly that you have not executed anything.")
    else:
        lines.append("For reviews you may run predefined checks; report what you observed, not how to fix it.")
    return "\n".join(lines)
