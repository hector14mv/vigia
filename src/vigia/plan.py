"""`vigia plan "<goal>"`: a planner session turns a goal into tickets in the template, for a person to ratify.

The planner reads the base branch (read-only, like a reviewer) and answers with JSON. vigia files every ticket it
writes as `vigia:proposed`, never as ready: a person reads it and swaps the label for the ready one, because what
the factory builds next is a person's call. A ticket that needs an earlier one of the plan merged first says
which. Work only a person can do (an account, a secret, a decision) becomes a `vigia:human-task` issue. The Mesa
shows both under "Your turn". A title that is already an open issue's, or repeats within the plan, is not filed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import jobs, tickets
from .config import HUMAN_TASK, PROPOSED, TIERS, Seat
from .context import Ctx
from .init import label_specs

DEFAULT_SEAT = Seat("claude", "claude-opus-5-5", "high")

PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["tickets", "human_tasks"],
    "properties": {
        "tickets": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "tier", "depends_on_earlier", "body"],
                "properties": {
                    "title": {"type": "string"},
                    "tier": {"type": "string", "enum": list(TIERS)},
                    "depends_on_earlier": {"type": "boolean"},
                    "body": {"type": "string"},
                },
            },
        },
        "human_tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "body"],
                "properties": {"title": {"type": "string"}, "body": {"type": "string"}},
            },
        },
    },
}


def _skill(clone: Path) -> str:
    """The ticket-writing guide: the repo's own vigia-ticket skill when it has one, else the template."""
    for path in (
        clone / "skills" / "vigia-ticket" / "SKILL.md",
        clone / ".claude" / "skills" / "vigia-ticket" / "SKILL.md",
    ):
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return "Write each ticket body in exactly this template:\n\n" + strip_front_matter(tickets.TEMPLATE)


def strip_front_matter(text: str) -> str:
    """Drop a leading `---` YAML block (GitHub's issue-template header): it belongs to the template file, not to
    a ticket's body."""
    match = re.match(r"\s*---\n.*?\n---\n", text, re.S)
    return text[match.end() :].lstrip() if match else text


def prompt(
    goal: str, repo: str, base: str, skill: str, open_titles: list[str], files: list[str], limit: int
) -> str:
    listing = "\n".join(files[:400]) + (f"\n… and {len(files) - 400} more files" if len(files) > 400 else "")
    already = "\n".join(f"- {t}" for t in open_titles[:300]) or "- none"
    return f"""\
You are the planner in vigia, an AI software factory. Turn the goal below into at most {limit} tickets that AI
builders will implement one by one, each reviewed by a model from another lab and merged by a script. The
current directory is a read-only checkout of `{base}` of {repo}: read the code before you write anything about it.

## How to write a ticket
{skill.strip()}

## Rules
- One concern per ticket, small enough for one pull request. Order them so each builds on merged work.
- Set `depends_on_earlier` to true when a ticket needs an earlier ticket of this plan merged first. Tickets without
  it may be built at the same time, from the same base, so it waits for a person to release it.
- Say what changes and what must keep working; leave how to the builder unless you verified it in the code.
- Every claim about the code (a file, a function, current behaviour) goes in `## Facts`, marked verified (what you
  read, with file:line) or hypothesis.
- Tier: T0 cosmetic, docs, tests, internal tooling; T1 product logic with no money and no other people's data;
  T2 money, auth, personal data, migrations, messages to customers, credentials, the process itself. When two
  fit, the higher wins.
- Do not repeat work an open issue already covers (listed below).
- Work no builder can do (creating an account, a secret, a purchase, a product decision) is a human task, not a
  ticket: give it a title and plain steps.
- Write in English.

## Output
Reply with only one JSON object: {{"tickets": [{{"title": "<conventional-commit style, under 70 chars>",
"tier": "T0" | "T1" | "T2", "depends_on_earlier": true | false, "body": "<the full ticket in the template>"}}], "human_tasks": [{{"title": "...",
"body": "..."}}]}}

## Open issues
{already}

## Files in the repository
{listing}

## The goal
{goal.strip()}
"""


