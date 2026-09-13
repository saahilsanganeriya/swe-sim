"""Session protocol: patience tiers, request-id idempotency, review, freeze, snapshot/restore."""
import asyncio, hashlib, json

import pytest

from swesim.models import StakeholderConfig
from swesim.stakeholder.session import REFUSAL, Reply, Session

VIEW = {"brief": "It breaks.",
        "facts": [{"id": "f01", "text": "Raise ValueError, not KeyError", "discovery": "on-request", "kind": "interface"},
                  {"id": "f04", "text": "weighted.py holds the code", "discovery": "repo-derivable", "kind": "context"}],
        "requirements": [{"id": "r01", "text": "Raise ValueError"}],
        "persona": {"summary": "maintainer", "tone": "terse", "disclosure_style": "answers what is asked", "role": "maintainer"},
        "checks": []}


class FakeLLM:
    def __init__(self, reply="ValueError.", ids=("f01",)):
        self.calls = 0; self.reply = reply; self.ids = list(ids); self.seen = []

    def __call__(self, system, messages, schema):
        self.calls += 1; self.seen.append((system, messages, schema))
        return {"reply": self.reply, "disclosed_fact_ids": self.ids}


def cfg(**patience):
    base = {"start": 3, "question_cost": 1, "review_cost": 2, "terse_below": 2, "refuse_at": 0}
    return StakeholderConfig.model_validate({"patience_profile": {**base, **patience}})


def test_patience_tiers_and_idempotency():
    s = Session("t1", VIEW, cfg()); llm = FakeLLM()
    r1 = asyncio.run(s.ask("q1", "Which exception?", llm))
    assert r1.reply == "ValueError." and r1.patience_remaining == 2 and r1.disclosed_fact_ids == ["f01"] and r1.tier == "full"
    r1b = asyncio.run(s.ask("q1", "Which exception?", llm)); assert r1b.reply == r1.reply and llm.calls == 1   # cached by request_id
    r2 = asyncio.run(s.ask("q2", "And the message?", llm)); assert r2.patience_remaining == 1 and r2.tier == "terse"
    r3 = asyncio.run(s.ask("q3", "More?", llm)); assert r3.patience_remaining == 0 and r3.tier == "terse" and llm.calls == 3
    h = asyncio.run(s.freeze()); assert len(h) == 64
    with pytest.raises(Session.Frozen): asyncio.run(s.ask("q4", "x", llm))


def test_refusal_is_fixed_and_free():
    s = Session("t1", VIEW, cfg(start=1, terse_below=1)); llm = FakeLLM()
    r1 = asyncio.run(s.ask("q1", "Which exception?", llm)); assert r1.tier == "terse" and r1.patience_remaining == 0
    r2 = asyncio.run(s.ask("q2", "Anything else?", llm))
    assert r2.tier == "refused" and r2.reply == REFUSAL and r2.disclosed_fact_ids == [] and r2.patience_remaining == 0
    assert llm.calls == 1                       # no model call at or below refuse_at
    # A review whose cost cannot be covered is refused too, without spending.
    s2 = Session("t2", VIEW, cfg(start=1, terse_below=1)); llm2 = FakeLLM()
    r = asyncio.run(s2.review("v1", "diff --git a/x b/x\n", {}, llm2))
    assert r.tier == "refused" and r.patience_remaining == 1 and llm2.calls == 0
    # Refusals are still recorded in the transcript with the request id.
    assert [e["request_id"] for e in s.events if e["kind"] == "message"] == ["q1", "q1", "q2", "q2"]


def test_request_id_binds_to_body():
    s = Session("t1", VIEW, cfg()); llm = FakeLLM()
    asyncio.run(s.ask("q1", "Which exception?", llm))
    with pytest.raises(Session.Conflict): asyncio.run(s.ask("q1", "A different question", llm))
    with pytest.raises(Session.Conflict): asyncio.run(s.review("q1", "diff", {}, llm))
    assert llm.calls == 1


