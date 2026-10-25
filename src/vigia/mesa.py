"""`vigia mesa`: the factory's control desk in a browser, on a laptop or a phone.

It answers what a person would otherwise ask in a chat: what needs me, how is each PR doing and why, what just
happened, how much quota is left, is the watcher alive. It is read-only on purpose. Every action is a link to
GitHub, where your own login decides: a button here that labels or approves would let any process on this
machine, an engine session included, act as you.

The server is the standard library's, bound to 127.0.0.1. To reach it from a phone, put it behind
`tailscale serve` (docs/server.md) rather than binding it to a public interface.
"""

from __future__ import annotations

import json
import re
import statistics
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import decisions, fuel, jobs, tickets
from .config import PROPOSED
from .context import Ctx
from .proc import CommandError
from .rules import decide
from .watch import Seen, sense
from .watch import state as pr_state

PAGE = Path(__file__).with_name("mesa.html")
DEFAULT_PORT = 8790
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


def _ts(value: str | None) -> datetime | None:
    try:
        return datetime.strptime(value or "", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_day(moment: datetime) -> str:
    """The calendar day on this machine's clock: "today" means the person's day, not UTC's."""
    return moment.astimezone().strftime("%Y-%m-%d")


def _tried_total(rows: list[dict]) -> dict:
    """What was spent on a PR over its whole life, every head and every park included."""
    heads = {r.get("sha") for r in rows if r["event"] in ("pr_opened", "head_seen", "pushed", "updated")}
    return {
        "gate_rounds": sum(
            1
            for r in rows
            if r["event"] == "verdict" and r.get("kind") == "gate" and r.get("verdict") != "UNAVAILABLE"
        ),
        "fix_rounds": sum(1 for r in rows if r["event"] == "fix_started"),
        "heads": len(heads - {None}),
        "p1": sum(r.get("p1", 0) for r in rows if r["event"] == "verdict"),
        "parks": sum(1 for r in rows if r["event"] == "parked"),
        "unparks": sum(1 for r in rows if r["event"] == "unparked"),
    }


def _seat(ctx: Ctx, action: str, tier: str | None) -> dict:
    """The engine and model a job of `action` runs with, from the tier's seats."""
    policy = ctx.cfg.tiers.get(tier) if tier else None
    if policy is None:
        return {"engine": None, "model": None}
    seat = {"gate": policy.gate, "review": policy.reviewer}.get(action, policy.builder)
    return {"engine": seat.engine, "model": seat.model} if seat else {"engine": None, "model": None}


def _decisions(ctx: Ctx, rows: list[dict]) -> list[dict] | None:
    """The open decision issues, each parsed or marked malformed. None when they cannot be read: unknown, not none."""
    try:
        issues = ctx.gh.issues(decisions.LABEL)
    except CommandError:
        return None
    out = []
    for i in issues:
        parsed = decisions.parse(i["body"])
        answered = next(
            (r for r in reversed(rows) if r["event"] == "decided" and r.get("issue") == i["number"]), None
        )
        item = {
            "number": i["number"],
            "title": i["title"],
            "url": i["url"],
            "since": i.get("created") or None,
        }
        if parsed is None:  # shown so a person can fix it, never guessed at
            item.update(malformed=True, question=i["title"], options=[], rec=None, blocks=[])
        else:
            item.update(parsed, malformed=False)
        item["answered"] = {"key": answered.get("key"), "by": answered.get("by")} if answered else None
        out.append(item)
    return out


def _verdict_line(row: dict, what: str) -> str:
    return (
        f"{what} {row['verdict']} · {row.get('engine')} · {row.get('model')} (lab {row.get('family')}) · "
        f"{row.get('p1', 0)} P1"
    )


def _pipeline(ctx: Ctx, seen, running: str | None) -> tuple[dict, str | None]:
    """Each step of the PR's path: done | now | wait | stop | skip | todo, and what it waits for, if anyone."""
    st, d = seen.state, seen.decision
    policy = ctx.cfg.tiers.get(st.tier) if st.tier else None
    on_head = [v for v in st.verdicts if v.sha == st.head and v.content]
    gate = next((v for v in reversed(on_head) if v.kind == "gate"), None)
    review = next((v for v in reversed(on_head) if v.kind == "review"), None)
    steps = {
        "build": "done",
        "ci": {"passing": "done", "pending": "now", "failing": "stop"}.get(st.ci, "todo"),
    }
    steps["gate"] = "done" if gate and gate.passes else "stop" if gate else "todo"
    if policy is None or policy.reviewer is None:
        steps["review"] = "skip"
    else:
        steps["review"] = "done" if review and review.passes else "stop" if review else "todo"
    waiting = None
    if policy is None or not policy.human:
        steps["human"] = "skip"
    elif st.human_ok:
        steps["human"] = "done"
    elif d.action == "wait" and "waiting for a human" in d.reason:
        steps["human"], waiting = "wait", "human"
    else:
        steps["human"] = "todo"
    steps["merge"] = "now" if d.action == "merge" else "todo"
    for action in (running, d.action):  # what is happening right now
        if action in ("gate", "review"):
            steps[action] = "now"
            break
        if action == "fix":
            stage = d.stage if d.stage in ("ci", "gate", "review") else "gate"
            steps[stage] = "now"
            break
    if st.parked:
        first = next((k for k, v in steps.items() if v not in ("done", "skip")), "merge")
        steps[first] = "stop"
    return steps, waiting


def _running(ctx: Ctx, rows: list[dict]) -> list[dict]:
    """The engine sessions holding a lock on this machine: read locally, so known even when GitHub is not.

    A build's seat, tier and branch come from its `build_started` row, so they are known locally too; a PR job's
    seat depends on the PR's tier, which `repo_state` fills in once GitHub has been read."""
    started = {r.get("target"): r for r in rows if r["event"] in ("job_spawned", "job_started")}
    builds = {r.get("issue"): r for r in rows if r["event"] == "build_started"}  # issue -> its latest build
    running = []
    for name in sorted(set(jobs.live(ctx))):
        kind, _, number = name.partition("-")
        row = started.get(name) or {}
        job = {
            "target": name,
            "number": int(number) if number.isdigit() else None,
            "issue": kind == "issue",
            "action": row.get("action") or "build",
            "since": row.get("ts"),
            "engine": None,
            "model": None,
            "tier": None,
            "branch": None,
        }
        build = builds.get(job["number"]) if job["issue"] else None
        if build and build["ts"] >= (job["since"] or ""):  # this build's row, not an earlier one's
            job.update({k: build.get(k) for k in ("engine", "model", "tier", "branch")})
        running.append(job)
    return running


def _refused(ctx: Ctx, rows: list[dict], unread: list[str]) -> list[dict] | None:
    """Refused tickets still waiting for a person: issues whose latest refusal came after their latest build start
    (a later refusal brings one back), and that are still open and not ready. Whatever their age, and every one of
    them: the open issues are read in one paginated call, so none is left unchecked. None when they cannot be read.
    """
    pending: dict[int, dict] = {}  # issue -> its latest refusal
    for r in rows:
        if r["event"] == "refused":
            pending[r["issue"]] = r
        elif r["event"] == "build_started":
            pending.pop(r.get("issue"), None)
    if not pending:
        return []
    try:
        still_open = {i["number"]: i for i in ctx.gh.open_issues()}
    except CommandError:
        unread.append("refused tickets")
        return None
    refused = []
    for n, r in sorted(pending.items(), key=lambda item: item[1]["ts"], reverse=True):
        issue = still_open.get(n)
        if issue and ctx.cfg.labels.ready not in issue["labels"]:
            refused.append(
                {
                    "number": issue["number"],
                    "title": issue["title"],
                    "url": issue["url"],
                    "missing": r.get("gaps") or [],
                    "since": r["ts"],  # it has waited for a person since this refusal
                }
            )
    return refused


def _labels(ctx: Ctx) -> dict:
    """The labels the page tells a person to add or remove: the repo's own, which vigia.toml may rename."""
    labels = ctx.cfg.labels
    return {"ready": labels.ready, "parked": labels.parked, "human_ok": labels.human_ok}


def _merged_today(rows: list[dict], now: datetime) -> list[dict]:
    """What merged on this machine's calendar day, newest first: from the ledger, so known even when GitHub is not."""
    today = _local_day(now)
    merged = [
        {"pr": r["pr"], "title": r.get("title"), "tier": r.get("tier"), "sha": r.get("sha"), "ts": r["ts"]}
        for r in rows
        if r["event"] == "merged" and (_ts(r["ts"]) and _local_day(_ts(r["ts"])) == today)
    ]
    return merged[::-1]


def repo_state(ctx: Ctx, now: datetime) -> dict:
    cfg, labels = ctx.cfg, ctx.cfg.labels
    rows = ctx.ledger.rows()
    by_pr: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("pr") is not None:
            by_pr[r["pr"]].append(r)
    running = _running(ctx, rows)
    # What could not be read from GitHub: those lists are incomplete, and the page must say so rather than show
    # them as empty.
    unread: list[str] = []
    open_decisions = _decisions(ctx, rows)
    if open_decisions is None:
        unread.append("decisions")
    prs = []
    for s in sense(ctx, track=False):
        n, head = s.pr["number"], s.pr["head"]
        mine = by_pr.get(n, [])
        job = next((j for j in running if j["number"] == n and not j["issue"]), None)
        if job:
            job.update(_seat(ctx, job["action"], s.state.tier), tier=s.state.tier)
        steps, waiting = _pipeline(ctx, s, job["action"] if job else None)
        opened = next((r for r in reversed(mine) if r["event"] == "pr_opened"), {})
        on_head = [r for r in mine if r["event"] == "verdict" and r.get("sha") == head]
        evidence = []
        if s.state.ci == "passing":
            evidence.append("CI green on this head")
        for kind, what in (("gate", "Gate"), ("review", f"{s.state.tier} review")):
            v = next((r for r in reversed(on_head) if r.get("kind") == kind), None)
            if v and v.get("verdict") != "UNAVAILABLE":
                evidence.append(_verdict_line(v, what))
        earlier = [r for r in mine if r["event"] == "verdict" and r.get("sha") != head]
        if earlier:
            p1 = sum(r.get("p1", 0) for r in earlier)
            evidence.append(
                f"{len(earlier)} earlier review(s) found {p1} P1"
                + ("s" if p1 != 1 else "")
                + ", all on older heads"
            )
        parked = next((r for r in reversed(mine) if r["event"] == "parked"), None) if s.state.parked else None
        since = None
        if parked:
            since = parked["ts"]
        elif waiting == "human" and on_head:
            since = on_head[-1]["ts"]  # it has waited for a person since the verdict that made it wait
        issue = opened.get("issue") or tickets.linked_issue(s.pr["body"])
        prs.append(
            {
                "number": n,
                "title": s.pr["title"],
                "url": s.pr["url"],
                "tier": s.state.tier or "?",
                "head": head,
                "ci": s.state.ci,
                "diff": s.pr.get("diff"),
                "next": s.decision.action,
                "reason": parked["reason"] if parked else s.decision.reason,
                "parked": s.state.parked,
                "waiting_for": waiting,
                "since": since,
                "pipeline": steps,
                "evidence": evidence,
                "built_by": f"{opened['engine']} · {opened['model']}" if opened.get("engine") else None,
                "tried": f"{s.state.fix_rounds} fix round(s) and "
                f"{len({v.sha for v in s.state.verdicts if v.kind == 'gate' and v.content})} gate round(s) since it was last unparked",
                "tried_total": _tried_total(mine),
                "issue_url": f"https://github.com/{cfg.repo}/issues/{issue}" if issue else None,
            }
        )
    queue = [
        {
            "number": i["number"],
            "title": i["title"],
            "url": i["url"],
            "tier": tickets.tier(i["labels"], labels.tier_prefix) or "?",
        }
        for i in ctx.gh.issues(labels.ready)
        if labels.building not in i["labels"]
    ]
    # A build that failed before it opened a PR parks the issue itself (builder._fail_issue): no PR shows it,
    # and only a person can resume it.
    parked_issues = []
    for i in ctx.gh.issues(labels.parked):
        failed = ctx.ledger.last("build_failed", issue=i["number"]) or {}
        parked_issues.append(
            {
                "number": i["number"],
                "title": i["title"],
                "url": i["url"],
                "reason": failed.get("reason") or "parked; the ticket's last comment says why",
                "since": failed.get("ts"),
            }
        )
    refused = _refused(ctx, rows, unread)
    try:
        human_tasks = [
            {
                "number": i["number"],
                "title": i["title"],
                "url": i["url"],
                "since": i.get("created") or None,
                "time": decisions.field(i["body"], "Time"),
                "due": decisions.field(i["body"], "Due"),
                "origin": decisions.field(i["body"], "Origin"),
            }
            for i in ctx.gh.issues("vigia:human-task")
        ]
    except CommandError:
        human_tasks = None  # unknown, not none
        unread.append("human tasks")
    try:
        proposed = [
            {
                "number": i["number"],
                "title": i["title"],
                "url": i["url"],
                "tier": tickets.tier(i["labels"], labels.tier_prefix) or "?",
                "since": i.get("created") or None,
            }
            for i in ctx.gh.open_issues()  # every page: `issues()` stops at 100
            if PROPOSED in i["labels"]
        ]
    except CommandError:
        proposed = None  # unknown, not none
        unread.append("proposed tickets")
    return {
        "repo": cfg.repo,
        "slots": cfg.slots,
        "session_minutes": cfg.caps.session_minutes,
        "jobs": running,
        "prs": prs,
        "queue": queue,
        "parked_issues": parked_issues,
        "refused": refused,
        "human_tasks": human_tasks,
        "proposed": proposed,
        "decisions": open_decisions,
        "merged_today": _merged_today(rows, now),
        "unread": unread,
        "labels": _labels(ctx),
    }


def _local(ctx: Ctx, rows: list[dict], now: datetime) -> dict:
    """What this machine knows about a repo on its own: the running sessions, the labels and today's merges from
    the ledger. Every list read from GitHub is unknown (null), never empty, so the page cannot say there is nothing
    to do."""
    return {
        "repo": ctx.cfg.repo,
        "slots": ctx.cfg.slots,
        "session_minutes": ctx.cfg.caps.session_minutes,
        "jobs": _running(ctx, rows),
        "prs": None,
        "queue": None,
        "parked_issues": None,
        "refused": None,
        "human_tasks": None,
        "proposed": None,
        "decisions": None,
        "merged_today": _merged_today(rows, now),
        "labels": _labels(ctx),
    }


def _unreadable(ctx: Ctx, rows: list[dict], now: datetime, exc: CommandError) -> dict:
    """A repo GitHub did not answer for: only what this machine knows, and why the rest is unknown."""
    return {**_local(ctx, rows, now), "error": str(exc)[:300]}


FEED_EVENTS = {
    "merged",
    "parked",
    "unparked",
    "verdict",
    "pr_opened",
    "pushed",
    "updated",
    "engine_failed",
    "build_started",
    "build_failed",
    "refused",
    "decided",
    "captured",
    "error",
}
_FEED_FIELDS = (
    "pr",
    "issue",
    "kind",
    "verdict",
    "round",
    "p1",
    "tier",
    "engine",
    "model",
    "stage",
    "title",
    "key",
)


def _feed_text(short: str, r: dict) -> str:
    e, sha = r["event"], str(r.get("sha") or "")[:8]
    who = f"{r.get('engine')} · {r.get('model')}"
    return {
        "merged": lambda: f"{short} #{r.get('pr')} merged at {sha} · tier {r.get('tier')}",
        "parked": lambda: f"{short} #{r.get('pr')} — {r.get('reason')}",
        "unparked": lambda: f"{short} #{r.get('pr')} unparked by a person at {sha}",
        "verdict": lambda: f"{short} #{r.get('pr')} {r.get('kind')} by {who} on {sha}",
        "pr_opened": lambda: f"{short} #{r.get('pr')} opened from #{r.get('issue')} by {who}",
        "pushed": lambda: f"{short} #{r.get('pr')} fix pushed {sha} ({r.get('stage')})",
        "updated": lambda: f"{short} #{r.get('pr')} brought the moved base in, new head {sha}",
        "engine_failed": lambda: f"{short} #{r.get('pr')} session failed — {r.get('reason')}",
        "build_started": lambda: f"{short} #{r.get('issue')} build started with {who}",
        "build_failed": lambda: f"{short} #{r.get('issue')} build failed — {r.get('reason')}",
        "refused": lambda: f"{short} #{r.get('issue')} refused — {'; '.join(r.get('gaps') or [])}",
        "decided": lambda: f"{short} #{r.get('issue')} decided {r.get('key')} by {r.get('by')}",
        "captured": lambda: f"{short} #{r.get('pr')} screenshots captured at {sha}",
        "error": lambda: f"{short} {r.get('action') or 'error'} — {str(r.get('detail') or '')[:160]}",
    }[e]()


def _feed_line(repo: str, r: dict) -> dict | None:
    """One feed entry with its fields, so the page builds its own glyph, title and meta; `text` is a ready line."""
    if r["event"] not in FEED_EVENTS:
        return None
    short = repo.split("/")[-1]
    line = {"ts": r["ts"], "repo": short, "event": r["event"]}
    line.update({k: r[k] for k in _FEED_FIELDS if r.get(k) is not None})
    if r.get("sha"):
        line["sha"] = str(r["sha"])[:7]
    reason = r.get("reason") or (str(r.get("detail"))[:200] if r.get("detail") else None)
    if reason:
        line["reason"] = reason
    line["text"] = _feed_text(short, r)
    return line


def _sessions(rows: list[dict]) -> list[dict]:
    """The rows that each mark one engine session: a build, a fix, or a review of either kind."""
    return [r for r in rows if r["event"] in ("build_started", "fix_started", "verdict")]


def state(
    contexts: list[Ctx], now: datetime | None = None, slots: int | None = None, github: bool = True
) -> dict:
    """Everything the page shows, for every repo. Anything that cannot be read is null, never zero.

    `slots` is the workspace's shared limit on engine sessions, when the repos run as one workspace: then
    the total is that limit, not the sum of each repo's `watch.slots`. With `github=False` nothing is read from
    GitHub: each repo is what this machine knows, marked `pending`, for the page to show while GitHub is read."""
    now = now or datetime.now(UTC)
    repos, feed, minutes, errors, beats = [], [], [], 0, []
    week, day, today = now - timedelta(days=7), now - timedelta(days=1), _local_day(now)
    merged_7d = p1_7d = parked_7d = 0
    t = {"merged": 0, "p1": 0, "parks": 0, "sessions": 0}
    gates_7d: dict[str, int] = defaultdict(int)
    claude = {"sessions_7d": 0, "seconds_7d": 0.0}
    per_tier: dict[str, list[int]] = defaultdict(list)
    for ctx in contexts:
        if not github:
            repos.append({**_local(ctx, ctx.ledger.rows(), now), "pending": True})
        else:
            try:
                repos.append(repo_state(ctx, now))
            except CommandError as exc:  # unknown, not empty: the page must not say there is nothing to do
                repos.append(_unreadable(ctx, ctx.ledger.rows(), now, exc))
        rows = ctx.ledger.rows()
        opened = {r.get("pr"): _ts(r["ts"]) for r in rows if r["event"] == "pr_opened"}
        sessions_by_pr: dict[int, int] = defaultdict(int)
        for r in _sessions(rows):
            if r.get("pr") is not None:
                sessions_by_pr[r["pr"]] += 1
        for r in rows:
            moment = _ts(r["ts"])
            line = _feed_line(ctx.cfg.repo, r)
            if line:
                feed.append(line)
            if moment is None:
                continue
            is_today = _local_day(moment) == today
            e = r["event"]
            if e == "merged":
                if moment >= week:
                    merged_7d += 1
                    if opened.get(r.get("pr")):
                        minutes.append((moment - opened[r["pr"]]).total_seconds() / 60)
                    # the build that opened the PR is a session too, and has no verdict row of its own
                    per_tier[r.get("tier") or "?"].append(sessions_by_pr.get(r.get("pr"), 0) + 1)
                t["merged"] += is_today
            elif e == "verdict":
                if moment >= week:
                    p1_7d += r.get("p1", 0) if r.get("kind") == "gate" else 0
                    if r.get("kind") == "gate" and r.get("engine"):
                        gates_7d[r["engine"]] += 1
                t["p1"] += r.get("p1", 0) if is_today else 0
            elif e == "parked":
                parked_7d += moment >= week
                t["parks"] += is_today
            elif e == "error" and moment >= day:
                errors += 1
            if e in ("build_started", "fix_started", "verdict"):
                t["sessions"] += is_today
            if (
                moment >= week
                and r.get("engine") == "claude"
                and e in ("pr_opened", "fix_started", "verdict")
            ):
                claude["sessions_7d"] += 1
                claude["seconds_7d"] += r.get("seconds") or 0
        beat = ctx.state / "heartbeat"
        if beat.exists():
            beats.append(beat.read_text(encoding="utf-8").strip())
    feed.sort(key=lambda e: e["ts"], reverse=True)
    claude["seconds_7d"] = round(claude["seconds_7d"])
    return {
        "generated": _iso(now),
        "slots": slots,
        "health": {"last_tick": max(beats) if beats else None, "errors_24h": errors},
        "fuel": {
            "codex": fuel.codex_weekly(),
            "claude": None,  # Claude Code keeps no quota file: `/usage` in a session is the only reading
            "gates_7d": dict(gates_7d),
            "claude_usage": claude,  # sessions and seconds vigia ran, not the account's quota
        },
        "stats": {
            "merged_7d": merged_7d,
            "p1_7d": p1_7d,
            "parked_7d": parked_7d,
            "sessions_today": t["sessions"],
            "merged_today": t["merged"],
            "today": t,
            "median_minutes": round(statistics.median(minutes)) if minutes else None,
            "sessions_per_merge": {k: round(sum(v) / len(v), 1) for k, v in sorted(per_tier.items())},
        },
        "repos": repos,
        "feed": feed[:40],
    }


TIMELINE = {
    "pr_opened",
    "verdict",
    "fix_started",
    "pushed",
    "updated",
    "engine_failed",
    "parked",
    "unparked",
    "merged",
}


def pr_detail(ctx: Ctx, n: int) -> dict:
    """One PR's whole story for the detail screen: kept out of /api/state so the poll stays small."""
    pr = ctx.gh.pr(n)
    st = pr_state(ctx, pr)
    seen = Seen(pr, st, decide(st, ctx.cfg))
    target = jobs.target("gate", n)
    running = next(
        (
            r.get("action")
            for r in reversed(ctx.ledger.rows())
            if r["event"] in ("job_spawned", "job_started") and r.get("target") == target
        ),
        None,
    )
    if target not in set(jobs.live(ctx)):
        running = None
    steps, waiting = _pipeline(ctx, seen, running)
    mine = [r for r in ctx.ledger.rows() if r.get("pr") == n]
    explained = {r.get("sha") for r in mine if r["event"] in ("pr_opened", "pushed", "updated")}
    # A P1 counts as fixed only once a later review of the same kind, on another head, came back with no P1 at all:
    # that review looked at everything and found nothing. A later review that reports a different blocker says
    # nothing about this finding, and a head nobody has reviewed yet proves nothing about the code it pointed at.
    clean = [
        (i, r["kind"], r.get("sha"))
        for i, r in enumerate(mine)
        if r["event"] == "verdict"
        and r["verdict"] != "UNAVAILABLE"
        and not any(f.get("severity") == "P1" for f in r.get("findings") or [])
    ]
    timeline = []
    for at, r in enumerate(mine):
        if r["event"] == "head_seen" and r.get("sha") not in explained:
            timeline.append({"ts": r["ts"], "event": "head_outside", "sha": r.get("sha")})
            explained.add(r.get("sha"))  # once per head
        elif r["event"] in TIMELINE:
            item = {k: v for k, v in r.items() if k != "findings"}
            if r["event"] == "verdict":
                rechecked = any(
                    i > at and kind == r["kind"] and sha != r.get("sha") for i, kind, sha in clean
                )
                item["findings"] = [
                    {
                        "severity": f.get("severity"),
                        "file": f.get("file"),
                        "line": f.get("line"),
                        "title": f.get("title"),
                        "status": ("fixed" if rechecked else "open") if f.get("severity") == "P1" else None,
                    }
                    for f in r.get("findings") or []
                ]
            timeline.append(item)
    opened = next((r for r in mine if r["event"] == "pr_opened"), {})
    parked = next((r for r in reversed(mine) if r["event"] == "parked"), None) if st.parked else None
    engine_line = None
    try:
        for c in reversed(ctx.gh.comments(n)):
            line = next((x for x in c["body"].splitlines() if x.startswith("Engine:")), None)
            if line:
                engine_line = line
                break
    except CommandError:
        pass
    return {
        "repo": ctx.cfg.repo,
        "number": n,
        "title": pr["title"],
        "url": pr["url"],
        "tier": st.tier or "?",
        "head": pr["head"],
        "ci": st.ci,
        "diff": pr.get("diff"),
        "parked": st.parked,
        "reason": parked["reason"] if parked else seen.decision.reason,
        "next": seen.decision.action,
        "waiting_for": waiting,
        "pipeline": steps,
        "built_by": f"{opened['engine']} · {opened['model']}" if opened.get("engine") else None,
        "issue": opened.get("issue") or tickets.linked_issue(pr["body"]),
        "opened": opened.get("ts"),
        "tried_total": _tried_total(mine),
        "timeline": timeline[::-1],
        "engine_line": engine_line,
    }


def page() -> bytes:
    """The page served at /: the prototype's markup with no sample data, so it reads /api/state."""
    return PAGE.read_text(encoding="utf-8").replace("{{SAMPLE}}", "").encode("utf-8")


BACKOFF_START = 30.0  # seconds to wait after the first failed refresh; doubles with each one that follows
BACKOFF_MAX = 300.0  # ... up to this
FIRST_WAIT = 0.5  # seconds after its start that the Mesa waits for GitHub before answering locally


def _degraded(repo: dict) -> bool:
    """GitHub could not be read for this repo, whole (`error`) or in part (`unread` names the lists that failed)."""
    return bool(repo.get("error") or repo.get("unread"))


class _Entry:
    """What one key last built: its body (or the error it raised), when, and how many refreshes in a row failed."""

    def __init__(self, at: float, body: bytes, fails: int, error: Exception | None):
        self.at, self.body, self.fails, self.error = at, body, fails, error


def _inline(fn) -> None:
    fn()


def _thread(fn) -> None:
    threading.Thread(target=fn, name="mesa-refresh", daemon=True).start()


class _Cache:
    """What the server answers with. Reading GitHub takes seconds (one `gh` call per list and per PR), so the state
    is refreshed apart from the requests: `spawn` runs a refresh, in a thread for the server, or before `get()`
    returns when it is inline (the default, which tests use)."""

    def __init__(
        self,
        contexts: list[Ctx],
        seconds: float,
        slots: int | None = None,
        clock=time.monotonic,
        spawn=_inline,
        first_wait: float = 0.0,
    ):
        self.contexts, self.seconds, self.slots, self.clock = contexts, seconds, slots, clock
        self.spawn, self.first_wait = spawn, first_wait
        self.lock, self.entries = threading.Lock(), {}  # key -> _Entry
        self.details = threading.Lock()  # one PR detail built at a time, never holding up the state
        self.refreshing = False  # a state refresh is running: at most one at a time
        self.built = threading.Event()  # set once the first state refresh has finished, well or not
        self.born = time.monotonic()  # for `first_wait` seconds from here, a request waits for GitHub
        self.last_ok: dict[str, str] = {}  # repo -> when GitHub was last read for it in full

    def _wait(self, entry: _Entry) -> float:
        """How long the entry stands before a refresh: the cache time, or the backoff after failed refreshes."""
        if not entry.fails:
            return self.seconds
        # the exponent is capped: after ~1000 failures in a row 2 ** n overflows a float before the cap could apply
        return min(max(BACKOFF_START * 2 ** min(entry.fails - 1, 32), self.seconds), BACKOFF_MAX)

    def _due(self, entry: _Entry | None) -> bool:
        return entry is None or self.clock() - entry.at >= self._wait(entry)

    def _build(self, key: str, build, degraded=lambda value: False) -> None:
        """Run `build()` and keep its body, or the error it raised. A refresh that fails (it raised, or `degraded`
        says GitHub could not be read) makes the next one wait longer, so a slow or rate-limiting GitHub is not
        hammered. The wait counts from when the refresh finished, not when it started: a slow GitHub must not use
        up its own backoff while answering. No lock is held while it builds."""
        with self.lock:
            entry = self.entries.get(key)
        fails = entry.fails + 1 if entry else 1
        try:
            value = build()
        except Exception as exc:
            with self.lock:
                self.entries[key] = _Entry(self.clock(), b"", fails, exc)
            return
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        with self.lock:
            self.entries[key] = _Entry(self.clock(), body, fails if degraded(value) else 0, None)

    @staticmethod
    def _served(entry: _Entry) -> bytes:
        if entry.error:
            raise entry.error
        return entry.body

    def _state(self) -> dict:
        s = state(self.contexts, slots=self.slots)
        for repo in s["repos"]:
            if _degraded(repo):
                repo["last_ok"] = self.last_ok.get(repo["repo"])  # null: not read OK since the Mesa started
            else:
                self.last_ok[repo["repo"]] = s["generated"]
        return s

    def _refresh(self) -> None:
        try:
            self._build("state", self._state, lambda s: any(_degraded(r) for r in s["repos"]))
        finally:
            with self.lock:
                self.refreshing = False
            self.built.set()

    def refresh(self) -> None:
        """Start a state refresh when one is due and none is running."""
        with self.lock:
            start = not self.refreshing and self._due(self.entries.get("state"))
            self.refreshing = self.refreshing or start
        if start:
            self.spawn(self._refresh)

    def get(self) -> bytes:
        """The last state built, at most once per cache time (or per backoff, inside which the same answer or the
        same error is served). A request never waits for GitHub: when the state is due, a refresh starts and this
        answer is the one before it. Until the first refresh is done, the answer is what this machine knows on its
        own, every repo marked `pending`, so the page shows something at once and asks again soon."""
        self.refresh()
        with self.lock:
            entry = self.entries.get("state")
        left = self.first_wait - (time.monotonic() - self.born)
        if entry is None and left > 0:  # a GitHub that answers at once is shown at once
            self.built.wait(left)
            with self.lock:
                entry = self.entries.get("state")
        if entry is None:
            return json.dumps(
                state(self.contexts, slots=self.slots, github=False), ensure_ascii=False
            ).encode("utf-8")
        return self._served(entry)

    def pr(self, repo: str, n: int) -> bytes | None:
        """The detail of PR `n` in `repo`, or None when no context drives that repo. It is read while the request
        waits (the page says it is reading), at most once per cache time like the state."""
        ctx = next((c for c in self.contexts if c.cfg.repo.lower() == repo.lower()), None)
        if ctx is None:
            return None
        key = f"pr:{ctx.cfg.repo}#{n}"
        with self.details:
            with self.lock:
                entry = self.entries.get(key)
            if self._due(entry):
                self._build(key, lambda: pr_detail(ctx, n))
                with self.lock:
                    entry = self.entries[key]
        return self._served(entry)


_PR_PATH = re.compile(r"^/api/pr/([\w.-]+)/([\w.-]+)/(\d{1,9})$")


HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; connect-src 'self'; "
    "style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data:",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}