def parse(text: str) -> dict | None:
    """The planner's JSON, or None when it returned none. Malformed entries are dropped, never guessed. A ticket
    after the first that does not say `depends_on_earlier: false` is taken to depend on the ones before it, counted
    in the planner's own order: a dropped earlier ticket is still a prerequisite that was never filed."""
    body = (text or "").strip()
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(body[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    out = {"tickets": [], "human_tasks": []}
    dropped = 0  # malformed tickets so far: a later dependent one must say its prerequisite was never filed
    for position, t in enumerate(data.get("tickets") or []):
        if (
            isinstance(t, dict)
            and t.get("tier") in TIERS
            and str(t.get("title") or "").strip()
            and str(t.get("body") or "").strip()
        ):
            body = strip_front_matter(str(t["body"]).strip())
            after = position > 0 and t.get("depends_on_earlier") is not False
            out["tickets"].append(
                {
                    "title": str(t["title"]).strip()[:120],
                    "tier": t["tier"],
                    "after": after,
                    "dropped_before": dropped,
                    "body": body,
                }
            )
        else:
            dropped += 1
    for h in data.get("human_tasks") or []:
        if isinstance(h, dict) and str(h.get("title") or "").strip():
            out["human_tasks"].append(
                {"title": str(h["title"]).strip()[:120], "body": str(h.get("body") or "").strip()}
            )
    return out


def file_plan(ctx: Ctx, plan: dict, goal: str) -> list[dict]:
    """Create the issues, all proposed. Returns [{'number', 'title', 'labels'}] for what was filed. A ticket or
    human task whose title is already an open issue's, read fresh here (the planner's session may have run for
    minutes), or repeats within the plan, is not filed again (ledger `plan_skipped`)."""
    labels = ctx.cfg.labels
    taken = {i["title"].strip().lower() for i in ctx.gh.open_issues()}
    planned: set[str] = set()
    open_before: list[
        str
    ] = []  # earlier tickets left to an open issue: a later dependent one waits for those too
    kept = []
    for t in plan["tickets"]:
        key = t["title"].strip().lower()
        if key in planned:
            ctx.ledger.append(
                "plan_skipped", title=t["title"][:120], reason="the plan already has this title"
            )
        elif key in taken:
            ctx.ledger.append("plan_skipped", title=t["title"][:120], reason="an open issue has this title")
            open_before.append(t["title"])
        else:
            kept.append({**t, "open_before": list(open_before)})
            planned.add(key)
    plan = {**plan, "tickets": kept}
    if labels.collisions():  # vigia.toml refuses these; a Config built another way must not either
        raise SystemExit(
            f"labels {labels.collisions()} are reserved for the planner and the tiers: nothing filed"
        )
    checked = []
    for t in plan["tickets"]:
        gaps = tickets.missing(t["body"])
        checked.append((t, gaps))
    # A repo set up by an older `vigia init` has no planner labels yet: create them first, so a plan is never
    # left half filed by a label GitHub refuses.
    needed = ({PROPOSED} if checked else set()) | ({HUMAN_TASK} if plan["human_tasks"] else set())
    for name, color, description in label_specs(labels):
        if name in needed:
            ctx.gh.ensure_label(name, color, description)
    filed = []
    for t, gaps in checked:
        names = [labels.tier(t["tier"]), PROPOSED]
        body = t["body"] + f"\n\n<!-- vigia:planned goal={json.dumps(goal[:200])} -->\n"
        if gaps:
            body += "\n> Planned by vigia, but the template check found gaps: " + "; ".join(gaps) + "\n"
        if t.get("after"):
            earlier = [f"#{f['number']}" for f in filed] + [
                f"the open issue titled {json.dumps(title)}" for title in t.get("open_before") or []
            ]
            if t.get("dropped_before"):
                follows = ", ".join(
                    earlier + ["a ticket the planner returned malformed, so it was not filed"]
                )
                body += f"\n> Planned to follow {follows}: check the plan before adding `{labels.ready}`.\n"
            else:
                body += f"\n> Planned to follow {', '.join(earlier)}: add `{labels.ready}` once they are merged.\n"
        number = ctx.gh.create_issue(t["title"], body, names)
        ctx.ledger.append("planned", issue=number, tier=t["tier"], gaps=gaps, after=bool(t.get("after")))
        filed.append({"number": number, "title": t["title"], "labels": names})
    for h in plan["human_tasks"]:
        key = h["title"].strip().lower()
        if key in taken or key in planned:
            reason = "an open issue has this title" if key in taken else "the plan already has this title"
            ctx.ledger.append("plan_skipped", title=h["title"][:120], reason=reason)
            continue
        planned.add(key)
        number = ctx.gh.create_issue(
            h["title"], h["body"] + "\n\nOnly a person can do this; vigia will not build it.\n", [HUMAN_TASK]
        )
        ctx.ledger.append("planned", issue=number, human_task=True)
        filed.append({"number": number, "title": h["title"], "labels": [HUMAN_TASK]})
    return filed


def plan(
    ctx: Ctx,
    goal: str,
    limit: int = 5,
    seat: Seat = DEFAULT_SEAT,
    dry_run: bool = False,
):
    """Run one planner session on the base branch and file what it proposes. Returns (plan, filed).

    The clone's lock is held only while this reads and while it files, never across the session: `vigia watch`
    keeps ticking meanwhile. The planner lock is held throughout, so two planners on one clone never collide and
    the tick's recovery leaves the planner's worktree alone."""
    cfg = ctx.cfg
    wait = (
        cfg.caps.session_minutes * 60
    )  # a tick may be running a whole session; the plan is worth waiting for
    with jobs.hold(ctx, jobs.PLANNER) as got:
        if not got:
            raise SystemExit("another vigia plan is running in this clone; try again when it ends")
        with ctx.lock(wait):
            base_sha = ctx.git.remote_branch_sha(cfg.base)
            open_titles = [i["title"] for i in ctx.gh.open_issues()]
            files = ctx.git.files(base_sha)
            path = ctx.root / f"plan-{base_sha[:8]}"
            if path.exists():  # a killed planner's: we hold the planner lock, so nothing else uses it
                ctx.git.remove_worktree(path)
            wt = ctx.git.worktree_detached(path, base_sha)
        try:
            text = prompt(goal, cfg.repo, cfg.base, _skill(wt), open_titles, files, limit)
            # the planner only reads: it runs with a reviewer's read-only sandbox, and answers in its own schema
            outcome = ctx.engines.run(
                seat, "reviewer", text, wt, name=f"plan-{base_sha[:8]}", schema=PLAN_SCHEMA
            )
        finally:
            ctx.git.remove_worktree(wt)
        # a session that failed or hit its time cap is not trusted, even when it left parseable JSON behind
        result = parse(outcome.text) if outcome.ok else None
        if result is None:
            ctx.ledger.append("plan_failed", goal=goal[:200], code=outcome.code, timed_out=outcome.timed_out)
            raise SystemExit(f"the planner returned no plan (exit {outcome.code}; log {outcome.log})")
        result["tickets"] = result["tickets"][:limit]
        if dry_run:
            return result, []
        with ctx.lock(wait):
            return result, file_plan(ctx, result, goal)
