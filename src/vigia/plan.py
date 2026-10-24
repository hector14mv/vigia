"""`vigia plan "<goal>"`: a planner session turns a goal into tickets in the template, and fills the queue.

The planner reads the base branch (read-only, like a reviewer) and answers with JSON. vigia files each ticket as
an issue: a launchable T0 or T1 ticket goes straight to the queue (`vigia:ready`); a T2 ticket, or one the
template check refuses, waits as `vigia:proposed` for a person to ratify, because what touches money, auth or
data is a person's call to start. So does a ticket that needs an earlier one of the plan merged first: the queue
starts ready tickets side by side, in any order, so it waits until a person releases it. Work only a person can do (an account, a secret, a decision) becomes a
`vigia:human-task` issue, which the Mesa shows under "Your turn".
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import tickets
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
    already = "\n".join(f"- {t}" for t in open_titles[:80]) or "- none"
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
    after the first that does not say `depends_on_earlier: false` is taken to depend on the ones before it."""
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
    for t in data.get("tickets") or []:
        if (
            isinstance(t, dict)
            and t.get("tier") in TIERS
            and str(t.get("title") or "").strip()
            and str(t.get("body") or "").strip()
        ):
            body = strip_front_matter(str(t["body"]).strip())
            after = bool(out["tickets"]) and t.get("depends_on_earlier") is not False
            out["tickets"].append(
                {"title": str(t["title"]).strip()[:120], "tier": t["tier"], "after": after, "body": body}
            )
    for h in data.get("human_tasks") or []:
        if isinstance(h, dict) and str(h.get("title") or "").strip():
            out["human_tasks"].append(
                {"title": str(h["title"]).strip()[:120], "body": str(h.get("body") or "").strip()}
            )
    return out


def file_plan(ctx: Ctx, plan: dict, goal: str, auto_ready: bool = True) -> list[dict]:
    """Create the issues. Returns [{'number', 'title', 'labels'}] for what was filed."""
    labels = ctx.cfg.labels
    if labels.collisions():  # vigia.toml refuses these; a Config built another way must not either
        raise SystemExit(
            f"labels {labels.collisions()} are reserved for the planner and the tiers: nothing filed"
        )
    checked = []
    for t in plan["tickets"]:
        gaps = tickets.missing(t["body"])
        checked.append((t, gaps, auto_ready and not gaps and t["tier"] != "T2" and not t.get("after")))
    # A repo set up by an older `vigia init` has no planner labels yet: create them first, so a plan is never
    # left half filed by a label GitHub refuses.
    needed = {PROPOSED for _, _, ready in checked if not ready} | (
        {HUMAN_TASK} if plan["human_tasks"] else set()
    )
    for name, color, description in label_specs(labels):
        if name in needed:
            ctx.gh.ensure_label(name, color, description)
    filed = []
    for t, gaps, ready in checked:
        names = [labels.tier(t["tier"]), labels.ready if ready else PROPOSED]
        body = t["body"] + f"\n\n<!-- vigia:planned goal={json.dumps(goal[:200])} -->\n"
        if gaps:
            body += "\n> Planned by vigia, but the template check found gaps: " + "; ".join(gaps) + "\n"
        if t.get("after") and filed:
            earlier = ", ".join(f"#{f['number']}" for f in filed)
            body += f"\n> Planned to follow {earlier}: add `{labels.ready}` once they are merged.\n"
        number = ctx.gh.create_issue(t["title"], body, names)
        ctx.ledger.append(
            "planned", issue=number, tier=t["tier"], ready=ready, gaps=gaps, after=bool(t.get("after"))
        )
        filed.append({"number": number, "title": t["title"], "labels": names})
    for h in plan["human_tasks"]:
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
    auto_ready: bool = True,
):
    """Run one planner session on the base branch and file what it proposes. Returns (plan, filed)."""
    cfg = ctx.cfg
    base_sha = ctx.git.remote_branch_sha(cfg.base)
    open_titles = [i["title"] for i in ctx.gh.issues_all()]
    files = ctx.git.files(base_sha)
    wt = ctx.git.worktree_detached(ctx.root / f"plan-{base_sha[:8]}", base_sha)
    try:
        text = prompt(goal, cfg.repo, cfg.base, _skill(wt), open_titles, files, limit)
        # the planner only reads: it runs with a reviewer's read-only sandbox, and answers in its own schema
        outcome = ctx.engines.run(seat, "reviewer", text, wt, name=f"plan-{base_sha[:8]}", schema=PLAN_SCHEMA)
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
    return result, file_plan(ctx, result, goal, auto_ready)
