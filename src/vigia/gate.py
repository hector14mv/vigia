"""The cross-lab gate and the tier reviewer: a read-only session on the PR's exact head.

The verdict is recorded against the full SHA the reviewer saw. If the head moves, that verdict no
longer covers the PR, and the decision table asks for a new one.
"""

from __future__ import annotations

from . import comments, jobs, prompts, tickets
from .context import Ctx
from .engines import parse_verdict
from .proc import CommandError


def _issue(ctx: Ctx, pr: dict) -> dict | None:
    opened = ctx.ledger.last("pr_opened", pr=pr["number"]) or {}
    number = opened.get("issue") or tickets.linked_issue(pr["body"])
    if not number:
        return None
    try:
        return ctx.gh.issue(number)
    except CommandError:
        return None


def recover(ctx: Ctx) -> None:
    """Remove worktrees an interrupted session left behind. Runs under the tick lock; a worktree whose PR
    has a live job (vigia.jobs) belongs to a review in progress and is kept, and so is a planner's while a
    `vigia plan` holds its lock."""
    for path in sorted(ctx.root.glob("gate-*")) + sorted(ctx.root.glob("review-*")):
        number = path.name.split("-")[1]
        if number.isdigit() and jobs.busy(ctx, jobs.target("gate", int(number))):
            continue
        ctx.git.remove_worktree(path)
    if not jobs.busy(ctx, jobs.PLANNER):
        for path in sorted(ctx.root.glob("plan-*")):
            ctx.git.remove_worktree(path)


def review(ctx: Ctx, pr: dict, kind: str, round_no: int) -> str | None:
    """Run the gate (`kind="gate"`) or the tier reviewer (`kind="review"`) on `pr`'s head.

    Returns the recorded verdict (APPROVE, CHANGES or UNAVAILABLE), or None if nothing ran.
    """
    cfg, n, sha = ctx.cfg, pr["number"], pr["head"]
    tier = tickets.tier(pr["labels"], cfg.labels.tier_prefix)
    if tier is None:
        return None
    policy = cfg.policy(tier)
    seat = policy.gate if kind == "gate" else policy.reviewer
    if seat is None:
        return None
    if (
        pr["base"] != cfg.base
    ):  # the diff below is computed against cfg.base; it is wrong for any other target
        ctx.say(f"PR #{n}: targets `{pr['base']}`, not `{cfg.base}`; no review")
        return None
    builder_family = ctx.builder_family(n)
    if kind == "gate" and builder_family in (None, seat.family):
        ctx.say(
            f"PR #{n}: refusing the gate: {seat.engine} (lab {seat.family}) reviewing work of lab {builder_family}"
        )
        return None
    if ctx.git.remote_branch_sha(pr["branch"]) != sha:
        ctx.say(f"PR #{n}: head moved; the review waits for the next tick")
        return None

    base_sha = ctx.git.remote_branch_sha(cfg.base)
    merge_base = ctx.git.merge_base(base_sha, sha)
    diff = ctx.git.diff(merge_base, sha)
    thread = ctx.gh.comments(n)
    handoff = comments.handoff_for(thread, sha, ctx.gh.login())
    earlier = [
        e for e in ctx.ledger.events("verdict", pr=n, kind="gate") if e.get("sha") != sha and e.get("p1")
    ]
    previous = (earlier[-1].get("findings") or []) if earlier else []
    gate_findings: list[dict] = []
    if kind == "review":
        on_head = ctx.ledger.last("verdict", pr=n, kind="gate", sha=sha, tier=tier) or {}
        gate_findings = on_head.get("findings") or []

    attempt = 1 + len(ctx.ledger.events("verdict", pr=n, kind=kind, sha=sha))
    name = f"{kind}-{n}-{sha[:8]}-{attempt}"
    ctx.say(f"PR #{n}: {kind} round {round_no} on {sha[:8]} with {seat.label()}")
    if (ctx.root / name).exists():  # left by an interrupted run of this very review
        ctx.git.remove_worktree(ctx.root / name)
    wt = ctx.git.worktree_detached(ctx.root / name, sha)
    try:
        prompt = prompts.reviewer(
            kind, cfg.repo, pr, tier, sha, merge_base, _issue(ctx, pr), handoff, diff, previous, gate_findings
        )
        outcome = ctx.engines.run(seat, "reviewer", prompt, wt, name=name)
    finally:
        ctx.git.remove_worktree(wt)
    parsed = parse_verdict(outcome.text) if outcome.code == 0 and not outcome.timed_out else None
    failure = ""
    if parsed is None:
        failure = (
            "timed out"
            if outcome.timed_out
            else f"exit {outcome.code}, no parsable verdict (log: {outcome.log.name})"
        )
    verdict = parsed["verdict"] if parsed else "UNAVAILABLE"
    ctx.ledger.append(
        "verdict",
        pr=n,
        kind=kind,
        sha=sha,
        tier=tier,
        verdict=verdict,
        round=round_no,
        p1=parsed["p1"] if parsed else 0,
        p2=parsed["p2"] if parsed else 0,
        findings=parsed["findings"] if parsed else [],
        engine=seat.engine,
        model=seat.model,
        family=seat.family,
        seconds=outcome.seconds,
    )
    # The ledger first: if posting the comment fails, the verdict still counts and the review is not re-run.
    ctx.gh.comment(
        n, comments.verdict(kind, round_no, sha, seat, builder_family, parsed, outcome.seconds, failure)
    )
    ctx.say(f"PR #{n}: {kind} {verdict}" + (f" ({parsed['p1']} P1, {parsed['p2']} P2)" if parsed else ""))
    return verdict
