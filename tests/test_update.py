"""A moved base: vigia brings it into the PR and reviews the result again before a person approves or it merges."""

from fakes import APPROVE, CHANGES, git
from test_e2e import TICKET, decisions, world  # noqa: F401  (world is a fixture)
from test_rules import CFG, H1, H2, H3, gate, pr, review
from vigia.rules import decide
from vigia.watch import tick


def test_an_approved_pr_behind_its_base_is_updated_not_merged():
    d = decide(pr(behind=True, verdicts=(gate(),)), CFG)
    assert d.action == "update"


def test_a_t2_behind_its_base_is_updated_before_the_human_is_asked():
    assert decide(pr(tier="T2", behind=True, verdicts=(gate(), review())), CFG).action == "update"


def test_an_update_head_is_reviewed_again_without_using_a_round():
    used = (gate(H1, "CHANGES", 1), gate(H2, "APPROVE"))
    d = decide(pr(head=H3, fix_rounds=1, verdicts=used, updated_heads=frozenset({H3})), CFG)
    assert d.action == "gate"
    assert (
        decide(pr(head=H3, fix_rounds=1, verdicts=used), CFG).action == "park"
    )  # a normal new head would park


def _move_base(ctx, gh, path, text):
    wt = ctx.root / f"base-{path.replace('.', '-')}"
    git("worktree", "add", "--quiet", "--detach", str(wt), "origin/main", cwd=ctx.clone)
    git("fetch", "--quiet", "origin", "+refs/heads/main:refs/remotes/origin/main", cwd=ctx.clone)
    git("checkout", "--quiet", "--detach", "origin/main", cwd=wt)
    (wt / path).write_text(text)
    git("add", "-A", cwd=wt)
    git("commit", "--quiet", "-m", f"base change to {path}", cwd=wt)
    git("push", "--quiet", "origin", "HEAD:refs/heads/main", cwd=wt)
    git("worktree", "remove", "--force", str(wt), cwd=ctx.clone)


def test_a_moved_base_is_brought_in_and_the_new_head_is_gated_before_the_merge(world):  # noqa: F811
    gh, build = world
    ctx, run = build([APPROVE, APPROVE])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx)  # build
    pr_n = next(iter(gh.prs_))
    assert decisions(tick(ctx)) == [(pr_n, "gate")]  # approve head A
    _move_base(ctx, gh, "NOTES.md", "the base moved\n")
    assert decisions(tick(ctx)) == [(pr_n, "update")]
    head = gh.branch_sha(gh.prs_[pr_n]["branch"])
    assert ctx.ledger.last("updated", pr=pr_n)["sha"] == head
    assert git("cat-file", "-e", f"{head}:NOTES.md", cwd=gh.origin) == ""  # the base's file is in the PR now
    assert decisions(tick(ctx)) == [(pr_n, "gate")]  # reviewed again, told the base moved
    assert "## The base moved" in run.calls[-1]["prompt"]
    assert decisions(tick(ctx)) == [(pr_n, "merge")]
    assert gh.merged == [(pr_n, head)]


def test_a_base_that_conflicts_parks_the_pr(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([APPROVE])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx)
    pr_n = next(iter(gh.prs_))
    tick(ctx)  # gate approves
    _move_base(
        ctx, gh, "textkit.py", "def shout(text):\n    return text.lower()\n"
    )  # the file the PR changed
    assert decisions(tick(ctx)) == [(pr_n, "update")]
    assert "vigia:parked" in gh.prs_[pr_n]["labels"]
    assert "conflicts" in gh.bodies(pr_n)[-1]


def test_a_fix_after_an_update_still_counts_its_rounds(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([CHANGES, APPROVE, APPROVE])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx)
    pr_n = next(iter(gh.prs_))
    for expected in ("gate", "fix", "gate"):  # round 1 P1, fix, round 2 approves
        assert decisions(tick(ctx)) == [(pr_n, expected)]
    _move_base(ctx, gh, "NOTES.md", "moved\n")
    assert decisions(tick(ctx)) == [(pr_n, "update")]
    assert decisions(tick(ctx)) == [(pr_n, "gate")]  # both rounds used, yet the update head is gated
    assert decisions(tick(ctx)) == [(pr_n, "merge")]