def test_review_records_sha_and_unverified_tool_call():
    s = Session("t1", VIEW, cfg()); llm = FakeLLM(reply="Looks incomplete: r01 not addressed.", ids=[])
    diff = "diff --git a/weighted.py b/weighted.py\n--- a/weighted.py\n+++ b/weighted.py\n@@ -1 +1 @@\n-raise KeyError\n+raise ValueError\n"
    r = asyncio.run(s.review("v1", diff, {"weighted.py": "raise ValueError\n", "unrelated.py": "x"}, llm))
    assert r.reviewed_diff_sha256 == hashlib.sha256(diff.encode()).hexdigest()
    assert r.patience_remaining == 1 and r.tier == "full"                 # review_cost 2 from 3
    assert r.tool_calls[0]["tool"] == "read_diff" and r.tool_calls[0]["verified"] is False and r.tool_calls[0]["sha256"] == r.reviewed_diff_sha256
    assert any(c["tool"] == "read_changed_file" and c["path"] == "weighted.py" and c["verified"] is False for c in r.tool_calls)
    assert not any(c.get("path") == "unrelated.py" for c in r.tool_calls)   # only paths present in the diff
    system, messages, _ = llm.seen[0]
    assert "not verified" in messages[-1]["content"] and diff in messages[-1]["content"] and "have not executed" in system
    ev = [e for e in s.events if e["kind"] == "message"]
    assert ev[0]["role"] == "user" and ev[0]["exchange"] == "review" and ev[1]["role"] == "assistant"
    assert ev[1]["reviewed_diff_sha256"] == r.reviewed_diff_sha256 and ev[1]["tool_calls"] == r.tool_calls
    # Public reply shape: no fact annotations, no tool audit; sha only for reviews.
    assert set(r.public()) == {"reply", "patience_remaining", "tier", "reviewed_diff_sha256"}
    a = asyncio.run(s.ask("q1", "Which exception?", llm)); assert set(a.public()) == {"reply", "patience_remaining", "tier"}
    with pytest.raises(ValueError): asyncio.run(s.review("v2", "x" * 200_001, {}, llm))
    with pytest.raises(ValueError): asyncio.run(s.review("v3", diff, {}, llm, tools=object()))   # checks review deferred


def test_review_tool_calls_bounded_by_config():
    """read_diff counts toward max_tool_calls_per_activation; a budget of 0 makes reviews unavailable."""
    diff = "".join(f"diff --git a/f{i}.py b/f{i}.py\n--- a/f{i}.py\n+++ b/f{i}.py\n@@ -1 +1 @@\n-a\n+b\n" for i in range(3))
    files = {f"f{i}.py": "b\n" for i in range(3)}
    def mk(n): return Session("t1", VIEW, StakeholderConfig.model_validate({"patience_profile": {"start": 6}, "max_tool_calls_per_activation": n}))
    r = asyncio.run(mk(2).review("v1", diff, files, FakeLLM(ids=[])))
    assert len(r.tool_calls) == 2 and r.tool_calls[0]["tool"] == "read_diff"
    r1 = asyncio.run(mk(1).review("v1", diff, files, FakeLLM(ids=[]))); assert [c["tool"] for c in r1.tool_calls] == ["read_diff"]
    r4 = asyncio.run(mk(4).review("v1", diff, files, FakeLLM(ids=[]))); assert len(r4.tool_calls) == 4
    s0 = mk(0); llm0 = FakeLLM()
    with pytest.raises(Session.BadRequest): asyncio.run(s0.review("v1", diff, files, llm0))
    assert llm0.calls == 0 and s0.events == [] and s0.remaining == 6


