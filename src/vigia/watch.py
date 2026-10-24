"""`vigia watch`: one tick reads GitHub and the ledger, decides with `rules.decide()`, and acts.

Cheap actions (merge, park) run for every PR in the tick. At most one engine session runs per
tick (a gate, a review, a fix or a build), oldest PR first, so vigia never has two models working
on the same repository at once in v0.1.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from . import builder, comments, gate, jobs, tickets
from .context import Ctx
from .ledger import utcnow
from .proc import CommandError
from .rules import Decision, PRState, Verdict, decide


@dataclass
class Seen:
    pr: dict
    state: PRState
    decision: Decision


def _window(ctx: Ctx, n: int) -> list[dict]:
    """This PR's ledger rows since it was last unparked (a human's fresh start resets every counter)."""
    rows = [r for r in ctx.ledger.rows() if r.get("pr") == n]
    start = max((i for i, r in enumerate(rows) if r["event"] == "unparked"), default=-1)
    return rows[start + 1 :]


def _track(ctx: Ctx, pr: dict) -> None:
    """Record head changes and unparks.

    Recording a new head is what invalidates an earlier human approval: the row stores the id of the
    latest `human_ok` event GitHub had then, and `human_approved()` only accepts a label event with a
    higher id (both ids are GitHub's, so no local clock is involved). Removing the stale label is a
    courtesy to the people reading the PR; if the removal fails, the stale label still counts for nothing.
    """
    n, labels = pr["number"], ctx.cfg.labels
    seen = ctx.ledger.last("head_seen", pr=n)
    moved = seen is None or seen.get("sha") != pr["head"]
    if moved or "human_ok_event" not in seen:  # a row from before the baseline existed is recorded again
        if not builder.record_head(ctx, n, pr["head"]):
            return
        if moved and seen is not None and labels.human_ok in pr["labels"]:
            try:
                ctx.gh.remove_label(n, labels.human_ok)
                ctx.gh.comment(
                    n,
                    f"New head `{pr['head']}`: the `{labels.human_ok}` label was for the previous head "
                    "and has been removed. Add it again after checking this head.\n",
                )
            except CommandError as exc:
                ctx.ledger.append("error", pr=n, action="clear_human_ok", detail=str(exc)[:500])
                ctx.say(f"PR #{n}: could not remove the stale `{labels.human_ok}` — it counts for nothing")
    rows = [r for r in ctx.ledger.rows() if r.get("pr") == n and r["event"] in ("parked", "unparked")]
    if rows and rows[-1]["event"] == "parked" and labels.parked not in pr["labels"]:
        ctx.ledger.append("unparked", pr=n, sha=pr["head"])


def human_approved(ctx: Ctx, pr: dict) -> bool:
    """True when the human label was added after vigia first saw the PR's current head (by one of
    `checks.humans`, when that list is set). A label from before the head moved never counts, whether or
    not it could be removed. "After" is GitHub's event order, never a comparison of local and GitHub
    clocks; a head recorded without that baseline (an older ledger) is re-recorded, so the label must be added again."""
    label = ctx.cfg.labels.human_ok
    if label not in pr["labels"]:
        return False
    seen = ctx.ledger.last("head_seen", pr=pr["number"])
    if seen is None or seen.get("sha") != pr["head"]:
        return False
    event = ctx.gh.label_event(pr["number"], label)
    baseline = seen.get("human_ok_event")
    if event is None or baseline is None or not event.get("id") or event["id"] <= baseline:
        return False
    return not ctx.cfg.humans or event["actor"] in ctx.cfg.humans


def state(ctx: Ctx, pr: dict) -> PRState:
    cfg, n = ctx.cfg, pr["number"]
    window = _window(ctx, n)
    human_ok = human_approved(ctx, pr)
    tier = tickets.tier(pr["labels"], cfg.labels.tier_prefix)
    return PRState(
        number=n,
        head=pr["head"],
        tier=tier,
        builder_family=ctx.builder_family(n),
        ci=pr["ci"],
        mergeable=pr["mergeable"],
        open=pr["state"] == "OPEN",
        draft=pr["draft"],
        parked=cfg.labels.parked in pr["labels"],
        human_ok=human_ok,
        on_base=pr["base"] == cfg.base,
        verdicts=tuple(
            Verdict(r["kind"], r["sha"], r["verdict"], r.get("p1", 0), r.get("p2", 0))
            for r in window
            if r["event"] == "verdict"
            and r.get("tier") == tier  # a verdict covers only the tier it was run for
        ),
        fix_rounds=sum(1 for r in window if r["event"] == "fix_started"),
        failures=sum(1 for r in window if r["event"] == "engine_failed" and r.get("sha") == pr["head"]),
    )


def sense(ctx: Ctx, track: bool = True) -> list[Seen]:
    out = []
    for pr in ctx.gh.prs(ctx.cfg.labels.review):
        if track:
            _track(ctx, pr)
        st = state(ctx, pr)
        out.append(Seen(pr, st, decide(st, ctx.cfg)))
    return out


def _why(ctx: Ctx, n: int, sha: str, tier: str) -> list[str]:
    why = ["CI green on this head"]
    for kind in ("gate", "review"):
        v = ctx.ledger.last("verdict", pr=n, kind=kind, sha=sha, tier=tier)
        if v:
            what = "cross-lab gate" if kind == "gate" else f"{tier} reviewer"
            why.append(
                f"{what}: {v['verdict']} by {v['engine']} · {v['model']} (lab {v['family']}), "
                f"{v.get('p1', 0)} P1 · {v.get('p2', 0)} P2"
            )
    if ctx.cfg.policy(tier).human:
        why.append(f"human label `{ctx.cfg.labels.human_ok}` on this head")
    why.append("`gh pr merge --squash --match-head-commit` — no model ran this step")
    return why


def merge(ctx: Ctx, seen: Seen) -> bool:
    n, sha = seen.pr["number"], seen.decision.sha
    fresh = ctx.gh.pr(n)  # re-read and re-decide right before the merge: the tick's snapshot may be old
    st = state(ctx, fresh)
    again = decide(st, ctx.cfg)
    if ctx.cfg.labels.review not in fresh["labels"]:  # an operator withdrew the PR from automation
        ctx.say(f"PR #{n}: not merging — `{ctx.cfg.labels.review}` was removed")
        return False
    if again.action != "merge" or fresh["head"] != sha:
        ctx.say(f"PR #{n}: not merging — {again}")
        return False
    ctx.gh.merge(n, sha)
    opened = ctx.ledger.last("pr_opened", pr=n) or {}
    ctx.ledger.append("merged", pr=n, sha=sha, tier=st.tier, issue=opened.get("issue"))
    ctx.gh.delete_branch(fresh["branch"])
    ctx.gh.comment(n, comments.merged(sha, st.tier or "?", _why(ctx, n, sha, st.tier or "T2")))
    ctx.say(f"PR #{n}: merged {sha[:8]}")
    return True


def park(ctx: Ctx, seen: Seen) -> None:
    """Park, after deciding again on a fresh read under the PR's lock: a job that just finished may have pushed
    a new head since the tick's snapshot, and a stale park would stop a PR that is fine."""
    n = seen.pr["number"]
    with jobs.hold(ctx, jobs.target("gate", n)) as got:
        if not got:
            ctx.say(f"PR #{n}: not parking — a job is working on it")
            return
        fresh = ctx.gh.pr(n)
        d = decide(state(ctx, fresh), ctx.cfg)
        if d.action != "park" or fresh["head"] != seen.decision.sha:
            ctx.say(f"PR #{n}: not parking — {d}")
            return
        _park(ctx, n, d)


