"""`vigia status`: the queue, the open PRs with their next step, what is parked and why, recent events."""

from __future__ import annotations

import json
from html import escape
from pathlib import Path

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


def _what(row: dict) -> str:
    what = row["event"]
    if what == "verdict":
        what = f"{row['kind']} {row['verdict']} ({row.get('p1', 0)} P1) by {row.get('engine')} · {row.get('model')}"
    elif what in ("parked",):
        what = f"parked — {row.get('reason')}"
    elif what == "pr_opened":
        what = f"opened from #{row.get('issue')} by {row.get('engine')} · {row.get('model')}"
    elif what == "merged":
        what = f"merged {str(row.get('sha'))[:8]}"
    return what


def _target(row: dict) -> str:
    return f"PR #{row['pr']}" if row.get("pr") else (f"#{row['issue']}" if row.get("issue") else "")


def _event(row: dict) -> str:
    return f"  {row['ts']}  {_target(row):<8} {_what(row)}"


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


_CSS = """
:root{--bg:#fafaf9;--fg:#1c1917;--muted:#78716c;--card:#fff;--line:#e7e5e4;--ok:#15803d;--bad:#b91c1c;--warn:#b45309;--link:#1d4ed8}
@media (prefers-color-scheme:dark){:root{--bg:#161412;--fg:#f5f5f4;--muted:#a8a29e;--card:#1f1c1a;--line:#3a3532;--ok:#4ade80;--bad:#f87171;--warn:#fbbf24;--link:#93c5fd}}
*{box-sizing:border-box}
body{margin:0;padding:24px 16px;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,sans-serif}
main{max-width:880px;margin:0 auto}
h1{font-size:1.4rem;margin:0 0 4px}
h2{font-size:1rem;margin:32px 0 8px}
.muted{color:var(--muted)}
a{color:var(--link)}
ul{list-style:none;margin:0;padding:0}
li,.pr{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin:0 0 8px;overflow-wrap:anywhere}
.pr .head{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:baseline}
.pr p{margin:4px 0 0}
code,.tier,.ci{font:13px ui-monospace,SFMono-Regular,Menlo,monospace}
.tier,.ci{border:1px solid var(--line);border-radius:6px;padding:0 6px}
.ci-passing{color:var(--ok)}.ci-failing{color:var(--bad)}.ci-pending{color:var(--warn)}
.parked{color:var(--warn);font-weight:600}
.pr.is-parked{border-color:var(--warn)}
"""


def _link(text: object, url: object) -> str:
    """Escaped text, linked only when the URL is plain http(s): never a `javascript:` href."""
    label = escape(str(text))
    href = str(url or "")
    if href.startswith(("https://", "http://")):
        return f'<a href="{escape(href)}">{label}</a>'
    return label


def _items(rows: list[dict], empty: str, tier: bool = False) -> str:
    if not rows:
        return f'<p class="muted">{empty}</p>'
    lis = []
    for i in rows:
        badge = f' <span class="tier">{escape(str(i.get("tier") or "??"))}</span>' if tier else ""
        lis.append(f"<li>#{escape(str(i['number']))}{badge} {escape(str(i['title']))}</li>")
    return "<ul>" + "".join(lis) + "</ul>"


def _pr_html(p: dict) -> str:
    ci = str(p["ci"])
    parked = ' <span class="parked">PARKED</span>' if p["parked"] else ""
    return (
        f'<div class="pr{" is-parked" if p["parked"] else ""}">'
        f'<div class="head"><strong>{_link("#" + str(p["number"]), p["url"])}</strong>'
        f' <span class="tier">{escape(str(p["tier"] or "??"))}</span>'
        f" <code>{escape(str(p['head'])[:8])}</code>"
        f' <span class="ci ci-{escape(ci, quote=True)}">ci {escape(ci)}</span>{parked}</div>'
        f"<p>{escape(str(p['title']))}</p>"
        f"<p><strong>{escape(str(p['next']))}</strong>: "
        f'<span class="muted">{escape(str(p["reason"]))}</span></p>'
        "</div>"
    )


def render_html(data: dict) -> str:
    """One self-contained page (inline CSS, no script, no external requests) with what `render` shows."""
    repo, base = escape(str(data["repo"])), escape(str(data["base"]))
    sections = [
        f"<h2>Queue ({len(data['queue'])} ready)</h2>" + _items(data["queue"], "(empty)", tier=True),
    ]
    if data["building"]:
        sections.append("<h2>Building</h2>" + _items(data["building"], "(none)"))
    sections.append(f"<h2>Pull requests ({len(data['prs'])} open)</h2>")
    sections.append("".join(_pr_html(p) for p in data["prs"]) or '<p class="muted">(none)</p>')
    if data["parked_issues"]:
        sections.append("<h2>Parked issues</h2>" + _items(data["parked_issues"], "(none)"))
    events = "".join(
        f"<li><code>{escape(str(r['ts']))}</code> {escape(_target(r))} {escape(str(_what(r)))}</li>"
        for r in data["recent"]
    )
    sections.append(
        "<h2>Recent ledger</h2>" + (f"<ul>{events}</ul>" if events else '<p class="muted">(empty)</p>')
    )
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>vigia · {repo}</title><style>{_CSS}</style></head><body><main>"
        f"<h1>vigia · {repo}</h1>"
        f'<p class="muted">base {base} · {escape(str(data["merged"]))} merged by vigia</p>'
        + "".join(sections)
        + "</main></body></html>\n"
    )


def write_html(ctx: Ctx, path: Path) -> Path:
    path = Path(path).expanduser()
    path.write_text(render_html(collect(ctx)), encoding="utf-8")
    return path.resolve()
