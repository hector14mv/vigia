"""End to end on a real local git remote: issue -> PR -> gate -> fix -> gate -> merge.

GitHub and the engines are fakes (tests/fakes.py); git, worktrees, commits and pushes are real, all
inside tmp_path.
"""

import json
from dataclasses import replace

import pytest

from fakes import APPROVE, CHANGES, Clock, FakeGitHub, engine, make_repo
from vigia import comments
from vigia.config import Config, default_tiers
from vigia.context import Ctx
from vigia.engines import Engines
from vigia.git import Git
from vigia.ledger import Ledger
from vigia.proc import CommandError
from vigia.watch import merge, pending_follow_ups, sense, tick

TICKET = """\
## Outcome
`textkit` gains a helper.

## Scope
`textkit.py`.
Not touched: `shout()`.
Must still hold: `shout("a") == "A"`.

## Acceptance criteria
- [ ] the helper exists

## Done-test
`python -c "import textkit"` works.

## Risks
None.
"""


@pytest.fixture
def world(tmp_path):
    origin, clone = make_repo(tmp_path)
    clock = Clock()
    gh = FakeGitHub(origin, clock=clock)
    cfg = Config(repo=gh.repo, base="main", tiers=default_tiers("claude"), test_command="true")

    def build(verdicts, cfg=cfg):
        run = engine(verdicts)
        ctx = Ctx(
            cfg=cfg,
            clone=clone,
            gh=gh,
            git=Git(clone, tmp_path / "work"),
            ledger=Ledger(clone / ".vigia" / "ledger.jsonl", clock=clock),
            engines=Engines(cfg, clone / ".vigia" / "logs", runner=run, root=tmp_path / "work"),
            root=tmp_path / "work",
            say=lambda _: None,
        )
        (tmp_path / "work").mkdir(exist_ok=True)
        return ctx, run

    return gh, build


def decisions(seen):
    return [(s.pr["number"], s.decision.action) for s in seen]


def test_t0_issue_to_merged_pr_with_one_fix_round(world):
    gh, build = world
    ctx, run = build([CHANGES, APPROVE])
    n = gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])

    assert tick(ctx) == []  # no PRs yet: this tick builds the issue
    pr = next(iter(gh.prs_))
    head1 = gh.branch_sha(gh.prs_[pr]["branch"])
    assert gh.prs_[pr]["body"].startswith(f"Closes #{n}")
    assert set(gh.prs_[pr]["labels"]) == {"vigia:review", "tier:T0"}
    assert gh.bodies(pr)[0].startswith(f"## Handoff {head1}\nEngine: claude · claude-sonnet-5-5")
    assert "vigia:building" not in gh.issues_[n]["labels"]

    assert decisions(tick(ctx)) == [(pr, "gate")]  # round 1: CHANGES (P1)
    assert run.calls[-1]["engine"] == "codex" and run.calls[-1]["role"] == "reviewer"
    assert f"Reviewed commit: {head1}" in gh.bodies(pr)[-1]

    assert decisions(tick(ctx)) == [(pr, "fix")]
    head2 = gh.branch_sha(gh.prs_[pr]["branch"])
    assert head2 != head1
    assert "wrong result" in run.calls[-1]["prompt"]  # the fixer got the finding

    assert decisions(tick(ctx)) == [(pr, "gate")]  # round 2 on the new head: APPROVE
    assert "Previous round" in run.calls[-1]["prompt"]

    assert decisions(tick(ctx)) == [(pr, "merge")]
    assert gh.merged == [(pr, head2)]
    assert "no model ran this step" in gh.bodies(pr)[-1]
    events = [r["event"] for r in ctx.ledger.rows()]
    assert events.count("verdict") == 2 and events[-2:] == ["merged", "follow_ups_done"]
    assert not any(ctx.root.iterdir())  # every worktree was removed


def test_a_tick_reports_the_decision_before_acting_on_it(world):
    gh, build = world
    ctx, _ = build([APPROVE])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx)
    pr = next(iter(gh.prs_))
    said = []
    ctx.say = said.append
    seen = tick(ctx)
    assert decisions(seen) == [(pr, "gate")]
    decision = next(i for i, line in enumerate(said) if line == f"PR #{pr}: {seen[0].decision}")
    action = next(i for i, line in enumerate(said) if line.startswith(f"PR #{pr}: gate round"))
    assert decision < action
    assert said.count(f"PR #{pr}: {seen[0].decision}") == 1