def _park(ctx: Ctx, n: int, d: Decision) -> None:
    labels = ctx.cfg.labels
    ctx.gh.add_labels(n, labels.parked)
    # The record comes before the comment: once the label is on, `_track` needs the `parked` row to
    # notice the unpark, so a failed comment must not lose it.
    ctx.ledger.append("parked", pr=n, sha=d.sha, reason=d.reason)
    ctx.say(f"PR #{n}: parked — {d.reason}")
    try:
        ctx.gh.comment(n, comments.parked(d.sha, d.reason, labels.parked))
    except CommandError as exc:
        ctx.ledger.append("error", pr=n, action="park_comment", detail=str(exc)[:500])
        ctx.say(f"PR #{n}: parked, but the explaining comment failed — {exc}")


def tick(
    ctx: Ctx, build: bool | None = None, slots: int | None = None, parallel: bool | None = None
) -> list[Seen]:
    """One pass. Returns what was seen and decided.

    `slots` (default `watch.slots`) is how many engine sessions may run at once. With one, the tick runs at
    most one session itself, inline. With more, it starts `vigia job` processes for the free slots and
    returns without waiting (vigia.jobs); a PR or issue that already has a live job is left alone. A workspace
    passes `parallel=True` with this repo's share of the workspace's slots, which can be zero.
    """
    build = ctx.cfg.build_in_watch if build is None else build
    slots = ctx.cfg.slots if slots is None else slots
    parallel = slots > 1 if parallel is None else parallel
    with ctx.lock():
        (ctx.state / "heartbeat").write_text(
            utcnow() + "\n", encoding="utf-8"
        )  # the Mesa's "watcher N s ago"
        builder.recover(ctx)
        gate.recover(ctx)
        seen = sense(ctx)
        running = set(jobs.live(ctx))
        for s in seen:  # a live job owns its PR: no merge, park or second session until it ends
            if jobs.target("gate", s.pr["number"]) in running:
                s.decision = Decision("wait", "a job is working on it", s.pr["head"])
            elif _still_building(ctx, s.pr["number"], running):
                s.decision = Decision("wait", "its build job has not finished its handoff", s.pr["head"])
        for s in seen:  # report every decision first, so the output reads in the order things happen
            ctx.say(f"PR #{s.pr['number']}: {s.decision}")
        for s in seen:
            try:
                if s.decision.action == "merge":
                    merge(ctx, s)
                elif s.decision.action == "park":
                    park(ctx, s)
            except CommandError as exc:  # one PR's failure must not stop the others; the next tick re-decides
                ctx.ledger.append("error", pr=s.pr["number"], action=s.decision.action, detail=str(exc)[:500])
                ctx.say(f"PR #{s.pr['number']}: {s.decision.action} failed — {exc}")
        if parallel:
            _spawn_jobs(ctx, seen, build, slots - len(running), running)
            return seen
        if running:  # one slot, and a job from an earlier parallel run still holds it
            return seen
        for s in seen:  # inline, through jobs.run too: same target lock and same last check as a job
            d = s.decision
            if d.action in ("gate", "review", "fix"):
                jobs.run(ctx, d.action, s.pr["number"], d.stage)
                return seen
        if build:
            issue = builder.next_issue(ctx, skip=_building(running))
            if issue is not None:
                jobs.run(ctx, "build", issue["number"])
        return seen


