"""The merge decision table. Every row is one situation and the single step vigia must take."""

from dataclasses import replace

import pytest

from vigia import comments
from vigia.config import Caps, Config, default_tiers
from vigia.rules import PRState, Verdict, decide

H1, H2, H3 = "1" * 40, "2" * 40, "3" * 40
CFG = Config(repo="o/r", base="main", tiers=default_tiers("claude"))


def pr(**kw) -> PRState:
    base = dict(number=7, head=H1, tier="T0", builder_family="anthropic", ci="passing", mergeable="MERGEABLE")
    return PRState(**{**base, **kw})


def gate(sha=H1, verdict="APPROVE", p1=0, p2=0) -> Verdict:
    return Verdict("gate", sha, verdict, p1, p2)


def review(sha=H1, verdict="APPROVE", p1=0) -> Verdict:
    return Verdict("review", sha, verdict, p1)


def act(state, cfg=CFG):
    d = decide(state, cfg)
    return d.action, d.reason


# --- T0 happy path -------------------------------------------------------------------------


def test_t0_merges_on_green_ci_and_gate_approve_of_exact_head():
    d = decide(pr(verdicts=(gate(),)), CFG)
    assert (d.action, d.sha) == ("merge", H1)


def test_t0_with_only_p2_findings_merges():
    assert act(pr(verdicts=(gate(p2=3),)))[0] == "merge"


def test_gate_approve_of_an_older_head_does_not_count():
    action, reason = act(pr(head=H2, verdicts=(gate(sha=H1),)))
    assert action == "gate" and "round 2 on this head" in reason


def test_approve_that_lists_a_p1_is_read_as_changes():
    assert act(pr(verdicts=(gate(verdict="APPROVE", p1=1),)))[0] == "fix"


# --- CI comes first -------------------------------------------------------------------------


@pytest.mark.parametrize("ci", ["pending", "none"])
def test_no_gate_while_ci_is_not_green(ci):
    assert act(pr(ci=ci))[0] == "wait"


def test_no_ci_is_fine_when_ci_is_not_required():
    cfg = replace(CFG, require_ci=False)
    assert act(pr(ci="none"), cfg)[0] == "gate"
    assert act(pr(ci="none", verdicts=(gate(),)), cfg)[0] == "merge"


def test_red_ci_asks_for_a_fix_round():
    d = decide(pr(ci="failing"), CFG)
    assert (d.action, d.stage) == ("fix", "ci")


def test_red_ci_parks_when_fix_rounds_are_used_up():
    action, reason = act(pr(ci="failing", fix_rounds=2))
    assert action == "park" and "the cap of 2 fix rounds is reached" in reason


def test_a_green_ci_never_merges_without_a_gate():
    assert act(pr())[0] == "gate"


# --- gate rounds ----------------------------------------------------------------------------


def test_p1_on_round_one_asks_for_a_fix():
    d = decide(pr(verdicts=(gate(verdict="CHANGES", p1=2),)), CFG)
    assert (d.action, d.stage) == ("fix", "gate")


def test_round_two_runs_on_the_fixed_head():
    state = pr(head=H2, fix_rounds=1, verdicts=(gate(sha=H1, verdict="CHANGES", p1=1),))
    d = decide(state, CFG)
    assert (d.action, d.stage) == ("gate", "2")


def test_p1_on_round_two_parks_instead_of_a_third_round():
    state = pr(head=H2, fix_rounds=1, verdicts=(gate(H1, "CHANGES", 1), gate(H2, "CHANGES", 1)))
    action, reason = act(state)
    assert action == "park" and "gate round" in reason


def test_head_moving_after_the_last_gate_round_parks():
    state = pr(head=H3, fix_rounds=2, verdicts=(gate(H1, "CHANGES", 1), gate(H2, "APPROVE")))
    action, reason = act(state)
    assert action == "park" and "the cap of 2 gate rounds is reached" in reason


def test_round_two_approve_merges():
    state = pr(head=H2, fix_rounds=1, verdicts=(gate(H1, "CHANGES", 1), gate(H2, "APPROVE")))
    assert act(state)[0] == "merge"


def test_unavailable_gate_holds_and_retries_then_parks():
    one = pr(verdicts=(gate(verdict="UNAVAILABLE"),))
    assert act(one)[0] == "gate"
    three = pr(verdicts=tuple(gate(verdict="UNAVAILABLE") for _ in range(3)))
    action, reason = act(three)
    assert action == "park" and "GATE_UNAVAILABLE" in reason


def test_unavailable_never_counts_as_a_round_or_a_pass():
    state = pr(verdicts=(gate(verdict="UNAVAILABLE"), gate(verdict="UNAVAILABLE")))
    d = decide(state, CFG)
    assert (d.action, d.stage) == ("gate", "1")


def test_caps_are_configurable():
    cfg = replace(CFG, caps=Caps(gate_rounds=1))
    action, reason = act(pr(verdicts=(gate(verdict="CHANGES", p1=1),)), cfg)
    assert action == "park" and "a fix needs another gate round, and the cap of 1 is reached" in reason