def test_ci_pending_holds_the_gate(world):
    gh, build = world
    ctx, run = build([APPROVE])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx)
    pr = next(iter(gh.prs_))
    gh.ci[gh.branch_sha(gh.prs_[pr]["branch"])] = "pending"
    reviews = len([c for c in run.calls if c["role"] == "reviewer"])
    assert decisions(tick(ctx)) == [(pr, "wait")]
    assert len([c for c in run.calls if c["role"] == "reviewer"]) == reviews


def test_t2_waits_for_a_human_label_on_the_exact_head(world):
    gh, build = world
    ctx, run = build([APPROVE, APPROVE])
    gh.add_issue("Touch billing", TICKET, ["vigia:ready", "tier:T2"])
    tick(ctx)
    pr = next(iter(gh.prs_))
    assert decisions(tick(ctx)) == [(pr, "gate")]
    assert decisions(tick(ctx)) == [(pr, "review")]
    assert run.calls[-1]["engine"] == "claude" and run.calls[-1]["role"] == "reviewer"
    assert decisions(tick(ctx)) == [(pr, "wait")]
    assert gh.merged == []
    gh.human_label(pr)
    assert decisions(tick(ctx)) == [(pr, "merge")]
    assert len(gh.merged) == 1


def test_a_new_head_clears_the_human_label(world):
    gh, build = world
    ctx, _ = build([])
    gh.add_issue("Touch billing", TICKET, ["vigia:ready", "tier:T2"])
    tick(ctx, build=True)
    pr = next(iter(gh.prs_))
    gh.human_label(pr)
    gh.ci[gh.branch_sha(gh.prs_[pr]["branch"])] = "pending"
    tick(ctx)
    assert "vigia:human-ok" in gh.prs_[pr]["labels"]
    # someone pushes to the branch behind vigia's back
    from fakes import git

    wt = ctx.root / "manual"
    git("worktree", "add", "--quiet", "--detach", str(wt), gh.prs_[pr]["head"], cwd=ctx.clone)
    (wt / "extra.txt").write_text("x")
    git("add", "-A", cwd=wt)
    git("commit", "--quiet", "-m", "manual", cwd=wt)
    git("push", "--quiet", "origin", f"HEAD:refs/heads/{gh.prs_[pr]['branch']}", cwd=wt)
    gh.ci[git("rev-parse", "HEAD", cwd=wt)] = "pending"
    tick(ctx)
    assert "vigia:human-ok" not in gh.prs_[pr]["labels"]


def test_two_p1_rounds_park_the_pr(world):
    gh, build = world
    ctx, _ = build([CHANGES, CHANGES, APPROVE])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx)
    pr = next(iter(gh.prs_))
    for expected in ("gate", "fix", "gate", "park"):
        assert decisions(tick(ctx)) == [(pr, expected)]
    assert "vigia:parked" in gh.prs_[pr]["labels"]
    assert gh.bodies(pr)[-1].startswith("PARKED ")
    assert decisions(tick(ctx)) == [(pr, "wait")]
    # a human removes the label: counters start again, so the gate runs a fresh round
    gh.remove_label(pr, "vigia:parked")
    assert decisions(tick(ctx)) == [(pr, "gate")]


def test_an_incomplete_ticket_is_refused_not_built(world):
    gh, build = world
    ctx, run = build([])
    n = gh.add_issue("Vague", "## Outcome\nmake it better\n", ["vigia:ready", "tier:T1"])
    tick(ctx)
    assert gh.prs_ == {} and run.calls == []
    assert "vigia:ready" not in gh.issues_[n]["labels"]
    assert "Must still hold:" in gh.bodies(n)[0]


def test_build_can_be_switched_off(world):
    gh, build = world
    ctx, run = build([])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx, build=False)
    assert gh.prs_ == {} and run.calls == []
    ctx2, _ = build([], cfg=replace(ctx.cfg, build_in_watch=False))
    tick(ctx2)
    assert gh.prs_ == {}