def test_a_pr_behind_its_base_is_updated_before_its_first_gate():
    assert decide(pr(behind=True), CFG).action == "update"


def test_a_t2_behind_its_base_is_updated_before_the_tier_review():
    assert decide(pr(tier="T2", behind=True, verdicts=(gate(),)), CFG).action == "update"


def test_a_pr_is_not_merged_where_github_would_merge_it_onto_a_moved_base():
    d = decide(pr(verdicts=(gate(),), base_guarded=False), CFG)
    assert d.action == "wait" and "up to date before merging" in d.reason


def test_a_moved_base_is_brought_in_before_the_first_gate(world):  # noqa: F811
    gh, build = world
    ctx, run = build([APPROVE])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx)  # build
    pr_n = next(iter(gh.prs_))
    _move_base(ctx, gh, "NOTES.md", "the base moved\n")
    calls = len(run.calls)
    assert decisions(tick(ctx)) == [(pr_n, "update")]
    assert len(run.calls) == calls  # no gate session was spent on the outdated head
    assert decisions(tick(ctx)) == [(pr_n, "gate")]
    assert ctx.ledger.last("verdict", pr=pr_n)["sha"] == ctx.ledger.last("updated", pr=pr_n)["sha"]


def test_without_githubs_guard_on_the_base_vigia_does_not_merge(world):  # noqa: F811
    gh, build = world
    gh.base_guarded = False
    ctx, _ = build([APPROVE])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx)
    tick(ctx)  # gate approves
    (seen,) = tick(ctx)
    assert seen.decision.action == "wait" and "up to date before merging" in seen.decision.reason
    assert gh.merged == []


def test_a_base_that_moves_after_the_last_check_stops_the_merge(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([APPROVE, APPROVE])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx)
    pr_n = next(iter(gh.prs_))
    tick(ctx)  # gate approves head A
    merge = gh.merge

    def merge_after_the_base_moved(n, sha):  # another PR lands between vigia's compare and its merge
        _move_base(ctx, gh, "NOTES.md", "landed meanwhile\n")
        gh.merge = merge
        merge(n, sha)

    gh.merge = merge_after_the_base_moved
    assert decisions(tick(ctx)) == [(pr_n, "merge")]
    assert gh.merged == []  # GitHub refused: the head was not reviewed against that base
    assert decisions(tick(ctx)) == [(pr_n, "update")]
    assert decisions(tick(ctx)) == [(pr_n, "gate")]
    assert decisions(tick(ctx)) == [(pr_n, "merge")]
    assert gh.merged == [(pr_n, gh.branch_sha(gh.prs_[pr_n]["branch"]))]


def test_an_update_worktree_left_by_an_interrupted_watcher_does_not_block_the_next(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([APPROVE])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx)
    pr_n = next(iter(gh.prs_))
    tick(ctx)  # gate approves
    _move_base(ctx, gh, "NOTES.md", "moved\n")
    head = gh.prs_[pr_n]["head"]
    ctx.git.worktree_detached(ctx.root / f"update-{pr_n}-{head[:8]}", head)  # killed before the push
    assert decisions(tick(ctx)) == [(pr_n, "update")]
    assert ctx.ledger.last("updated", pr=pr_n)["from_sha"] == head
    assert not (ctx.root / f"update-{pr_n}-{head[:8]}").exists()


def test_a_gate_job_is_no_longer_due_once_the_base_moved(world):  # noqa: F811
    from vigia import jobs

    gh, build = world
    ctx, _ = build([APPROVE])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx)
    pr_n = next(iter(gh.prs_))
    assert jobs.still_due(ctx, "gate", pr_n)[0]
    _move_base(
        ctx, gh, "NOTES.md", "moved\n"
    )  # after the watcher spawned the job, before it spends a session
    due, _, decision = jobs.still_due(ctx, "gate", pr_n)
    assert not due and decision.action == "update"