# --- T1: + stronger reviewer ----------------------------------------------------------------


def test_t1_needs_the_reviewer_after_the_gate():
    assert act(pr(tier="T1", verdicts=(gate(),)))[0] == "review"


def test_t1_merges_with_gate_and_review_on_the_same_head():
    assert act(pr(tier="T1", verdicts=(gate(), review())))[0] == "merge"


def test_t1_review_of_an_older_head_does_not_count():
    state = pr(tier="T1", head=H2, fix_rounds=1, verdicts=(gate(H1), review(H1, "CHANGES", 1), gate(H2)))
    assert act(state)[0] == "review"


def test_t1_review_changes_asks_for_a_fix():
    d = decide(pr(tier="T1", verdicts=(gate(), review(verdict="CHANGES", p1=1))), CFG)
    assert (d.action, d.stage) == ("fix", "review")


# A vigia.toml may leave out T1's reviewer: the PR then merges on green CI plus the gate on the exact head.
NO_T1_REVIEWER = replace(CFG, tiers={**CFG.tiers, "T1": replace(CFG.tiers["T1"], reviewer=None)})


def test_t1_with_no_reviewer_merges_on_green_ci_and_gate_approve_of_exact_head():
    d = decide(pr(tier="T1", verdicts=(gate(),)), NO_T1_REVIEWER)
    assert (d.action, d.sha) == ("merge", H1)
    assert act(pr(tier="T1", verdicts=(gate(),)), CFG)[0] == "review"  # with a reviewer, the same PR waits


def test_t1_with_no_reviewer_still_needs_green_ci_and_the_gate_on_this_head():
    assert act(pr(tier="T1", ci="pending", verdicts=(gate(),)), NO_T1_REVIEWER)[0] == "wait"
    assert act(pr(tier="T1"), NO_T1_REVIEWER)[0] == "gate"
    assert act(pr(tier="T1", head=H2, verdicts=(gate(sha=H1),)), NO_T1_REVIEWER)[0] == "gate"
    assert act(pr(tier="T1", verdicts=(gate(verdict="CHANGES", p1=1),)), NO_T1_REVIEWER)[0] == "fix"


def test_reasons_read_as_plain_english():
    assert decide(pr(), CFG).reason == "round 1 on this head"
    assert decide(pr(tier="T1", verdicts=(gate(),)), CFG).reason == "T1 round 1 on this head"
    assert (
        decide(pr(tier="T1", verdicts=(gate(), review())), CFG).reason == "T1 requirements hold on this head"
    )
    assert str(decide(pr(), CFG)) == "gate: round 1 on this head"


# --- T2: + human label ------------------------------------------------------------------------


def test_t2_waits_for_the_human_label():
    action, reason = act(pr(tier="T2", verdicts=(gate(), review())))
    assert action == "wait" and "vigia:human-ok" in reason


def test_t2_merges_with_the_human_label():
    assert act(pr(tier="T2", human_ok=True, verdicts=(gate(), review())))[0] == "merge"


def test_t2_human_label_alone_merges_nothing():
    assert act(pr(tier="T2", human_ok=True))[0] == "gate"


# --- structure ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kw, needle",
    [
        ({"tier": None}, "no tier label"),
        ({"builder_family": None}, "unknown builder"),
        ({"builder_family": "openai"}, "builder's own lab"),
        ({"mergeable": "CONFLICTING"}, "conflicts"),
        ({"failures": 3}, "failed engine runs"),
    ],
)
def test_structural_problems_park(kw, needle):
    action, reason = act(pr(**kw))
    assert action == "park" and needle in reason


def test_an_opencode_builder_of_the_gate_s_own_lab_parks():
    line = f"## Handoff {H1}\nEngine: opencode · anthropic/claude-x · builder · lab openai\n\nNotes."
    family = comments.engine_family([{"author": "bot", "body": line}], "bot")
    claude_gates = Config(repo="o/r", base="main", tiers=default_tiers("codex"))
    action, reason = act(pr(builder_family=family, verdicts=(gate(),)), claude_gates)
    assert action == "park" and "builder's own lab" in reason
    assert act(pr(builder_family=family, verdicts=(gate(),)))[0] == "merge"  # a codex gate is another lab


@pytest.mark.parametrize("kw", [{"open": False}, {"parked": True}, {"draft": True}])
def test_closed_parked_and_draft_prs_are_left_alone(kw):
    assert act(pr(verdicts=(gate(),), **kw))[0] == "wait"


def test_unknown_mergeability_waits_instead_of_merging():
    assert act(pr(mergeable="UNKNOWN", verdicts=(gate(),)))[0] == "wait"


def test_every_merge_names_the_exact_head():
    for state in [pr(verdicts=(gate(),)), pr(tier="T1", verdicts=(gate(), review()))]:
        d = decide(state, CFG)
        assert d.action == "merge" and d.sha == state.head


def test_a_pr_aimed_at_another_base_parks_even_with_every_verdict_in():
    action, reason = act(pr(on_base=False, verdicts=(gate(),)))
    assert action == "park" and "base branch" in reason