def test_a_pr_opened_by_hand_joins_through_a_handoff(world):
    from fakes import git

    gh, build = world
    ctx, run = build([APPROVE])
    wt = ctx.root / "by-hand"
    git("worktree", "add", "--quiet", "--detach", str(wt), "origin/main", cwd=ctx.clone)
    (wt / "notes.txt").write_text("written by a human or another agent\n")
    git("add", "-A", cwd=wt)
    git("commit", "--quiet", "-m", "docs: notes", cwd=wt)
    git("push", "--quiet", "origin", "HEAD:refs/heads/hand/notes", cwd=wt)
    pr = gh.create_pr("hand/notes", "main", "docs: notes", "A hand-made change.")
    gh.add_labels(pr, "vigia:review", "tier:T0")

    assert decisions(tick(ctx, build=False)) == [(pr, "park")]  # no lineage yet: parked, never gated
    gh.remove_label(pr, "vigia:parked")
    sha = gh.branch_sha("hand/notes")
    gh.comment(pr, f"## Handoff {sha}\nEngine: claude · claude-opus-5-5 · by hand · lab anthropic\n\nNotes.")
    assert decisions(tick(ctx, build=False)) == [(pr, "gate")]
    assert run.calls[-1]["engine"] == "codex"
    assert decisions(tick(ctx, build=False)) == [(pr, "merge")]


def test_a_handoff_from_another_login_does_not_count(world):
    gh, build = world
    ctx, run = build([])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx)
    pr = next(iter(gh.prs_))
    ctx.ledger.path.unlink()  # forget vigia's own record of who built it
    gh.thread[pr] = [dict(c, author="mallory") for c in gh.thread[pr]]
    assert decisions(tick(ctx, build=False)) == [(pr, "park")]


def test_a_builder_that_changes_nothing_parks_the_issue(world):
    gh, build = world
    ctx, _ = build([])
    ctx.engines._run = engine([], edits=lambda cwd, k: None)
    n = gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx)
    assert gh.prs_ == {}
    assert set(gh.issues_[n]["labels"]) == {"tier:T0", "vigia:parked"}
    assert "left no changes" in gh.bodies(n)[-1]


def test_a_rejected_push_fails_the_build_cleanly(world):
    gh, build = world
    ctx, _ = build([])
    hook = gh.origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho rejected >&2\nexit 1\n")
    hook.chmod(0o755)
    n = gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx)
    assert gh.prs_ == {}
    assert "vigia:building" not in gh.issues_[n]["labels"] and "vigia:parked" in gh.issues_[n]["labels"]
    assert "failed after the session" in gh.bodies(n)[-1]
    assert not any(ctx.root.iterdir())


def _fail_once(gh, method, match):
    """Make `gh.<method>` raise a 503 the first time `match(*args)` holds, then behave normally."""
    from vigia.proc import CommandError

    real, state = getattr(gh, method), {"left": 1}

    def flaky(*args):
        if state["left"] and match(*args):
            state["left"] -= 1
            raise CommandError(["gh", "api"], 1, "", "HTTP 503")
        return real(*args)

    setattr(gh, method, flaky)


def _push_behind_its_back(ctx, gh, pr):
    from fakes import git

    wt = ctx.root / f"manual-{len(list(ctx.root.iterdir()))}"
    git("worktree", "add", "--quiet", "--detach", str(wt), gh.prs_[pr]["head"], cwd=ctx.clone)
    (wt / "extra.txt").write_text("x")
    git("add", "-A", cwd=wt)
    git("commit", "--quiet", "-m", "manual", cwd=wt)
    git("push", "--quiet", "origin", f"HEAD:refs/heads/{gh.prs_[pr]['branch']}", cwd=wt)
    return git("rev-parse", "HEAD", cwd=wt)


def _removal_always_fails(gh, label="vigia:human-ok"):
    from vigia.proc import CommandError

    real = gh.remove_label

    def broken(n, name):
        if name == label:
            raise CommandError(["gh", "api"], 1, "", "HTTP 503")
        return real(n, name)

    gh.remove_label = broken


def test_a_stale_human_label_never_counts_even_if_it_cannot_be_removed(world):
    gh, build = world
    ctx, _ = build([APPROVE] * 4)
    gh.add_issue("Touch billing", TICKET, ["vigia:ready", "tier:T2"])
    tick(ctx, build=True)
    pr = next(iter(gh.prs_))
    tick(ctx)  # gate
    tick(ctx)  # review
    gh.human_label(pr)  # a human approves head A
    _removal_always_fails(gh)
    _push_behind_its_back(ctx, gh, pr)  # head B
    assert decisions(tick(ctx, build=False)) == [(pr, "gate")]
    assert ctx.ledger.last("error", pr=pr)["action"] == "clear_human_ok"
    assert decisions(tick(ctx, build=False)) == [(pr, "review")]
    assert decisions(tick(ctx, build=False)) == [
        (pr, "wait")
    ]  # the label is still there, and counts for nothing
    assert "vigia:human-ok" in gh.prs_[pr]["labels"] and gh.merged == []
    gh.human_label(pr)  # a human approves head B
    assert decisions(tick(ctx, build=False)) == [(pr, "merge")]


