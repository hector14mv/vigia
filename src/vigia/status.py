"""`vigia status`: the queue, the open PRs with their next step, what is parked and why, recent events."""

from __future__ import annotations

import json

from . import tickets
from .context import Ctx
from .watch import sense


def collect(ctx: Ctx) -> dict:
    cfg, labels = ctx.cfg, ctx.cfg.labels
    ready = ctx.gh.issues(labels.ready)
    building = ctx.gh.issues(labels.building)
    parked_issues = ctx.gh.issues(labels.parked)
    prs = []
    for s in sense(ctx, track=False):
        parked = ctx.ledger.last("parked", pr=s.pr["number"]) if s.state.parked else None
        prs.append(
            {
                "number": s.pr["number"],
                "title": s.pr["title"],
                "url": s.pr["url"],
                "tier": s.state.tier,
                "head": s.pr["head"],
                "ci": s.state.ci,
                "next": s.decision.action,
                "reason": parked["reason"] if parked else s.decision.reason,
                "parked": s.state.parked,
            }
        )
    return {
        "repo": cfg.repo,
        "base": cfg.base,
        "queue": [
            {
                "number": i["number"],
                "title": i["title"],
                "tier": tickets.tier(i["labels"], labels.tier_prefix),
            }
            for i in ready
            if labels.building not in i["labels"]
        ],
        "building": [{"number": i["number"], "title": i["title"]} for i in building],
        "parked_issues": [{"number": i["number"], "title": i["title"]} for i in parked_issues],
        "prs": prs,
        "recent": ctx.ledger.rows()[-12:],
        "merged": len(ctx.ledger.events("merged")),
    }


def _event(row: dict) -> str:
    what = row["event"]
    if what == "verdict":
        what = f"{row['kind']} {row['verdict']} ({row.get('p1', 0)} P1) by {row.get('engine')} · {row.get('model')}"
    elif what in ("parked",):
        what = f"parked — {row.get('reason')}"
    elif what == "pr_opened":
        what = f"opened from #{row.get('issue')} by {row.get('engine')} · {row.get('model')}"
    elif what == "merged":
        what = f"merged {str(row.get('sha'))[:8]}"
    target = f"PR #{row['pr']}" if row.get("pr") else (f"#{row['issue']}" if row.get("issue") else "")
    return f"  {row['ts']}  {target:<8} {what}"


def render(data: dict) -> str:
    out = [f"vigia · {data['repo']} (base {data['base']}) · {data['merged']} merged by vigia", ""]
    out.append(f"Queue ({len(data['queue'])} ready)")
    out += [f"  #{i['number']:<5} {i['tier'] or '??'}  {i['title']}" for i in data["queue"]] or ["  (empty)"]
    if data["building"]:
        out += ["", "Building"] + [f"  #{i['number']:<5} {i['title']}" for i in data["building"]]
    out += ["", f"Pull requests ({len(data['prs'])} open)"]
    for p in data["prs"]:
        flag = "PARKED " if p["parked"] else ""
        out.append(
            f"  #{p['number']:<5} {p['tier'] or '??'}  {p['head'][:8]}  ci {p['ci']:<8} {flag}{p['next']}: {p['reason']}"
        )
        out.append(f"         {p['title']}")
    if not data["prs"]:
        out.append("  (none)")
    if data["parked_issues"]:
        out += ["", "Parked issues"] + [f"  #{i['number']:<5} {i['title']}" for i in data["parked_issues"]]
    out += ["", "Recent ledger"] + ([_event(r) for r in data["recent"]] or ["  (empty)"])
    return "\n".join(out)


def show(ctx: Ctx, as_json: bool = False) -> str:
    data = collect(ctx)
    return json.dumps(data, indent=2, default=str) if as_json else render(data)
