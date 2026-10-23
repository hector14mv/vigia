"""End to end on a real local git remote: issue -> PR -> gate -> fix -> gate -> merge.

GitHub and the engines are fakes (tests/fakes.py); git, worktrees, commits and pushes are real, all
inside tmp_path.
"""

from dataclasses import replace

import pytest

from fakes import APPROVE, CHANGES, FakeGitHub, engine, make_repo
from vigia.config import Config, default_tiers
from vigia.context import Ctx
from vigia.engines import Engines
from vigia.git import Git
from vigia.ledger import Ledger
from vigia.watch import tick

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
    gh = FakeGitHub(origin)
    cfg = Config(repo=gh.repo, base="main", tiers=default_tiers("claude"), test_command="true")

    def build(verdicts, cfg=cfg):
        run = engine(verdicts)
        ctx = Ctx(
            cfg=cfg,
            clone=clone,
            gh=gh,
            git=Git(clone, tmp_path / "work"),
            ledger=Ledger(clone / ".vigia" / "ledger.jsonl"),
            engines=Engines(cfg, clone / ".vigia" / "logs", runner=run),
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
    assert events.count("verdict") == 2 and events[-1] == "merged"
    assert not any(ctx.root.iterdir())  # every worktree was removed


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