def test_a_stale_human_label_never_counts_when_the_local_clock_runs_behind(world):
    gh, build = world
    ctx, _ = build([APPROVE] * 4)
    gh.add_issue("Touch billing", TICKET, ["vigia:ready", "tier:T2"])
    tick(ctx, build=True)
    pr = next(iter(gh.prs_))
    tick(ctx)  # gate
    tick(ctx)  # review
    gh.human_label(pr)  # a human approves head A; GitHub stamps it 2026-10-24
    _removal_always_fails(gh)
    ctx.ledger.clock = lambda: "2026-01-01T00:00:00Z"  # this machine's clock is far behind GitHub's
    _push_behind_its_back(ctx, gh, pr)  # head B
    for expected in ("gate", "review", "wait"):
        assert decisions(tick(ctx, build=False)) == [(pr, expected)]
    assert gh.merged == []
    gh.human_label(pr)  # a human approves head B
    assert decisions(tick(ctx, build=False)) == [(pr, "merge")]


def test_a_head_recorded_before_the_event_baseline_needs_the_label_again(world):
    gh, build = world
    ctx, _ = build([APPROVE] * 2)
    gh.add_issue("Touch billing", TICKET, ["vigia:ready", "tier:T2"])
    tick(ctx, build=True)
    pr = next(iter(gh.prs_))
    tick(ctx)
    tick(ctx)
    gh.human_label(pr)
    rows = ctx.ledger.path.read_text().splitlines()
    ctx.ledger.path.write_text("\n".join(r.replace(', "human_ok_event": 0', "") for r in rows) + "\n")
    assert decisions(tick(ctx, build=False)) == [(pr, "wait")]
    gh.human_label(pr)
    assert decisions(tick(ctx, build=False)) == [(pr, "merge")]


def test_a_tier_change_discards_verdicts_run_for_the_old_tier(world):
    gh, build = world
    ctx, run = build([APPROVE] * 4)
    gh.add_issue("Touch billing", TICKET, ["vigia:ready", "tier:T1"])
    tick(ctx, build=True)
    pr = next(iter(gh.prs_))
    assert decisions(tick(ctx, build=False)) == [(pr, "gate")]
    assert decisions(tick(ctx, build=False)) == [(pr, "review")]
    gh.remove_label(pr, "tier:T1")
    gh.add_labels(pr, "tier:T2")  # the risk was misclassified; the head is unchanged
    gh.human_label(pr)
    seen = sense(ctx, track=False)
    assert decisions(seen) == [(pr, "gate")]  # the T1 verdicts do not cover T2
    assert decisions(tick(ctx, build=False)) == [(pr, "gate")]
    assert decisions(tick(ctx, build=False)) == [(pr, "review")]
    assert ctx.ledger.last("verdict", pr=pr, kind="review")["tier"] == "T2"
    assert decisions(tick(ctx, build=False)) == [(pr, "merge")]


def test_an_interrupted_review_leaves_no_worktree_that_blocks_the_next_one(world):
    gh, build = world
    ctx, _ = build([APPROVE] * 2)
    gh.add_issue("Touch billing", TICKET, ["vigia:ready", "tier:T1"])
    tick(ctx, build=True)
    pr = next(iter(gh.prs_))
    head = gh.prs_[pr]["head"]
    from fakes import git

    # vigia died after making the gate worktree and before recording a verdict
    stale = ctx.root / f"gate-{pr}-{head[:8]}-1"
    git("worktree", "add", "--quiet", "--detach", str(stale), head, cwd=ctx.clone)
    assert decisions(tick(ctx, build=False)) == [(pr, "gate")]
    assert ctx.ledger.last("verdict", pr=pr, kind="gate")["verdict"] == "APPROVE"
    assert not stale.exists()


