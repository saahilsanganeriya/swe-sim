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
    c = StakeholderConfig.model_validate({"patience_profile": {"start": 6}, "max_tool_calls_per_activation": 2})
    s = Session("t1", VIEW, c); llm = FakeLLM(ids=[])
    diff = "".join(f"diff --git a/f{i}.py b/f{i}.py\n--- a/f{i}.py\n+++ b/f{i}.py\n@@ -1 +1 @@\n-a\n+b\n" for i in range(3))
    r = asyncio.run(s.review("v1", diff, {f"f{i}.py": "b\n" for i in range(3)}, llm))
    assert len(r.tool_calls) == 2 and r.tool_calls[0]["tool"] == "read_diff"


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
