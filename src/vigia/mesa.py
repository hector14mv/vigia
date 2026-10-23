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
import statistics
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import fuel, jobs, tickets
from .context import Ctx
from .proc import CommandError
from .watch import sense

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


def repo_state(ctx: Ctx, now: datetime) -> dict:
    cfg, labels = ctx.cfg, ctx.cfg.labels
    rows = ctx.ledger.rows()
    live = set(jobs.live(ctx))
    started = {r.get("target"): r for r in rows if r["event"] in ("job_spawned", "job_started")}
    running = []
    for name in sorted(live):
        kind, _, number = name.partition("-")
        row = started.get(name) or {}
        running.append(
            {
                "target": name,
                "number": int(number) if number.isdigit() else None,
                "issue": kind == "issue",
                "action": row.get("action") or "build",
                "since": row.get("ts"),
            }
        )
    prs = []
    for s in sense(ctx, track=False):
        n = s.pr["number"]
        job = next((j for j in running if j["number"] == n and not j["issue"]), None)
        steps, waiting = _pipeline(ctx, s, job["action"] if job else None)
        opened = ctx.ledger.last("pr_opened", pr=n) or {}
        evidence = []
        if s.state.ci == "passing":
            evidence.append("CI green on this head")
        for kind, what in (("gate", "Gate"), ("review", f"{s.state.tier} review")):
            v = ctx.ledger.last("verdict", pr=n, kind=kind, sha=s.pr["head"])
            if v and v.get("verdict") != "UNAVAILABLE":
                evidence.append(_verdict_line(v, what))
        earlier = [
            r for r in rows if r["event"] == "verdict" and r.get("pr") == n and r.get("sha") != s.pr["head"]
        ]
        if earlier:
            p1 = sum(r.get("p1", 0) for r in earlier)
            evidence.append(
                f"{len(earlier)} earlier review(s) found {p1} P1"
                + ("s" if p1 != 1 else "")
                + ", all on older heads"
            )
        parked = ctx.ledger.last("parked", pr=n) if s.state.parked else None
        issue = opened.get("issue") or tickets.linked_issue(s.pr["body"])
        prs.append(
            {
                "number": n,
                "title": s.pr["title"],
                "url": s.pr["url"],
                "tier": s.state.tier or "?",
                "head": s.pr["head"],
                "ci": s.state.ci,
                "next": s.decision.action,
                "reason": parked["reason"] if parked else s.decision.reason,
                "parked": s.state.parked,
                "waiting_for": waiting,
                "pipeline": steps,
                "evidence": evidence,
                "built_by": f"{opened['engine']} · {opened['model']}" if opened.get("engine") else None,
                "tried": f"{s.state.fix_rounds} fix round(s) and "
                f"{len({v.sha for v in s.state.verdicts if v.kind == 'gate' and v.content})} gate round(s) since it was last unparked",
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
    week_ago = now - timedelta(days=7)
    refused, seen_issues = [], set()
    for r in reversed(rows):
        if r["event"] != "refused" or r.get("issue") in seen_issues or (_ts(r["ts"]) or now) < week_ago:
            continue
        seen_issues.add(r["issue"])
        try:
            issue = ctx.gh.issue(r["issue"])
        except CommandError:
            continue
        if issue["state"] == "OPEN" and labels.ready not in issue["labels"]:
            refused.append(
                {
                    "number": issue["number"],
                    "title": issue["title"],
                    "url": issue["url"],
                    "missing": r.get("gaps") or [],
                }
            )
        if len(refused) >= 5:
            break
    try:
        human_tasks = [
            {"number": i["number"], "title": i["title"], "url": i["url"]}
            for i in ctx.gh.issues("vigia:human-task")
        ]
    except CommandError:
        human_tasks = []
    return {
        "repo": cfg.repo,
        "slots": cfg.slots,
        "jobs": running,
        "prs": prs,
        "queue": queue,
        "refused": refused,
        "human_tasks": human_tasks,
    }


def _feed_line(repo: str, r: dict) -> dict | None:
    short = repo.split("/")[-1]
    e = r["event"]
    if e == "merged":
        return {
            "ts": r["ts"],
            "event": e,
            "text": f"{short} #{r['pr']} merged at {str(r.get('sha'))[:8]} · tier {r.get('tier')}",
        }
    if e == "parked":
        return {"ts": r["ts"], "event": e, "text": f"{short} #{r['pr']} — {r.get('reason')}"}
    if e == "verdict" and r.get("verdict") != "UNAVAILABLE":
        return {
            "ts": r["ts"],
            "event": e,
            "verdict": r.get("verdict"),
            "p1": r.get("p1", 0),
            "text": f"{short} #{r['pr']} {r.get('kind')} by {r.get('engine')} · {r.get('model')} on {str(r.get('sha'))[:8]}",
        }
    if e == "pr_opened":
        return {
            "ts": r["ts"],
            "event": e,
            "text": f"{short} #{r['pr']} opened from #{r.get('issue')} by {r.get('engine')} · {r.get('model')}",
        }
    if e == "pushed":
        return {
            "ts": r["ts"],
            "event": e,
            "text": f"{short} #{r['pr']} fix pushed {str(r.get('sha'))[:8]} ({r.get('stage')})",
        }
    return None


def state(contexts: list[Ctx], now: datetime | None = None) -> dict:
    """Everything the page shows, for every repo. Anything that cannot be read is null, never zero."""
    now = now or datetime.now(UTC)
    repos, feed, merged_7d, p1_7d, minutes, sessions_today, errors, beats = [], [], 0, 0, [], 0, 0, []
    week, day = now - timedelta(days=7), now - timedelta(days=1)
    today = now.strftime("%Y-%m-%d")
    for ctx in contexts:
        try:
            repos.append(repo_state(ctx, now))
        except CommandError as exc:
            repos.append(
                {
                    "repo": ctx.cfg.repo,
                    "slots": ctx.cfg.slots,
                    "jobs": [],
                    "prs": [],
                    "queue": [],
                    "refused": [],
                    "human_tasks": [],
                    "error": str(exc)[:300],
                }
            )
        rows = ctx.ledger.rows()
        opened = {r.get("pr"): _ts(r["ts"]) for r in rows if r["event"] == "pr_opened"}
        for r in rows:
            moment = _ts(r["ts"])
            line = _feed_line(ctx.cfg.repo, r)
            if line:
                feed.append(line)
            if moment is None:
                continue
            if r["event"] == "merged" and moment >= week:
                merged_7d += 1
                if opened.get(r.get("pr")):
                    minutes.append((moment - opened[r["pr"]]).total_seconds() / 60)
            if r["event"] == "verdict" and r.get("kind") == "gate" and moment >= week:
                p1_7d += r.get("p1", 0)
            if r["event"] in ("verdict", "build_started", "fix_started") and r["ts"].startswith(today):
                sessions_today += 1
            if r["event"] == "error" and moment >= day:
                errors += 1
        beat = ctx.state / "heartbeat"
        if beat.exists():
            beats.append(beat.read_text(encoding="utf-8").strip())
    feed.sort(key=lambda e: e["ts"], reverse=True)
    return {
        "generated": _iso(now),
        "health": {"last_tick": max(beats) if beats else None, "errors_24h": errors},
        "fuel": {"codex": fuel.codex_weekly(), "claude": None},
        "stats": {
            "merged_7d": merged_7d,
            "p1_7d": p1_7d,
            "sessions_today": sessions_today,
            "median_minutes": round(statistics.median(minutes)) if minutes else None,
        },
        "repos": repos,
        "feed": feed[:40],
    }


def page() -> bytes:
    """The page served at /: the prototype's markup with no sample data, so it reads /api/state."""
    return PAGE.read_text(encoding="utf-8").replace("{{SAMPLE}}", "").encode("utf-8")


class _Cache:
    def __init__(self, contexts: list[Ctx], seconds: float):
        self.contexts, self.seconds = contexts, seconds
        self.lock, self.body, self.at = threading.Lock(), b"", 0.0

    def get(self) -> bytes:
        with self.lock:
            if time.monotonic() - self.at > self.seconds or not self.body:
                self.body = json.dumps(state(self.contexts), ensure_ascii=False).encode("utf-8")
                self.at = time.monotonic()
            return self.body


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
            elif self.path in ("/", "/index.html"):
                self._send(200, page(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                try:
                    self._send(200, cache.get(), "application/json; charset=utf-8")
                except Exception as exc:  # report, never crash the server
                    self._send(
                        500, json.dumps({"error": str(exc)[:300]}).encode(), "application/json; charset=utf-8"
                    )
            else:
                self._send(404, b"not found\n", "text/plain; charset=utf-8")

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
) -> ThreadingHTTPServer:
    allowed = LOCAL_HOSTS | {h.lower() for h in allow_hosts} | {host.lower()}
    return ThreadingHTTPServer((host, port), handler(_Cache(contexts, cache_seconds), allowed))