def test_review_replaces_a_worktree_left_under_its_own_name(world):
    from vigia.gate import review

    gh, build = world
    ctx, _ = build([APPROVE])
    gh.add_issue("Touch billing", TICKET, ["vigia:ready", "tier:T1"])
    tick(ctx, build=True)
    pr = next(iter(gh.prs_))
    head = gh.prs_[pr]["head"]
    stale = ctx.root / f"gate-{pr}-{head[:8]}-1"
    stale.mkdir()
    (stale / "junk").write_text("x")
    assert review(ctx, gh.pr(pr), "gate", 1) == "APPROVE"
    assert not stale.exists()


def test_a_fix_round_invalidates_the_human_label_even_if_removal_fails(world):
    gh, build = world
    ctx, _ = build([CHANGES, APPROVE, APPROVE])
    gh.add_issue("Touch billing", TICKET, ["vigia:ready", "tier:T2"])
    tick(ctx, build=True)
    pr = next(iter(gh.prs_))
    tick(ctx)  # gate -> CHANGES
    gh.human_label(pr)
    _removal_always_fails(gh)
    assert decisions(tick(ctx, build=False)) == [(pr, "fix")]
    assert "vigia:human-ok" in gh.prs_[pr]["labels"]
    for expected in ("gate", "review", "wait"):
        assert decisions(tick(ctx, build=False)) == [(pr, expected)]
    assert gh.merged == []


def test_a_build_that_fails_while_claiming_the_issue_parks_it(world):
    from vigia.proc import CommandError

    gh, build = world
    ctx, run = build([])
    n = gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    real = gh.add_labels

    def broken(number, *labels):
        if "vigia:building" in labels:
            raise CommandError(["gh", "api"], 1, "", "HTTP 503")
        return real(number, *labels)

    gh.add_labels = broken
    tick(ctx)
    assert run.calls == [] and gh.prs_ == {}
    assert "vigia:parked" in gh.issues_[n]["labels"]
    assert "could not start the build" in gh.bodies(n)[-1]


def test_an_interrupted_build_is_parked_by_the_next_tick(world):
    gh, build = world
    ctx, run = build([])
    n = gh.add_issue("Add a helper", TICKET, ["tier:T0"])
    ctx.ledger.append(
        "build_started", issue=n, tier="T0", engine="claude", model="claude-sonnet-5-5", branch="x"
    )
    tick(ctx)  # e.g. the process died mid-build: the issue is neither ready nor building
    assert "vigia:parked" in gh.issues_[n]["labels"]
    assert "interrupted" in gh.bodies(n)[-1]
    assert ctx.ledger.last("build_failed", issue=n)
    tick(ctx)
    assert len(gh.bodies(n)) == 1  # recovered once


def test_a_pr_aimed_at_another_branch_is_parked_not_reviewed(world):
    gh, build = world
    ctx, run = build([APPROVE])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx)
    pr = next(iter(gh.prs_))
    gh.prs_[pr]["base"] = "release"
    assert decisions(tick(ctx, build=False)) == [(pr, "park")]
    assert not [c for c in run.calls if c["role"] == "reviewer"]
    assert gh.merged == []


def test_a_retarget_after_approval_blocks_the_merge(world):
    gh, build = world
    ctx, _ = build([APPROVE])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx)
    pr = next(iter(gh.prs_))
    tick(ctx, build=False)  # gate approves; merge decision is next
    s = sense(ctx, track=False)[0]
    assert s.decision.action == "merge"
    gh.prs_[pr]["base"] = "release"
    assert merge(ctx, s) is False and gh.merged == []


def test_removing_the_review_label_before_the_merge_withdraws_the_pr(world):
    gh, build = world
    ctx, _ = build([APPROVE])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx)
    pr = next(iter(gh.prs_))
    tick(ctx, build=False)  # gate approves; merge decision is next
    s = sense(ctx, track=False)[0]
    assert s.decision.action == "merge"
    gh.remove_label(pr, "vigia:review")  # an operator withdraws the PR after the tick sensed it
    assert merge(ctx, s) is False and gh.merged == []


