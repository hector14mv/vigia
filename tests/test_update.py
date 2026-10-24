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
