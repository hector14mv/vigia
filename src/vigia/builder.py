"""`vigia run`: a ready issue becomes a pull request. Also the fix round on an open PR.

The engine only edits files in a throwaway worktree. vigia itself commits, pushes, opens the PR and
writes the handoff, so the steps that touch GitHub are deterministic and the same for every engine.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import comments, decisions, jobs, prompts, tickets
from .context import Ctx
from .engines import Outcome, handoff_body, handoff_commit
from .proc import CommandError, run

HANDOFF_FILE = ".vigia-handoff.md"
SUMMARY = re.compile(r"\b\d+ (passed|failed|errors?)\b|\bok\b|\bFAIL", re.I)


def _take_handoff(wt: Path) -> str:
    path = wt / HANDOFF_FILE
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    path.unlink()
    return text


def _run_tests(ctx: Ctx, wt: Path) -> str:
    cmd = ctx.cfg.test_command
    if not cmd:
        return "not configured (`checks.test_command`)"
    try:
        result = run(["sh", "-c", cmd], cwd=wt, check=False, timeout=900)
    except CommandError as exc:
        return f"`{cmd}` → {exc}"
    lines = [line.strip() for line in (result.stdout + result.stderr).splitlines() if line.strip()]
    # Prefer the runner's summary line ("16 passed in 0.02s") over a row of progress dots.
    summary = next((line for line in reversed(lines) if SUMMARY.search(line)), lines[-1] if lines else "")
    status = "passed" if result.code == 0 else f"FAILED (exit {result.code})"
    return f"`{cmd}` → {status}" + (f" — {summary[:160]}" if summary else "")


def refuse(ctx: Ctx, issue: dict, gaps: list[str]) -> None:
    """A ready issue that cannot be built: say why once, and take it out of the queue."""
    labels = ctx.cfg.labels
    ctx.gh.comment(
        issue["number"],
        "vigia cannot build this ticket yet. Missing:\n"
        + "\n".join(f"- {g}" for g in gaps)
        + f"\n\nAdd what is missing and put the `{labels.ready}` label back.\n",
    )
    ctx.gh.remove_label(issue["number"], labels.ready)
    ctx.ledger.append("refused", issue=issue["number"], gaps=gaps)
    ctx.say(f"#{issue['number']}: not launchable — {'; '.join(gaps)}")


def gaps(ctx: Ctx, issue: dict) -> list[str]:
    found = tickets.missing(issue["body"])
    if tickets.tier(issue["labels"], ctx.cfg.labels.tier_prefix) is None:
        found.append(
            "exactly one tier label (" + ", ".join(ctx.cfg.labels.tier(t) for t in ctx.cfg.tiers) + ")"
        )
    return found


def unanswered_decisions(ctx: Ctx) -> dict[int, list[int]]:
    """Issue or PR number -> the open decision issues that block it and have no recorded answer.
    Raises CommandError when the decisions cannot be read: the caller holds the work rather than guess."""
    answered = {r.get("issue") for r in ctx.ledger.events("decided")}
    waits: dict[int, list[int]] = {}
    for d in ctx.gh.issues(decisions.LABEL):
        if d["number"] not in answered:
            # `field`, not `parse`: a decision whose body is malformed still blocks what it says it blocks
            for n in decisions.refs(decisions.field(d["body"], "Blocks")):
                waits.setdefault(n, []).append(d["number"])
    return waits


def blockers(ctx: Ctx, issue: dict, waits: dict[int, list[int]] | None = None) -> list[int]:
    """The open items that keep `issue` from being built: tickets in its `Blocked by:` line that are still open
    and decision issues that name it in `Blocks:` and are not answered. Raises CommandError when that cannot be
    read."""
    waits = unanswered_decisions(ctx) if waits is None else waits
    held = [n for n in decisions.blocked_by(issue["body"]) if n != issue["number"] and ctx.gh.item_open(n)]
    return sorted(set(held) | set(waits.get(issue["number"], [])))


def next_issue(ctx: Ctx, skip: set[int] | frozenset[int] = frozenset()) -> dict | None:
    """The oldest ready issue that is launchable, other than those in `skip` (already being built).
    Blocked ones (an open `Blocked by:` ticket, an unanswered decision) wait; non-launchable ones are refused
    on the way."""
    labels = ctx.cfg.labels
    waits: dict[int, list[int]] | None = None
    for issue in ctx.gh.issues(labels.ready):
        if issue["number"] in skip or labels.building in issue["labels"] or labels.parked in issue["labels"]:
            continue
        try:
            waits = unanswered_decisions(ctx) if waits is None else waits
            held = blockers(ctx, issue, waits)
        except CommandError as exc:  # unknown is not free: hold it until the next tick can read
            ctx.say(f"#{issue['number']}: not building — could not read what blocks it ({exc})")
            continue
        if held:
            ctx.say(f"#{issue['number']}: not building — blocked by {', '.join(f'#{n}' for n in held)}")
            continue
        problems = gaps(ctx, issue)
        if problems:
            refuse(ctx, issue, problems)
            continue
        return issue
    return None


def _fail_issue(ctx: Ctx, issue: dict, reason: str, outcome: Outcome | None) -> None:
    labels = ctx.cfg.labels
    detail = (
        f" Session: exit {outcome.code}{', timed out' if outcome.timed_out else ''}, {outcome.seconds:.0f} s."
        if outcome
        else ""
    )
    ctx.gh.comment(
        issue["number"],
        f"vigia could not build this ticket: {reason}.{detail}\n\n"
        f"Remove `{labels.parked}` and add `{labels.ready}` to try again.\n",
    )
    ctx.gh.remove_label(issue["number"], labels.building)
    ctx.gh.add_labels(issue["number"], labels.parked)
    ctx.ledger.append("build_failed", issue=issue["number"], reason=reason)
    ctx.say(f"#{issue['number']}: build failed — {reason}")


def enroll(ctx: Ctx, pr: int, issue: int, tier: str) -> None:
    """Make a PR vigia opened visible to the watcher (its labels) and release the issue.

    Idempotent, and `enrolled` is recorded last: if any step fails, `recover` repeats all of them.
    """
    labels = ctx.cfg.labels
    ctx.gh.add_labels(pr, labels.review, labels.tier(tier))
    ctx.gh.remove_label(issue, labels.building)
    ctx.ledger.append("enrolled", pr=pr, issue=issue)


def _unfinished_builds(ctx: Ctx) -> list[int]:
    """Issues whose latest `build_started` has no `pr_opened` or `build_failed` after it."""
    open_builds: dict[int, dict] = {}
    for row in ctx.ledger.rows():
        if row["event"] == "build_started":
            open_builds[row["issue"]] = row
        elif row["event"] in ("pr_opened", "build_failed") and row.get("issue") in open_builds:
            del open_builds[row["issue"]]
    return sorted(open_builds)


def record_head(ctx: Ctx, n: int, sha: str) -> bool:
    """Record that vigia has seen `sha` as PR `n`'s head. False if that could not be recorded.

    The row carries the id of the latest `human_ok` event GitHub has *now*, read fresh from the event
    history (never from a label snapshot taken earlier, which a label added meanwhile would not be in).
    `watch.human_approved` accepts only a label event with a higher id, so an approval given before this
    head was recorded never counts for it, and no local clock is compared with GitHub's. Without a row
    for the current head nothing counts as approved.
    """
    try:
        event = ctx.gh.label_event(n, ctx.cfg.labels.human_ok)
    except CommandError as exc:
        ctx.ledger.append("error", pr=n, action="read_human_ok", detail=str(exc)[:500])
        ctx.say(f"PR #{n}: could not read the `{ctx.cfg.labels.human_ok}` history — retrying next tick")
        return False
    baseline = (event or {}).get("id") or 0
    ctx.ledger.append("head_seen", pr=n, sha=sha, human_ok_event=baseline)
    return True


def recover(ctx: Ctx) -> None:
    """Repair what an interrupted step left behind. Runs at the start of every tick, under the lock;
    anything a live job owns (vigia.jobs) is not interrupted and is left alone.

    1. A build that started but neither opened a PR nor failed was interrupted (a crash, a failed label
       call): its issue is parked with the reason, never left outside the queue.
    2. A PR whose label step failed after `pr_opened` was recorded is enrolled (next paragraph).
    """
    for number in _unfinished_builds(ctx):
        # Hold the issue's lock, then read the ledger again: a job that ended between the first read and
        # here has recorded `pr_opened` or `build_failed`, and must not be parked from the stale list.
        with jobs.hold(ctx, jobs.target("build", number)) as got:
            if not got or number not in _unfinished_builds(ctx):
                continue  # a live build job, or one that finished meanwhile: not interrupted
            try:
                issue = ctx.gh.issue(number)
                if issue["state"] == "OPEN":
                    _fail_issue(ctx, issue, "the build was interrupted before it opened a pull request", None)
                else:
                    ctx.ledger.append("build_failed", issue=number, reason="interrupted; the issue is closed")
            except CommandError as exc:  # the next tick tries again
                ctx.ledger.append("error", issue=number, action="recover_build", detail=str(exc)[:500])
                ctx.say(f"#{number}: recovering the interrupted build failed — {exc}")
    _enroll_pending(ctx)


def _enroll_pending(ctx: Ctx) -> None:
    """Finish the enrollment of PRs whose label step failed after `pr_opened` was recorded.

    The watcher finds PRs only through the review label, so such a PR would otherwise never be seen.
    Only an issue still marked `building` with its PR still open is touched: anything else was
    enrolled before `enrolled` events existed, or has moved on.
    """
    done = {r["pr"] for r in ctx.ledger.events("enrolled")}
    for row in ctx.ledger.events("pr_opened"):
        pr, issue = row["pr"], row.get("issue")
        if pr in done:
            continue
        done.add(pr)
        try:
            if (
                issue
                and ctx.gh.pr(pr)["state"] == "OPEN"
                and ctx.cfg.labels.building in ctx.gh.issue(issue)["labels"]
            ):
                enroll(ctx, pr, issue, row["tier"])
                ctx.say(f"PR #{pr}: finished enrolling after an earlier failure")
            else:
                ctx.ledger.append("enrolled", pr=pr, issue=issue)
        except CommandError as exc:  # the next tick tries again
            ctx.ledger.append("error", pr=pr, issue=issue, action="enroll", detail=str(exc)[:500])
            ctx.say(f"PR #{pr}: enrolling failed — {exc}")


def build(ctx: Ctx, issue: dict) -> int | None:
    """Build one launchable issue. Returns the new PR number, or None if the build failed."""
    cfg, labels, n = ctx.cfg, ctx.cfg.labels, issue["number"]
    tier = tickets.tier(issue["labels"], labels.tier_prefix)
    assert tier is not None
    seat = cfg.policy(tier).builder
    attempt = 1 + len(ctx.ledger.events("build_started", issue=n))
    name = tickets.slug(issue["title"])
    branch = f"vigia/{n}-{name}" + (f"-r{attempt}" if attempt > 1 else "")

    # The ledger first: if anything below fails, `recover` finds a build that never ended and parks it.
    ctx.ledger.append(
        "build_started", issue=n, tier=tier, engine=seat.engine, model=seat.model, branch=branch
    )
    ctx.say(f"#{n}: building with {seat.label()} on {branch}")
    try:
        ctx.gh.add_labels(n, labels.building)
        ctx.gh.remove_label(n, labels.ready)
        base_sha = ctx.git.remote_branch_sha(cfg.base)
        wt = ctx.git.worktree_detached(ctx.root / f"build-{n}-a{attempt}", base_sha)
    except CommandError as exc:
        _fail_issue(ctx, issue, f"vigia could not start the build ({exc})", None)
        return None
    pr: int | None = None
    try:
        prompt = prompts.builder(cfg.repo, cfg.base, issue, tier, cfg.test_command)
        outcome = ctx.engines.run(seat, "builder", prompt, wt, name=f"build-{n}-a{attempt}")
        notes = _take_handoff(wt)
        if not outcome.ok:
            _fail_issue(ctx, issue, "the builder session did not finish cleanly", outcome)
            return None
        tests = _run_tests(ctx, wt)
        if ctx.git.dirty(wt):
            ctx.git.commit_all(wt, handoff_commit(notes, f"feat: {issue['title']} (#{n})"))
        if ctx.git.count(wt, base_sha) == 0:
            _fail_issue(ctx, issue, "the builder left no changes", outcome)
            return None
        head = ctx.git.rev("HEAD", cwd=wt)
        files = ctx.git.changed_files(wt, base_sha)
        ctx.git.push(wt, branch)
        policy = cfg.policy(tier)
        body = (
            f"Closes #{n}\n\n"
            f"Built by vigia from #{n} with {seat.label()} (lab {seat.family}). "
            f"Tier {tier}: {prompts.TIER_MEANING[tier]}.\n\n"
            f"What happens next: CI, then a review by the other lab on the exact head"
            + (", then a stronger reviewer" if policy.reviewer else "")
            + (f", then a human label `{labels.human_ok}`" if policy.human else "")
            + ", then a merge by a script with no AI.\n"
        )
        pr = ctx.gh.create_pr(branch, cfg.base, issue["title"], body)
        # The lineage goes to the ledger first: whatever fails after this, vigia knows who built the PR.
        ctx.ledger.append(
            "pr_opened",
            pr=pr,
            issue=n,
            sha=head,
            branch=branch,
            tier=tier,
            engine=seat.engine,
            model=seat.model,
            family=seat.family,
            seconds=outcome.seconds,
        )
        record_head(ctx, pr, head)
        enroll(ctx, pr, n, tier)
        handoff = comments.handoff(
            head, seat, "builder", n, tier, outcome.seconds, outcome.code, tests, files, handoff_body(notes)
        )
        ctx.gh.comment(pr, handoff)
        ctx.say(f"#{n}: opened PR #{pr} at {head[:8]}")
        return pr
    except CommandError as exc:
        if pr is None:
            _fail_issue(ctx, issue, f"vigia failed after the session ({exc})", None)
            return None
        ctx.ledger.append("error", pr=pr, issue=n, detail=str(exc)[:500])
        raise
    finally:
        ctx.git.remove_worktree(wt)


def _ci_log(ctx: Ctx, pr: dict) -> str:
    try:
        return ctx.gh.failed_log(pr["branch"], pr["head"])
    except CommandError:
        return ""


def fix(ctx: Ctx, pr: dict, stage: str) -> bool:
    """One fresh fix round on `pr`'s exact head. True when a new head was pushed."""
    cfg, labels, n, sha = ctx.cfg, ctx.cfg.labels, pr["number"], pr["head"]
    opened = ctx.ledger.last("pr_opened", pr=n) or {}
    tier = tickets.tier(pr["labels"], labels.tier_prefix)
    if tier is None:
        return False
    seat = cfg.policy(tier).builder
    family = ctx.builder_family(n)
    if family != seat.family:
        ctx.ledger.append(
            "engine_failed",
            pr=n,
            sha=sha,
            stage=stage,
            reason=f"the {tier} builder is {seat.engine} (lab {seat.family}), but this PR was built by lab {family}",
        )
        return False
    if ctx.git.remote_branch_sha(pr["branch"]) != sha:
        ctx.say(f"PR #{n}: head moved; skipping this fix round")
        return False
    findings: list[dict] = []
    if stage in ("gate", "review"):
        last = ctx.ledger.last("verdict", pr=n, sha=sha, kind=stage)
        findings = (last or {}).get("findings") or []
    issue = None
    if opened.get("issue") or tickets.linked_issue(pr["body"]):
        try:
            issue = ctx.gh.issue(opened.get("issue") or tickets.linked_issue(pr["body"]))
        except CommandError:
            issue = None
    k = 1 + len(ctx.ledger.events("fix_started", pr=n))
    ctx.ledger.append("fix_started", pr=n, sha=sha, stage=stage, engine=seat.engine, model=seat.model)
    ctx.say(f"PR #{n}: fix round {k} ({stage}) with {seat.label()}")
    wt = ctx.git.worktree_detached(ctx.root / f"fix-{n}-{sha[:8]}-{k}", sha)
    try:
        prompt = prompts.fixer(
            cfg.repo, pr, issue, stage, findings, _ci_log(ctx, pr) if stage == "ci" else "", cfg.test_command
        )
        outcome = ctx.engines.run(seat, "builder", prompt, wt, name=f"fix-{n}-{sha[:8]}-{k}")
        notes = _take_handoff(wt)
        if outcome.ok and ctx.git.dirty(wt):
            ctx.git.commit_all(wt, handoff_commit(notes, f"fix: address {stage} findings (#{n})"))
        if not outcome.ok or ctx.git.count(wt, sha) == 0:
            reason = (
                "the fixer session did not finish cleanly" if not outcome.ok else "the fixer left no changes"
            )
            ctx.ledger.append("engine_failed", pr=n, sha=sha, stage=stage, reason=reason)
            ctx.say(f"PR #{n}: {reason}")
            return False
        tests = _run_tests(ctx, wt)
        head = ctx.git.rev("HEAD", cwd=wt)
        files = ctx.git.changed_files(wt, sha)
        try:
            ctx.git.push(wt, pr["branch"])
        except CommandError as exc:
            ctx.ledger.append("engine_failed", pr=n, sha=sha, stage=stage, reason=f"push failed: {exc}"[:500])
            ctx.say(f"PR #{n}: push failed — {exc}")
            return False
        ctx.ledger.append("pushed", pr=n, from_sha=sha, sha=head, stage=stage)
        # Recording the new head is what invalidates an earlier human approval (watch.human_approved);
        # removing the stale label is only a courtesy to the people reading the PR.
        record_head(ctx, n, head)
        if labels.human_ok in pr["labels"]:
            try:
                ctx.gh.remove_label(n, labels.human_ok)
            except CommandError as exc:
                ctx.ledger.append("error", pr=n, action="clear_human_ok", detail=str(exc)[:500])
        ctx.gh.comment(
            n,
            comments.handoff(
                head,
                seat,
                f"fixer (round {k}, {stage})",
                opened.get("issue"),
                tier,
                outcome.seconds,
                outcome.code,
                tests,
                files,
                handoff_body(notes),
            ),
        )
        ctx.say(f"PR #{n}: pushed fix {head[:8]}")
        return True
    finally:
        ctx.git.remove_worktree(wt)