def test_a_failed_parking_comment_still_records_the_park(world):
    gh, build = world
    ctx, _ = build([CHANGES, CHANGES, APPROVE])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx)
    pr = next(iter(gh.prs_))
    for expected in ("gate", "fix", "gate"):
        assert decisions(tick(ctx)) == [(pr, expected)]
    _fail_once(gh, "comment", lambda n, body: body.startswith("PARKED "))
    assert decisions(tick(ctx)) == [(pr, "park")]
    assert "vigia:parked" in gh.prs_[pr]["labels"]
    assert ctx.ledger.last("parked", pr=pr)
    # the operator unparks: the counters restart and the gate runs a fresh round instead of parking again
    gh.remove_label(pr, "vigia:parked")
    assert decisions(tick(ctx)) == [(pr, "gate")]
    assert ctx.ledger.last("unparked", pr=pr)


def test_a_build_whose_labels_failed_is_enrolled_by_the_next_tick(world):
    from vigia.proc import CommandError

    gh, build = world
    ctx, _ = build([APPROVE])
    n = gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    _fail_once(gh, "add_labels", lambda pr, *labels: "vigia:review" in labels)
    with pytest.raises(CommandError):
        tick(ctx)
    pr = next(iter(gh.prs_))
    assert "vigia:review" not in gh.prs_[pr]["labels"] and "vigia:building" in gh.issues_[n]["labels"]
    assert ctx.ledger.last("pr_opened", pr=pr)
    assert decisions(tick(ctx)) == [(pr, "gate")]  # recovered, then gated in the same tick
    assert set(gh.prs_[pr]["labels"]) == {"vigia:review", "tier:T0"}
    assert "vigia:building" not in gh.issues_[n]["labels"]
    assert ctx.ledger.last("enrolled", pr=pr)
    tick(ctx)
    assert len(ctx.ledger.events("enrolled", pr=pr)) == 1


def test_a_label_added_while_the_fixer_works_does_not_approve_the_fixed_head(world):
    gh, build = world
    ctx, run = build([CHANGES, APPROVE, APPROVE])
    gh.add_issue("Touch billing", TICKET, ["vigia:ready", "tier:T2"])
    tick(ctx, build=True)
    pr = next(iter(gh.prs_))
    tick(ctx)  # gate -> CHANGES
    default = run.edit

    def edit_and_label(cwd, k):
        gh.human_label(pr)  # a human approves the old head while the fix session runs
        default(cwd, k)

    run.edit = edit_and_label
    assert decisions(tick(ctx, build=False)) == [(pr, "fix")]
    run.edit = default
    for expected in ("gate", "review", "wait"):
        assert decisions(tick(ctx, build=False)) == [(pr, expected)]
    assert gh.merged == []


def test_two_clones_of_one_repository_get_separate_work_roots(tmp_path, monkeypatch):
    from vigia import safety
    from vigia.config import render

    monkeypatch.setattr(safety, "default_root", lambda: tmp_path / "tmp" / "vigia")
    text = render(Config(repo="acme/textkit", base="main", tiers=default_tiers()))
    roots = []
    for name in ("clone-a", "clone-b"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "vigia.toml").write_text(text)
        roots.append(Ctx.load(tmp_path / name).root)
    assert roots[0] != roots[1]
    assert all(r.parent == (tmp_path / "tmp" / "vigia").resolve() for r in roots)


# -- follow-ups: the notes a handoff leaves outside its ticket become issues when the PR merges --


def handoff_with(gh, pr, follow_ups, author="owner"):
    sha = gh.branch_sha(gh.prs_[pr]["branch"])
    body = f"## Handoff {sha}\nEngine: claude · claude-sonnet-5-5 · fixer · lab anthropic\n\n## Summary\n- x\n{follow_ups}"
    gh.thread.setdefault(pr, []).append({"author": author, "body": body, "ts": ""})


def ready_to_merge(world, follow_ups=None):
    gh, build = world
    ctx, run = build([APPROVE])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx)
    pr = next(iter(gh.prs_))
    if follow_ups:
        handoff_with(gh, pr, follow_ups)
    return gh, ctx, pr


def follow_up_issues(gh):
    return [i for i in gh.issues_.values() if "vigia:follow-up" in i["labels"]]


TWO_AND_NONE = "\n## Follow-ups\n- `shout()` ignores non-ASCII letters\n- docs say `textkit` but the package is `tk`\n- None.\n"