def test_bad_request_is_distinct_from_service_errors():
    """Tool rejections of agent input raise BadRequest (a ValueError) and leave the session untouched."""
    s = Session("t1", VIEW, cfg()); llm = FakeLLM()
    with pytest.raises(Session.BadRequest): asyncio.run(s.review("v1", "é" * 150_000, {}, llm))   # 150k chars, 300k bytes
    assert issubclass(Session.BadRequest, ValueError) and llm.calls == 0 and s.events == [] and s.cache == {}
    with pytest.raises(ValueError) as ei: asyncio.run(s.ask("q1", "x", FakeLLM(reply="")))        # model failure is not a BadRequest
    assert not isinstance(ei.value, Session.BadRequest)


def test_lookup_reports_frozen_cached_and_conflict_without_side_effects():
    s = Session("t1", VIEW, cfg()); llm = FakeLLM()
    assert s.lookup("q1", "ask", "Which exception?") is None
    r = asyncio.run(s.ask("q1", "Which exception?", llm))
    assert s.lookup("q1", "ask", "Which exception?") == r
    with pytest.raises(Session.Conflict): s.lookup("q1", "ask", "other")
    with pytest.raises(Session.Conflict): s.lookup("q1", "review", "Which exception?", {})
    n = len(s.events); asyncio.run(s.freeze())
    with pytest.raises(Session.Frozen): s.lookup("q9", "ask", "x")
    assert len(s.events) == n and llm.calls == 1


def test_prior_review_bodies_are_stubbed_in_model_history_but_kept_in_transcript():
    s = Session("t1", VIEW, cfg(start=9, terse_below=0)); llm = FakeLLM(ids=[])
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+SECRET_DIFF_LINE\n"
    r = asyncio.run(s.review("v1", diff, {"a.py": "SECRET_FILE_BODY\n"}, llm))
    asyncio.run(s.ask("q1", "Is that enough?", llm))
    _, messages, _ = llm.seen[1]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[0]["content"] == f"[review request v1: diff sha256 {r.reviewed_diff_sha256}]"
    assert "SECRET_DIFF_LINE" not in json.dumps(messages) and "SECRET_FILE_BODY" not in json.dumps(messages)
    user_review = next(e for e in s.events if e["kind"] == "message" and e["role"] == "user" and e["exchange"] == "review")
    assert "SECRET_DIFF_LINE" in user_review["content"] and "SECRET_FILE_BODY" in user_review["content"] and user_review["reviewed_diff_sha256"] == r.reviewed_diff_sha256
    # The review's own request still carried the full body to the model.
    assert "SECRET_DIFF_LINE" in llm.seen[0][1][-1]["content"]


def test_disclosed_ids_filtered_and_empty_reply_rejected():
    s = Session("t1", VIEW, cfg()); llm = FakeLLM(ids=["f01", "f09", "f01"])
    r = asyncio.run(s.ask("q1", "Which exception?", llm)); assert r.disclosed_fact_ids == ["f01"]
    bad = FakeLLM(reply="")
    with pytest.raises(ValueError): asyncio.run(s.ask("q2", "x", bad))
    assert s.remaining == 2 and not any(e.get("request_id") == "q2" for e in s.events)   # failed exchange spends nothing


def test_transcript_event_shape_and_history():
    s = Session("t1", VIEW, cfg()); llm = FakeLLM()
    s._record("session", spec_hash="h", condition="interactive", run_config_hash="r")
    asyncio.run(s.ask("q1", "Which exception?", llm)); asyncio.run(s.ask("q2", "And the message?", llm))
    user, asst = [e for e in s.events if e["kind"] == "message"][:2]
    assert {"kind", "role", "exchange", "request_id", "ts", "seq", "content", "actor"} <= set(user)
    assert {"disclosed_fact_ids", "tool_calls", "tier", "patience_remaining", "reviewed_diff_sha256", "delivery"} <= set(asst)
    assert asst["delivery"] == "generated" and asst["actor"] == "stakeholder" and user["actor"] == "coding-agent"
    assert [e["seq"] for e in s.events] == list(range(len(s.events)))
    # Second call sees the prior exchange as history and the terse instruction.
    system, messages, schema = llm.seen[1]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"] and messages[0]["content"] == "Which exception?"
    assert "terse" in system and "remaining 2" in system and "SECRET" not in system and schema
    assert "f04 [context, repo-derivable]" in system