def _still_building(ctx: Ctx, n: int, running: set[str]) -> bool:
    """True while the build job that opened PR `n` still holds its issue: the PR is labelled before the build
    posts its handoff comment, and a gate started in that gap would see a PR with no known builder."""
    issue = (ctx.ledger.last("pr_opened", pr=n) or {}).get("issue")
    return issue is not None and jobs.target("build", issue) in running


def _building(running: set[str]) -> set[int]:
    return {int(name.split("-", 1)[1]) for name in running if name.startswith("issue-")}


def _spawn_jobs(ctx: Ctx, seen: list[Seen], build: bool, free: int, running: set[str]) -> None:
    """Fill the free slots: open PRs first, oldest first (they are closest to a merge), then new builds."""
    for s in seen:
        if free <= 0:
            return
        d = s.decision
        if d.action in ("gate", "review", "fix") and jobs.spawn(ctx, d.action, s.pr["number"], d.stage):
            free -= 1
    skip = _building(running)
    while build and free > 0:
        issue = builder.next_issue(ctx, skip=skip)
        if issue is None:
            return
        skip.add(issue["number"])
        if jobs.spawn(ctx, "build", issue["number"]):
            free -= 1


def loop(ctx: Ctx, interval: int, build: bool | None = None) -> None:
    ctx.say(f"vigia watching {ctx.cfg.repo} every {interval} s (Ctrl-C to stop)")
    while True:
        started = time.monotonic()
        try:
            tick(ctx, build)
        except Exception as exc:  # one bad tick must not stop the watcher; the next one starts clean
            ctx.ledger.append("error", detail=str(exc)[:500])
            ctx.say(f"tick failed: {exc}")
        time.sleep(max(5, interval - (time.monotonic() - started)))


def workspace_tick(ws, contexts: list[Ctx], build: bool | None = None) -> None:
    """One pass over every repo of a workspace, sharing `ws.slots` engine sessions between them."""
    from . import workspace

    with workspace.lock(ws):  # one watcher per workspace: the shared budget is checked and spent in one place
        for ctx in contexts:
            try:
                live = workspace.live_by_repo(contexts)
                if ws.slots <= 1 and not sum(live.values()):  # the workspace's one slot wins over watch.slots
                    tick(ctx, build, slots=1, parallel=False)
                    continue
                tick(
                    ctx, build, slots=workspace.allowance(ws.slots, live, ctx.clone.resolve()), parallel=True
                )
            except Exception as exc:  # one repo's failure must not stop the others
                ctx.ledger.append("error", detail=str(exc)[:500])
                ctx.say(f"{ctx.cfg.repo}: tick failed — {exc}")


def workspace_loop(ws, contexts: list[Ctx], interval: int, build: bool | None = None) -> None:
    names = ", ".join(c.cfg.repo for c in contexts)
    print(f"vigia watching {names} with {ws.slots} slot(s), every {interval} s (Ctrl-C to stop)")
    while True:
        started = time.monotonic()
        workspace_tick(ws, contexts, build)
        time.sleep(max(5, interval - (time.monotonic() - started)))