def test_a_merged_pr_files_one_issue_per_follow_up_and_a_second_tick_files_none(world):
    gh, ctx, pr = ready_to_merge(world, TWO_AND_NONE)
    tick(ctx, build=False)  # the gate approves
    assert follow_up_issues(gh) == []  # nothing is filed before the merge
    tick(ctx, build=False)  # merge
    assert gh.merged and "no model ran this step" in gh.bodies(pr)[-1]
    issues = follow_up_issues(gh)
    assert [i["title"] for i in issues] == [
        "`shout()` ignores non-ASCII letters",
        "docs say `textkit` but the package is `tk`",
    ]
    head = gh.merged[0][1]
    for issue in issues:
        assert issue["labels"] == ["vigia:follow-up"]  # no tier label and never `vigia:ready`
        assert f"#{pr}" in issue["body"] and f"https://github.com/acme/textkit/pull/{pr}" in issue["body"]
        assert head in issue["body"] and "vigia ticket template" in issue["body"]
        assert issue["body"].startswith("> ")
    assert [r["event"] for r in ctx.ledger.rows()].count("followup_filed") == 2

    tick(ctx, build=False)
    tick(ctx, build=False)
    assert len(follow_up_issues(gh)) == 2


def test_a_failed_filing_never_undoes_the_merge_and_the_next_tick_retries_it(world, monkeypatch):
    gh, ctx, pr = ready_to_merge(world, "\n## Follow-ups\n- rename the helper\n")
    real, calls = gh.create_issue, []

    def flaky(title, body, labels):
        calls.append(title)
        if len(calls) == 1:
            raise CommandError(["gh", "api"], 1, "", "HTTP 502")
        return real(title, body, labels)

    monkeypatch.setattr(gh, "create_issue", flaky)
    tick(ctx, build=False)
    tick(ctx, build=False)  # merge; the issue cannot be created
    assert gh.merged and gh.prs_[pr]["state"] == "MERGED"
    assert "no model ran this step" in gh.bodies(pr)[-1]
    assert follow_up_issues(gh) == []
    errors = ctx.ledger.events("error", pr=pr, action="follow_up")
    assert len(errors) == 1 and "HTTP 502" in errors[0]["detail"]

    tick(ctx, build=False)  # retried
    tick(ctx, build=False)
    assert [i["title"] for i in follow_up_issues(gh)] == ["rename the helper"]
    assert len(calls) == 2


def test_filing_stops_after_caps_attempts(world, monkeypatch):
    gh, ctx, pr = ready_to_merge(world, "\n## Follow-ups\n- rename the helper\n")
    calls = []

    def down(title, body, labels):
        calls.append(title)
        raise CommandError(["gh", "api"], 1, "", "HTTP 502")

    monkeypatch.setattr(gh, "create_issue", down)
    for _ in range(8):
        tick(ctx, build=False)
    assert len(calls) == ctx.cfg.caps.attempts == 3
    assert gh.merged and follow_up_issues(gh) == []


def test_a_crash_between_creating_the_issue_and_recording_it_does_not_duplicate(world):
    gh, ctx, pr = ready_to_merge(world, "\n## Follow-ups\n- rename the helper\n")
    text = "rename the helper"
    title, body = comments.follow_up_issue(text, pr, "u", "s")
    gh.add_issue(title, body, ["vigia:follow-up"])  # filed by a run that died before its ledger row
    tick(ctx, build=False)
    tick(ctx, build=False)
    assert len(follow_up_issues(gh)) == 1
    assert len(ctx.ledger.events("followup_filed", pr=pr)) == 1


def test_a_crash_leftover_that_was_closed_meanwhile_is_still_adopted(world):
    gh, ctx, pr = ready_to_merge(world, "\n## Follow-ups\n- rename the helper\n")
    title, body = comments.follow_up_issue("rename the helper", pr, "u", "s")
    left = gh.add_issue(title, body, ["vigia:follow-up"])  # filed by a run that died before its ledger row
    gh.issues_[left]["state"] = "CLOSED"  # someone closed it before the retry
    tick(ctx, build=False)
    tick(ctx, build=False)
    assert len(follow_up_issues(gh)) == 1
    assert [r["issue"] for r in ctx.ledger.events("followup_filed", pr=pr)] == [left]