def test_freeze_is_idempotent_and_hashes_events():
    s = Session("t1", VIEW, cfg()); llm = FakeLLM()
    asyncio.run(s.ask("q1", "Which exception?", llm))
    h1 = asyncio.run(s.freeze()); h2 = asyncio.run(s.freeze())
    assert h1 == h2 == hashlib.sha256(json.dumps(s.events, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with pytest.raises(Session.Frozen): asyncio.run(s.review("v1", "d", {}, llm))
    d = s.to_dict()
    assert d["frozen"] is True and d["transcript_hash"] == h1 and d["patience_start"] == 3 and d["patience_end"] == 2 and d["trial_id"] == "t1"


def test_snapshot_restore_roundtrip_preserves_cache_and_budget():
    s = Session("t1", VIEW, cfg(), image="ubuntu:24.04"); llm = FakeLLM()
    asyncio.run(s.ask("q1", "Which exception?", llm))
    snap = json.loads(json.dumps(s.snapshot()))          # must be JSON-serializable
    s2 = Session.restore(snap)
    assert s2.remaining == 2 and s2.image == "ubuntu:24.04" and s2.events == s.events and s2.frozen is False
    r = asyncio.run(s2.ask("q1", "Which exception?", llm)); assert r.reply == "ValueError." and llm.calls == 1
    h = asyncio.run(s2.freeze()); s3 = Session.restore(s2.snapshot())
    assert s3.frozen and asyncio.run(s3.freeze()) == h


def test_system_prompt_excludes_author_only():
    from swesim.stakeholder.prompts import system_prompt
    view = dict(VIEW); view["facts"] = VIEW["facts"] + [{"id": "f09", "text": "SECRET", "discovery": "author-only", "kind": "interface"}]
    out = system_prompt(view, view["persona"], {"tier": "full", "remaining": 3}, "diff-only")
    assert "SECRET" not in out and "f09" not in out and "f01" in out and "r01" in out and "It breaks." in out


def test_system_prompt_mentions_checks_by_description_only():
    from swesim.stakeholder.prompts import system_prompt
    view = dict(VIEW); view["checks"] = [{"id": "c1", "description": "Run the reporter's script", "command": "python secret_cmd.py", "expected_observation": "prints ok"}]
    out = system_prompt(view, view["persona"], {"tier": "terse", "remaining": 1}, "diff-only")
    assert "Run the reporter's script" in out and "secret_cmd" not in out and "one or two sentences" in out


def test_reply_dataclass_defaults():
    r = Reply("x", [], 1, "full"); assert r.tool_calls == [] and r.reviewed_diff_sha256 is None
    assert r.redacted_terms == [] and r.redaction_count == 0 and r.regenerated is False and r.withheld_sentences == []


# -- code-owned reply filter (forbidden terms from the hidden tests) --------------------------------------------------
TERMS = ["Continuous", "IntervalProperty", "Series", "assert", "pytest", "test_", "_setup(", "tests/"]
VIEW_F = {**VIEW, "forbidden_terms": TERMS}
LEAK = ("The change looks right. I'd want the regression to cover calling Continuous()._setup on a boolean pandas Series "
        "and observing mapped output [1, 0, 0]. The original case should still not raise.")
CLEAN = "The change looks right. Please make sure the original case still does not raise."


class SeqLLM:
    """Returns the scripted replies in order (the last one repeats) and records every call."""
    def __init__(self, *replies): self.replies = list(replies); self.calls = 0; self.seen = []
    def __call__(self, system, messages, schema):
        self.seen.append((system, messages, schema)); r = self.replies[min(self.calls, len(self.replies) - 1)]; self.calls += 1
        return {"reply": r, "disclosed_fact_ids": ["f01"]}


def test_term_patterns_match_tokens_prefixes_and_literals():
    from swesim.stakeholder.session import find_forbidden
    assert find_forbidden("a TimeSeries object", ["Series"]) == []                      # whole-token identifiers
    assert find_forbidden("a pandas Series object", ["Series"]) == ["Series"]
    assert find_forbidden("see test_bool_series", ["test_"]) == ["test_"]              # trailing underscore is a prefix
    assert find_forbidden("call Continuous()._setup(x)", ["_setup("]) == ["_setup("]  # literal substring
    assert find_forbidden("in tests/test_scales.py", ["tests/"]) == ["tests/"]
    assert find_forbidden("nothing here", TERMS) == []
    assert find_forbidden("Please add a Regression Test for this.", ["regression test"]) == ["regression test"]   # phrases: any case
    assert find_forbidden("this is a regression from 0.12", ["regression test"]) == []                            # the reporter may say "regression"
    assert find_forbidden("I'd want a test that shows it", ["a test that"]) == ["a test that"]


def test_forbidden_reply_is_regenerated_then_redacted():
    s = Session("t1", VIEW_F, cfg(start=6, terse_below=0)); llm = SeqLLM(LEAK, LEAK)
    r = asyncio.run(s.ask("q1", "Anything else you need?", llm))
    assert llm.calls == 2
    from swesim.stakeholder.prompts import STRICT_INSTRUCTION
    assert STRICT_INSTRUCTION not in llm.seen[0][0] and STRICT_INSTRUCTION in llm.seen[1][0]   # second attempt is stricter
    assert r.reply == "The change looks right. [withheld] The original case should still not raise."
    assert r.regenerated is True and r.redaction_count == 1 and r.redacted_terms == ["Continuous", "Series"]   # "._setup on": no "(" so the literal does not fire
    assert r.withheld_sentences == ["I'd want the regression to cover calling Continuous()._setup on a boolean pandas Series and observing mapped output [1, 0, 0]."]
    for t in ("Continuous", "IntervalProperty", "_setup(", "Series"): assert t not in r.reply
    assert set(r.public()) == {"reply", "patience_remaining", "tier"} and r.disclosed_fact_ids == ["f01"] and r.patience_remaining == 5
    ev = [e for e in s.events if e["kind"] == "message" and e["role"] == "assistant"][0]
    assert ev["content"] == r.reply and ev["redacted_terms"] == r.redacted_terms and ev["redaction_count"] == 1 and ev["regenerated"] is True
    assert ev["withheld_sentences"] == r.withheld_sentences
    # The redacted text (never the draft) is what later turns see as history, and the deny-list is not in the prompt.
    asyncio.run(s.ask("q2", "And?", llm)); system, messages, _ = llm.seen[-1]
    assert messages[1]["content"] == r.reply and "IntervalProperty" not in system and "forbidden" not in system.lower()


def test_clean_reply_passes_unchanged():
    s = Session("t1", VIEW_F, cfg()); llm = SeqLLM(CLEAN)
    r = asyncio.run(s.ask("q1", "Anything else?", llm))
    assert llm.calls == 1 and r.reply == CLEAN and r.redaction_count == 0 and r.redacted_terms == [] and r.regenerated is False
    ev = [e for e in s.events if e["kind"] == "message" and e["role"] == "assistant"][0]
    assert ev["redacted_terms"] == [] and ev["redaction_count"] == 0 and ev["regenerated"] is False and ev["withheld_sentences"] == []


def test_leak_then_clean_regeneration_keeps_the_second_draft():
    s = Session("t1", VIEW_F, cfg()); llm = SeqLLM(LEAK, CLEAN)
    r = asyncio.run(s.ask("q1", "Anything else?", llm))
    assert llm.calls == 2 and r.reply == CLEAN and r.regenerated is True and r.redaction_count == 0 and r.redacted_terms == []


def test_regeneration_failure_falls_back_to_redacting_the_first_draft():
    class Flaky(SeqLLM):
        def __call__(self, system, messages, schema):
            if self.calls == 1: self.calls += 1; raise RuntimeError("provider hiccup")
            return super().__call__(system, messages, schema)
    s = Session("t1", VIEW_F, cfg()); llm = Flaky(LEAK)
    r = asyncio.run(s.ask("q1", "Anything else?", llm))
    assert llm.calls == 2 and r.regenerated is False and r.redaction_count == 1 and "[withheld]" in r.reply and "Continuous" not in r.reply


def test_review_reply_is_filtered_too_and_no_terms_means_no_filter():
    s = Session("t1", VIEW_F, cfg(start=6, terse_below=0)); llm = SeqLLM(LEAK)
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
    r = asyncio.run(s.review("v1", diff, {}, llm)); assert r.redaction_count == 1 and "Continuous" not in r.reply and r.reviewed_diff_sha256
    s2 = Session("t2", VIEW, cfg()); llm2 = SeqLLM(LEAK)                       # no deny-list: nothing is scanned
    r2 = asyncio.run(s2.ask("q1", "x", llm2)); assert llm2.calls == 1 and r2.reply == LEAK and r2.redaction_count == 0
    # Redaction of an all-forbidden reply leaves a single [withheld] marker, never an empty reply.
    s3 = Session("t3", VIEW_F, cfg()); r3 = asyncio.run(s3.ask("q1", "x", SeqLLM("Use Continuous()._setup(x).")))
    assert r3.reply == "[withheld]" and r3.redaction_count == 1


def test_system_prompt_speaks_as_the_reporter_and_never_lists_forbidden_terms():
    from swesim.stakeholder.prompts import OPTIONS_RULE, STRICT_INSTRUCTION, system_prompt
    out = system_prompt(VIEW_F, VIEW_F["persona"], {"tier": "full", "remaining": 3}, "diff-only")
    assert "who filed this request" in out and "Never propose tests" in out and "have not executed" in out
    assert "which requirement still seems unmet" in out and "requirement level" in out and OPTIONS_RULE in out
    for t in TERMS: assert t not in out, t
    assert STRICT_INSTRUCTION not in out and STRICT_INSTRUCTION in system_prompt(VIEW_F, VIEW_F["persona"], {"tier": "full", "remaining": 3}, "diff-only", strict=True)
    beh = system_prompt(VIEW_F, VIEW_F["persona"], {"tier": "full", "remaining": 3}, "behavior")
    assert "cannot read code and you will not be shown diffs" in beh and "do not review diffs" in beh and "have not executed" in beh
    assert "cannot read code and you will not be shown diffs" not in out       # diff-only mode is shown the diff, still not a programmer


def test_option_picks_are_filtered_like_identifiers():
    """Agent offers (a)/(b)/(c) code paths; the model echoes a pick plus internal names: regenerated, then rewritten so no
    option letter selection and no identifier absent from the brief/facts survives."""
    from swesim.stakeholder.session import option_markers
    view = {**VIEW, "forbidden_terms": ["Color", "Property", "ContinuousBase", "default_scale", "_setup("]}
    question = ("Which do you prefer? (a) change Color.default_scale, (b) change Property.default_scale, "
                "(c) cast in ContinuousBase._setup.")
    assert option_markers(question) == ["(a)", "(b)", "(c)"]
    pick = ("I'd go with (c): cast in ContinuousBase._setup so booleans act like 0 and 1. Option (a) is too broad. "
            "Boolean color values should act like 0 and 1 on a continuous scale; how scales are inferred for other properties must not change.")
    s = Session("t1", view, cfg(start=6, terse_below=0)); llm = SeqLLM(pick, pick)
    r = asyncio.run(s.ask("q1", question, llm))
    assert llm.calls == 2 and r.regenerated is True and r.redaction_count == 2
    assert r.reply == "[withheld] [withheld] Boolean color values should act like 0 and 1 on a continuous scale; how scales are inferred for other properties must not change."
    assert option_markers(r.reply) == [] and not any(t in r.reply for t in view["forbidden_terms"])
    assert r.redacted_terms == ["(a)", "(c)", "ContinuousBase"]
    # "Option B" phrasing counts as a pick too; a reply that is only a pick becomes a single [withheld].
    s2 = Session("t2", view, cfg()); r2 = asyncio.run(s2.ask("q1", question, SeqLLM("Option B, please.")))
    assert r2.reply == "[withheld]" and r2.redacted_terms == ["(b)"]
    # Without options in the agent's message, "(a)" in a reply is ordinary text.
    s3 = Session("t3", view, cfg()); r3 = asyncio.run(s3.ask("q1", "What exactly is wrong?", SeqLLM("See item (a) of my report: it breaks.")))
    assert r3.redaction_count == 0 and "(a)" in r3.reply


def _behavior_cfg(**patience):
    """A StakeholderConfig in behavior mode. Built without validation while models.py still pins review_mode to 'diff-only'."""
    from swesim.models import PatienceProfile
    base = {"start": 3, "question_cost": 1, "review_cost": 2, "terse_below": 2, "refuse_at": 0}
    return StakeholderConfig.model_construct(model_id="openai/gpt-5.5", prompt_version="v1", temperature=0.0,
                                             patience_profile=PatienceProfile(**{**base, **patience}), review_mode="behavior",
                                             max_tool_calls_per_activation=2)


def test_confirm_costs_review_cost_is_filtered_and_recorded_as_confirm():
    s = Session("t1", VIEW_F, cfg(start=4, terse_below=1)); llm = SeqLLM(LEAK)
    r = asyncio.run(s.confirm("c1", "Boolean values now act like 0 and 1 on a continuous scale; before, plotting them raised an error.", llm))
    assert r.patience_remaining == 2 and r.tier == "full" and r.redaction_count == 1 and "Continuous" not in r.reply and r.reviewed_diff_sha256 is None
    ev = [e for e in s.events if e["kind"] == "message"]
    assert ev[0]["exchange"] == "confirm" and ev[0]["role"] == "user" and ev[0]["content"].endswith("raised an error.") and "reviewed_diff_sha256" not in ev[0]
    assert ev[1]["exchange"] == "confirm" and set(ev[1]) >= {"disclosed_fact_ids", "tool_calls", "redacted_terms", "redaction_count", "regenerated", "withheld_sentences"}
    assert ev[1]["tool_calls"] == [] and set(r.public()) == {"reply", "patience_remaining", "tier"}
    assert "do not name code" in llm.seen[0][1][-1]["content"]                    # confirmation framing precedes the description
    cached = asyncio.run(s.confirm("c1", "Boolean values now act like 0 and 1 on a continuous scale; before, plotting them raised an error.", llm))
    assert cached.reply == r.reply and llm.calls == 2
    with pytest.raises(Session.Conflict): asyncio.run(s.confirm("c1", "something else", llm))
    with pytest.raises(Session.BadRequest): asyncio.run(s.confirm("c2", "x" * 4001, llm))
    with pytest.raises(Session.BadRequest): asyncio.run(s.confirm("c3", "   ", llm))
    assert llm.calls == 2 and s.remaining == 2


def test_diff_review_is_disabled_unless_review_mode_is_diff_only():
    s = Session("t1", VIEW, _behavior_cfg()); llm = FakeLLM()
    assert s.diff_review_enabled is False
    with pytest.raises(Session.Unavailable): asyncio.run(s.review("v1", "diff --git a/a b/a\n", {}, llm))
    assert llm.calls == 0 and s.events == [] and s.remaining == 3
    ok = asyncio.run(s.confirm("c1", "It no longer breaks on boolean input.", llm)); assert ok.tier == "full" and s.remaining == 1
    assert Session("t2", VIEW, cfg()).diff_review_enabled is True                 # explicit diff-only keeps the channel