def handler(cache: _Cache, allowed: set[str]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "vigia-mesa"

        def log_message(self, fmt, *args):  # quiet: the Mesa is polled every few seconds
            return

        def _host_ok(self) -> bool:
            # A web page elsewhere can point a hostname at 127.0.0.1 (DNS rebinding) and read this server: only
            # answer requests addressed to a host the person chose.
            host = (self.headers.get("Host") or "").strip().lower()
            name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
            return name in allowed or name.endswith(".ts.net")

        def _send(self, code: int, body: bytes, kind: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            for k, v in HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 - http.server's naming
            if not self._host_ok():
                self._send(403, b"forbidden host\n", "text/plain; charset=utf-8")
            elif (path := self.path.split("?", 1)[0]) in ("/", "/index.html"):
                self._send(200, page(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._json(cache.get)
            elif match := _PR_PATH.match(path):
                self._json(lambda: cache.pr(f"{match.group(1)}/{match.group(2)}", int(match.group(3))))
            else:
                self._send(404, b"not found\n", "text/plain; charset=utf-8")

        def _json(self, build) -> None:
            kind = "application/json; charset=utf-8"
            try:
                body = build()
            except CommandError as exc:  # GitHub said no: a PR that does not exist, or GitHub is down
                missing = "could not resolve" in str(exc).lower() or "not found" in str(exc).lower()
                self._send(404 if missing else 502, json.dumps({"error": str(exc)[:300]}).encode(), kind)
                return
            except Exception as exc:  # report, never crash the server
                self._send(500, json.dumps({"error": str(exc)[:300]}).encode(), kind)
                return
            if body is None:
                self._send(404, b'{"error": "not found"}', "application/json; charset=utf-8")
            else:
                self._send(200, body, "application/json; charset=utf-8")

        def _read_only(self):
            self._send(405, b"the Mesa is read-only: act on GitHub\n", "text/plain; charset=utf-8")

        do_POST = do_PUT = do_PATCH = do_DELETE = _read_only  # noqa: N815

    return Handler


def server(
    contexts: list[Ctx],
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    allow_hosts: tuple[str, ...] = (),
    cache_seconds: float = 15,
    slots: int | None = None,
) -> ThreadingHTTPServer:
    allowed = LOCAL_HOSTS | {h.lower() for h in allow_hosts} | {host.lower()}
    cache = _Cache(contexts, cache_seconds, slots, spawn=_thread, first_wait=FIRST_WAIT)
    cache.refresh()  # read GitHub now, so the first person to open the page rarely waits even for the local state
    return ThreadingHTTPServer((host, port), handler(cache, allowed))