def test_at_most_five_follow_ups_are_filed_and_duplicates_are_skipped(world):
    bullets = "\n".join(f"- item {k}" for k in range(1, 8))
    gh, ctx, pr = ready_to_merge(world, f"\n## Follow-ups\n- item 1\n{bullets}\n")
    handoff_with(
        gh, pr, "\n## follow-ups\n- ITEM 1\n- item   2\n- NONE\n"
    )  # a later round repeats and shouts
    tick(ctx, build=False)
    tick(ctx, build=False)
    titles = [i["title"] for i in follow_up_issues(gh)]
    assert titles == ["item 1", "item 2", "item 3", "item 4", "item 5"] and len(titles) == 5
    for _ in range(2):
        tick(ctx, build=False)
    assert len(follow_up_issues(gh)) == 5


def test_a_long_follow_up_is_cut_at_a_word_boundary(world):
    long = "make the shout helper handle accented characters and the German sharp s the way the docs promise"
    gh, ctx, pr = ready_to_merge(world, f"\n## Follow-ups\n- {long}\n")
    tick(ctx, build=False)
    tick(ctx, build=False)
    (issue,) = follow_up_issues(gh)
    assert issue["title"] == "make the shout helper handle accented characters and the German sharp"
    assert len(issue["title"]) <= 70 and f"> {long}" in issue["body"]


def test_only_vigias_own_handoffs_are_read(world):
    gh, ctx, pr = ready_to_merge(world)
    handoff_with(gh, pr, "\n## Follow-ups\n- planted by a stranger\n", author="mallory")
    tick(ctx, build=False)
    tick(ctx, build=False)
    assert gh.merged and follow_up_issues(gh) == []


def test_nothing_is_filed_for_a_pr_that_was_not_merged(world):
    gh, ctx, pr = ready_to_merge(world, TWO_AND_NONE)
    tick(ctx, build=False)  # gate approves; merge decision is next
    s = sense(ctx, track=False)[0]
    gh.remove_label(pr, "vigia:review")
    assert merge(ctx, s) is False
    tick(ctx, build=False)
    assert follow_up_issues(gh) == [] and ctx.ledger.events("followup_filed") == []


def test_a_merge_recorded_before_this_feature_is_not_swept(world):
    gh, ctx, pr = ready_to_merge(world, TWO_AND_NONE)
    ctx.ledger.append("merged", pr=pr, sha=gh.branch_sha(gh.prs_[pr]["branch"]), tier="T0", issue=1)
    assert pending_follow_ups(ctx) == []
    gh.prs_[pr]["state"] = "MERGED"
    tick(ctx, build=False)
    assert follow_up_issues(gh) == []


def test_a_follow_up_filed_before_a_crash_is_adopted_whatever_its_label_now(world):  # noqa: F811
    from vigia.watch import file_follow_ups

    gh, ctx, pr = ready_to_merge(world, "\n## Follow-ups\n- rename the helper\n")
    tick(ctx, build=False)
    tick(ctx, build=False)  # merge, and the follow-up is filed
    (issue,) = follow_up_issues(gh)
    # a crash had lost the ledger row; meanwhile someone relabelled the issue
    rows = [r for r in ctx.ledger.rows() if r["event"] not in ("followup_filed", "follow_ups_done")]
    ctx.ledger.path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    gh.issues_[issue["number"]]["labels"] = ["triaged"]
    file_follow_ups(ctx, pr)
    assert not follow_up_issues(gh)  # relabelled away
    titles = [i["title"] for i in gh.issues_.values()]
    assert titles.count("rename the helper") == 1  # adopted, not filed again
    assert ctx.ledger.events("followup_filed", pr=pr)[0]["issue"] == issue["number"]


def test_the_cap_counts_everything_already_filed_for_the_pr(world):  # noqa: F811
    from vigia.watch import MAX_FOLLOW_UPS, file_follow_ups

    bullets = "".join(f"- note {k}\n" for k in range(MAX_FOLLOW_UPS))
    gh, ctx, pr = ready_to_merge(world, f"\n## Follow-ups\n{bullets}")
    tick(ctx, build=False)
    tick(ctx, build=False)  # merge: five filed
    assert len(follow_up_issues(gh)) == MAX_FOLLOW_UPS
    # a crash before follow_ups_done, then a new bullet appears first in a later handoff
    rows = [r for r in ctx.ledger.rows() if r["event"] != "follow_ups_done"]
    ctx.ledger.path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    handoff_with(gh, pr, "\n## Follow-ups\n- a newer note\n")
    file_follow_ups(ctx, pr)
    assert len(follow_up_issues(gh)) == MAX_FOLLOW_UPS  # never a sixth
