"""The Mesa: what it shows for each situation, and a server that only reads and only answers its own host."""

import http.client
import json
import re
import shutil
import subprocess
import threading
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from fakes import APPROVE, CHANGES, engine, git
from test_decisions import BODY
from test_e2e import TICKET, world  # noqa: F401  (world is a fixture)
from vigia import jobs, mesa
from vigia.config import Labels
from vigia.proc import CommandError
from vigia.watch import tick


def _pr(state, number):
    return next(p for r in state["repos"] for p in r["prs"] if p["number"] == number)


def test_a_t2_waiting_for_the_human_shows_its_evidence(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([APPROVE, APPROVE])
    gh.add_issue("Touch billing", TICKET, ["vigia:ready", "tier:T2"])
    for _ in range(4):
        tick(ctx)
    pr = next(iter(gh.prs_))
    p = _pr(mesa.state([ctx]), pr)
    assert p["waiting_for"] == "human"
    assert p["pipeline"] == {
        "build": "done",
        "ci": "done",
        "gate": "done",
        "review": "done",
        "human": "wait",
        "merge": "todo",
    }
    assert p["evidence"][0] == "CI green on this head"
    assert any(e.startswith("Gate APPROVE · codex") for e in p["evidence"])
    assert p["built_by"] == "claude · claude-opus-5-5"


def test_a_parked_pr_shows_where_it_stopped_and_why(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([CHANGES, CHANGES])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    for _ in range(5):  # build, gate, fix, gate, park
        tick(ctx)
    pr = next(iter(gh.prs_))
    p = _pr(mesa.state([ctx]), pr)
    assert p["parked"] and "gate round 2" in p["reason"]
    assert p["pipeline"]["gate"] == "stop" and p["pipeline"]["review"] == "skip"
    assert "1 fix round(s) and 2 gate round(s)" in p["tried"]
    assert p["issue_url"].endswith("/issues/1")


def test_a_running_job_shows_on_its_pr(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx)
    pr = next(iter(gh.prs_))
    ctx.ledger.append("job_started", action="gate", target=f"pr-{pr}")
    with jobs.hold(ctx, f"pr-{pr}"):
        repo = mesa.state([ctx])["repos"][0]
    assert repo["jobs"][0]["action"] == "gate" and repo["jobs"][0]["number"] == pr
    assert _pr({"repos": [repo]}, pr)["pipeline"]["gate"] == "now"


def test_feed_stats_and_health_come_from_the_ledger(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([APPROVE])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    for _ in range(3):
        tick(ctx)
    s = mesa.state([ctx])
    events = [e["event"] for e in s["feed"]]
    assert events[0] == "merged" and "verdict" in events and "pr_opened" in events
    assert s["stats"]["merged_7d"] == 1 and s["health"]["last_tick"]
    assert s["fuel"]["claude"] is None  # unknown is null, never zero


def _serve(ctx):
    srv = mesa.server([ctx], "127.0.0.1", 0, cache_seconds=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _get(srv, path, host="127.0.0.1", method="GET"):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    conn.request(method, path, headers={"Host": f"{host}:{srv.server_address[1]}"})
    resp = conn.getresponse()
    return resp.status, resp.getheader("Content-Type"), resp.read()


def test_the_server_serves_the_page_and_the_state(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    srv = _serve(ctx)
    try:
        status, kind, body = _get(srv, "/")
        assert status == 200 and kind == "text/html; charset=utf-8"
        assert b"{{SAMPLE}}" not in body and b'<meta charset="utf-8">' in body
        status, kind, body = _get(srv, "/api/state", host="localhost")
        assert status == 200 and json.loads(body)["repos"][0]["repo"] == gh.repo
    finally:
        srv.shutdown()


def test_the_page_has_the_five_places_and_writes_nothing(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    srv = _serve(ctx)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        conn.request("GET", "/", headers={"Host": f"127.0.0.1:{srv.server_address[1]}"})
        resp = conn.getresponse()
        body, csp = resp.read(), resp.getheader("Content-Security-Policy")
    finally:
        srv.shutdown()
    assert "connect-src 'self'" in csp and "script-src 'unsafe-inline'" in csp
    assert b'<meta name="viewport"' in body and b'id="mesa-sample"' in body and b"{{SAMPLE}}" not in body
    for place in (b'"Your turn"', b'"Factory"', b'"Captures"', b'"Feed"', b'"Fuel"'):
        assert place in body
    # read-only: no form, no write verb, no script from elsewhere, no markup built from strings
    assert b"<form" not in body and b"method:" not in body and b"<script src" not in body
    assert b"innerHTML" not in body and b'fetch("/api/state"' in body and b'fetch("/api/pr/"' in body
    assert b"copy by hand" in body.lower() and b"mesa.theme" in body and b"mesa.tab" in body


def test_the_server_is_read_only_and_answers_only_its_own_host(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    srv = _serve(ctx)
    try:
        assert _get(srv, "/api/state", host="evil.example")[0] == 403  # DNS rebinding
        assert _get(srv, "/api/state", host="mesa.tail1234.ts.net")[0] == 200  # behind tailscale serve
        assert _get(srv, "/api/state", method="POST")[0] == 405
        assert _get(srv, "/nope")[0] == 404
    finally:
        srv.shutdown()


PLACES = ("turn", "factory", "captures", "feed", "fuel", "decide")
LINK = re.compile(
    r"^(#/[A-Za-z0-9_./-]*|https://claude\.ai/new|https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
    r"(/(pull|issues)/\d{1,9}(/files)?)?(/issues\?q=[A-Za-z0-9%._+:-]*)?(#new_comment_field)?)$"
)


def _render(tmp_path, state, routes=PLACES):
    """What the page shows for `state` on each route, at phone width and at desktop width: the page's own scripts
    run under Node (tests/mesa_render.js) with a small DOM, so these are the cards, words and links it builds."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    data = tmp_path / "state.json"
    data.write_text(json.dumps(state), encoding="utf-8")
    script = Path(__file__).with_name("mesa_render.js")
    out = subprocess.run(
        [node, str(script), str(mesa.PAGE), str(data), *routes], capture_output=True, text=True, check=False
    )
    assert out.returncode == 0, out.stderr
    shown = json.loads(out.stdout)
    for layout in shown.values():
        for view in layout.values():
            bad = [href for href in view["links"] if not LINK.match(href)]
            assert not bad, f"links outside the allow-list: {bad}"
    return shown


def _views(shown, route):
    """The same route on both layouts."""
    return [shown["phone"][route], shown["desk"][route]]


def test_a_repo_that_cannot_be_read_is_shown_as_unknown_not_as_empty(world, tmp_path):  # noqa: F811
    gh, build = world
    ctx, _ = build([])

    def expired(label):
        raise CommandError(["gh", "pr", "list"], 1, "", "HTTP 401: Bad credentials")

    gh.prs = expired
    n = gh.add_issue("Add a helper", TICKET, ["vigia:building", "tier:T1"])
    ctx.ledger.append("job_started", action="build", target=f"issue-{n}")
    ctx.ledger.append(
        "build_started", issue=n, tier="T1", engine="claude", model="m", branch=f"vigia/{n}-add"
    )
    with jobs.hold(ctx, f"issue-{n}"):
        repo = mesa.state([ctx])["repos"][0]
    assert "Bad credentials" in repo["error"]
    for name in ("prs", "queue", "parked_issues", "refused", "human_tasks", "proposed", "decisions"):
        assert repo[name] is None, name  # unknown, not zero
    # what this machine knows on its own still shows: the running build with its seat, the labels, the ledger
    assert [(j["number"], j["engine"], j["branch"]) for j in repo["jobs"]] == [
        (n, "claude", f"vigia/{n}-add")
    ]
    assert repo["labels"]["ready"] == "vigia:ready" and repo["merged_today"] == []
    shown = _render(tmp_path, mesa.state([ctx]))
    for turn, factory in zip(_views(shown, "turn"), _views(shown, "factory"), strict=True):
        assert "Nothing needs you" not in turn["text"] and "Cannot read" in turn["text"]
        assert "Bad credentials" in turn["text"] and "gh auth status" in turn["text"]
        assert "No open pull requests" not in factory["text"] and "unknown" in factory["text"]
        assert "0 open PRs" not in factory["text"] and "queue empty" not in factory["text"]


def test_a_ticket_parked_before_it_had_a_pr_needs_you(world, tmp_path):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    ctx.engines._run = engine([], edits=lambda cwd, k: None)
    n = gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx)  # the builder leaves no changes: the issue is parked, no PR is opened
    s = mesa.state([ctx])
    failed = ctx.ledger.last("build_failed", issue=n)
    assert s["repos"][0]["parked_issues"] == [
        {
            "number": n,
            "title": "Add a helper",
            "url": "",
            "reason": "the builder left no changes",
            "since": failed["ts"],
        }
    ]
    assert s["feed"][0]["event"] == "build_failed" and "left no changes" in s["feed"][0]["text"]
    shown = _render(tmp_path, s, routes=("turn",))
    for turn in _views(shown, "turn"):
        assert "Nothing needs you" not in turn["text"]
        assert f"Build failed · {gh.repo.split('/')[-1]} #{n}" in turn["text"]
        assert "Add a helper" in turn["text"] and "the builder left no changes" in turn["text"]


def test_a_workspace_counts_its_shared_limit_not_the_sum_of_its_repos(world, tmp_path):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    s = mesa.state([ctx], slots=2)
    assert s["slots"] == 2
    two = dict(
        s["repos"][0], slots=1, jobs=[{"target": "pr-1", "number": 1, "issue": False, "action": "gate"}] * 2
    )
    other = dict(s["repos"][0], repo="acme/other", slots=1, jobs=[])
    shown = _render(tmp_path, dict(s, repos=[two, other]), routes=("factory",))
    assert "sessions 2 / 2" in shown["desk"]["factory"]["text"]
    assert "2 of 2 slots" in shown["phone"]["factory"]["text"]
    alone = dict(s, slots=None, repos=[two, other])  # no workspace: each repo's own watch.slots adds up
    shown = _render(tmp_path, alone, routes=("factory",))
    assert "sessions 2 / 3" in shown["desk"]["factory"]["text"]
    assert "2 of 3 slots" in shown["phone"]["factory"]["text"]


def test_a_list_that_cannot_be_read_is_never_shown_as_nothing_to_do(world, tmp_path):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    real = gh.issues

    def flaky(label):
        if label == "vigia:human-task":
            raise CommandError(["gh", "issue", "list"], 1, "", "HTTP 502")
        return real(label)

    gh.issues = flaky
    repo = mesa.state([ctx])["repos"][0]
    assert repo["human_tasks"] is None and repo["unread"] == ["human tasks"]
    for turn in _views(_render(tmp_path, mesa.state([ctx]), routes=("turn",)), "turn"):
        assert "Nothing needs you" not in turn["text"] and "Could not read: human tasks" in turn["text"]


def test_decisions_that_cannot_be_read_are_unknown_not_none(world, tmp_path):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx)  # opens a PR
    real = gh.issues

    def flaky(label):
        if label == "vigia:decision":
            raise CommandError(["gh", "issue", "list"], 1, "", "HTTP 502")
        return real(label)

    gh.issues = flaky
    s = mesa.state([ctx])
    repo = s["repos"][0]
    assert repo["decisions"] is None and repo["unread"] == ["decisions"]
    assert repo["prs"][0]["blocked_by"] is None  # whether a decision blocks it is unknown
    assert mesa.pr_detail(ctx, repo["prs"][0]["number"])["blocked_by"] is None
    shown = _render(tmp_path, s, routes=("turn", "decide"))
    for turn, decide in zip(_views(shown, "turn"), _views(shown, "decide"), strict=True):
        assert "Nothing needs you" not in turn["text"] and "Could not read: decisions" in turn["text"]
        assert "No open decisions" not in decide["text"] and "Decisions unknown" in decide["text"]


def test_a_refused_ticket_stays_until_its_issue_resolves_it_whatever_its_age(world, tmp_path):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    n = gh.add_issue("Too vague", "## Outcome\nmake it better\n", ["tier:T1"])
    ctx.ledger.append("refused", issue=n, gaps=["no Scope section"])
    later = datetime.fromisoformat(ctx.ledger.rows()[-1]["ts"].replace("Z", "+00:00")) + timedelta(days=30)
    repo = mesa.state([ctx], later)["repos"][0]
    assert [i["number"] for i in repo["refused"]] == [n]  # a month later, still there
    assert repo["refused"][0]["since"] == ctx.ledger.last("refused", issue=n)["ts"]
    gh.issues_[n]["state"] = "CLOSED"
    assert mesa.state([ctx], later)["repos"][0]["refused"] == []  # gone once the issue resolves it


def test_every_pending_refusal_shows_however_many_newer_ones_were_resolved(world, tmp_path):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    first = gh.add_issue("Still vague", "x", ["tier:T1"])
    ctx.ledger.append("refused", issue=first, gaps=["no Outcome section"])
    for k in range(12):
        n = gh.add_issue(f"Gave up {k}", "x", ["tier:T1"])
        ctx.ledger.append("refused", issue=n, gaps=["no Outcome section"])
        gh.issues_[n]["state"] = "CLOSED"
    repo = mesa.state([ctx])["repos"][0]
    assert [i["number"] for i in repo["refused"]] == [first] and repo["refused"][0]["missing"] == [
        "no Outcome section"
    ]


def test_refusals_that_cannot_be_checked_are_unknown_not_none(world, tmp_path):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    n = gh.add_issue("Vague", "x", ["tier:T1"])
    ctx.ledger.append("refused", issue=n, gaps=["no Outcome section"])

    def down():
        raise CommandError(["gh", "api"], 1, "", "HTTP 502")

    gh.open_issues = down
    repo = mesa.state([ctx])["repos"][0]
    assert repo["refused"] is None and "refused tickets" in repo["unread"]
    for turn in _views(_render(tmp_path, mesa.state([ctx]), routes=("turn",)), "turn"):
        assert "Nothing needs you" not in turn["text"] and "Could not read: refused tickets" in turn["text"]


def test_a_refused_ticket_that_is_being_built_is_no_longer_refused(world, tmp_path):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    n = gh.add_issue("Add a helper", "## Outcome\nmake it better\n", ["vigia:ready", "tier:T0"])
    tick(ctx)  # refused: the ready label is removed
    assert [i["number"] for i in mesa.state([ctx])["repos"][0]["refused"]] == [n]
    gh.issues_[n]["body"] = TICKET
    gh.add_labels(n, "vigia:ready")
    tick(ctx)  # built: ready is removed again, and a PR is opened
    assert gh.prs_ and "vigia:ready" not in gh.issues_[n]["labels"]
    assert mesa.state([ctx])["repos"][0]["refused"] == []
    ctx.ledger.append("refused", issue=n, gaps=["no Scope section"])  # refused again after that build
    assert mesa.state([ctx])["repos"][0]["refused"][0]["missing"] == ["no Scope section"]


def test_the_page_names_the_labels_the_repo_configured(world, tmp_path):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    labels = Labels(ready="todo:agent", parked="on-hold", human_ok="approved:head")
    ctx, _ = build([APPROVE, APPROVE], cfg=replace(ctx.cfg, labels=labels))
    gh.add_issue("Touch billing", TICKET, ["todo:agent", "tier:T2"])
    for _ in range(4):
        tick(ctx)
    s = mesa.state([ctx])
    pr = next(iter(gh.prs_))
    assert _pr(s, pr)["waiting_for"] == "human"
    repo = s["repos"][0]
    parked = dict(_pr(s, pr), number=pr + 1, next="wait", waiting_for=None, parked=True)
    issue = {"number": 99, "title": "Broke", "url": "", "reason": "the builder left no changes"}
    planned = {"number": 98, "title": "Planned", "url": "", "tier": "T1"}
    detail = mesa.pr_detail(ctx, pr)
    page = dict(
        s,
        repos=[dict(repo, prs=repo["prs"] + [parked], parked_issues=[issue], proposed=[planned], queue=[])],
        pr_details={
            f"{gh.repo}#{pr}": detail,
            f"{gh.repo}#{pr + 1}": dict(detail, number=pr + 1, parked=True, waiting_for=None),
        },
    )
    shown = _render(
        tmp_path, page, routes=("turn", "factory", f"pr/{gh.repo}/{pr}", f"pr/{gh.repo}/{pr + 1}")
    )
    for layout in shown.values():
        turn, factory = layout["turn"]["html"], layout["factory"]["html"]
        assert "<code>approved:head</code>" in turn and "vigia:human-ok" not in turn
        # the ticket parked before a PR, then the parked PR, then the planned ticket
        assert "remove <code>on-hold</code> and add <code>todo:agent</code>" in turn
        assert "then remove <code>on-hold</code>" in turn
        assert "<code>vigia:proposed</code> for <code>todo:agent</code>" in turn
        assert "vigia:parked" not in turn and "vigia:ready" not in turn
        assert "vigia:ready" not in factory
        waiting, stopped = layout[f"pr/{gh.repo}/{pr}"]["html"], layout[f"pr/{gh.repo}/{pr + 1}"]["html"]
        assert "<code>approved:head</code>" in waiting and "vigia:human-ok" not in waiting
        assert "Removing <code>on-hold</code>" in stopped and "vigia:parked" not in stopped
    assert "add <code>todo:agent</code>" in shown["phone"]["factory"]["html"]  # the phone's empty queue


def test_every_place_renders_on_both_layouts_and_says_when_nothing_needs_you(world, tmp_path):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    s = mesa.state([ctx])  # every list read, all empty
    shown = _render(tmp_path, s)
    for turn in _views(shown, "turn"):
        assert "Nothing needs you" in turn["text"] and "Could not read" not in turn["text"]
    for route, words in (
        ("factory", "Sessions"),
        ("captures", "No UI changes"),
        ("feed", "Today"),
        ("fuel", "Codex"),
        ("decide", "No open decisions"),
    ):
        for view in _views(shown, route):
            assert words in view["text"], route


def test_the_real_state_renders_every_card_and_its_detail(world, tmp_path):  # noqa: F811
    gh, build = world
    ctx, _ = build([CHANGES, APPROVE, APPROVE])
    gh.add_issue("Touch billing", TICKET, ["vigia:ready", "tier:T2"])
    for _ in range(6):  # build, gate P1, fix, gate, review, wait for the human
        tick(ctx)
    pr = next(iter(gh.prs_))
    d = gh.add_issue("Pick a path", BODY.replace("#38, #39", f"#{pr}"), ["vigia:decision"])
    gh.add_issue("Buy the domain", "Time: 10 min\nDue: 2026-10-27\nOrigin: plan\n", ["vigia:human-task"])
    s = dict(mesa.state([ctx], _now(ctx)), pr_details={f"{gh.repo}#{pr}": mesa.pr_detail(ctx, pr)})
    routes = (*PLACES, f"decide/{gh.repo}/{d}", f"pr/{gh.repo}/{pr}")
    shown = _render(tmp_path, s, routes=routes)
    for turn in _views(shown, "turn"):
        assert (
            "Touch billing" in turn["text"] and "Should engine sessions keep a GitHub login?" in turn["text"]
        )
        assert "Buy the domain" in turn["text"] and "Nothing needs you" not in turn["text"]
    for view in _views(shown, f"decide/{gh.repo}/{d}"):
        assert "Isolate the session" in view["text"] and "Answer C on GitHub" in view["text"]
    for view in _views(shown, f"pr/{gh.repo}/{pr}"):
        assert "What was tried" in view["text"] and "Gate round 1" in view["text"]


def test_the_page_puts_every_api_string_in_as_text_and_links_only_github(world, tmp_path):  # noqa: F811
    gh, build = world
    ctx, _ = build([APPROVE, APPROVE])
    gh.add_issue("Touch billing", TICKET, ["vigia:ready", "tier:T2"])
    for _ in range(4):
        tick(ctx)
    s = mesa.state([ctx])
    repo = s["repos"][0]
    evil = '<img src=x onerror="alert(1)">'
    pr = dict(repo["prs"][0], title=evil, url="javascript:alert(1)")
    task = {"number": 7, "title": evil, "url": "https://evil.example/steal", "since": None}
    page = dict(s, repos=[dict(repo, prs=[pr], human_tasks=[task], unread=[evil])])
    shown = _render(tmp_path, page, routes=("turn", "factory"))  # _render also checks every link
    for layout in shown.values():
        for view in layout.values():
            assert "img" not in view["tags"] and "script" not in view["tags"]
            assert evil not in view["html"] and "&lt;img src=x" in view["html"]
            assert not any("javascript:" in href or "evil.example" in href for href in view["links"])
        assert evil in layout["turn"]["text"]  # shown, as text


def _now(ctx):
    """Just after the ledger's last row: the fake clock and "today" agree on the day."""
    return datetime.fromisoformat(ctx.ledger.rows()[-1]["ts"].replace("Z", "+00:00")) + timedelta(seconds=5)


def test_a_human_wait_says_since_when_and_what_it_cost(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([CHANGES, APPROVE, APPROVE])
    gh.add_issue("Touch billing", TICKET, ["vigia:ready", "tier:T2"])
    for _ in range(6):  # build, gate P1, fix, gate, review, wait
        tick(ctx)
    pr = next(iter(gh.prs_))
    p = _pr(mesa.state([ctx], _now(ctx)), pr)
    review = ctx.ledger.last("verdict", pr=pr, kind="review")
    assert p["waiting_for"] == "human" and p["since"] == review["ts"]
    assert p["tried_total"] == {
        "gate_rounds": 2,
        "fix_rounds": 1,
        "heads": 2,
        "p1": 1,
        "parks": 0,
        "unparks": 0,
    }


def test_jobs_show_their_engine_and_merges_today_their_title(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([APPROVE])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    for _ in range(3):
        tick(ctx)
    gh.add_issue("Add another helper", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx)  # opens the second PR
    second = max(gh.prs_)
    ctx.ledger.append("job_started", action="gate", target=f"pr-{second}")
    with jobs.hold(ctx, f"pr-{second}"):
        s = mesa.state([ctx], _now(ctx))
    repo = s["repos"][0]
    assert repo["jobs"][0]["engine"] == "codex" and repo["jobs"][0]["model"] == ctx.cfg.tiers["T0"].gate.model
    assert repo["jobs"][0]["tier"] == "T0" and repo["jobs"][0]["branch"] is None
    assert repo["merged_today"][0]["title"] == "Add a helper" and repo["session_minutes"] == 30
    assert s["stats"]["merged_today"] == 1 and s["stats"]["today"]["merged"] == 1
    assert s["stats"]["sessions_per_merge"] == {"T0": 2.0}  # one build, one gate
    assert s["fuel"]["gates_7d"] == {"codex": 1}


def test_a_build_shows_its_seat_tier_and_branch(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    n = gh.add_issue("Add a helper", TICKET, ["vigia:building", "tier:T1"])
    ctx.ledger.append("job_started", action="build", target=f"issue-{n}")
    ctx.ledger.append(
        "build_started",
        issue=n,
        tier="T1",
        engine="claude",
        model="claude-sonnet-5-5",
        branch=f"vigia/{n}-add",
    )
    with jobs.hold(ctx, f"issue-{n}"):
        job = mesa.state([ctx])["repos"][0]["jobs"][0]
    assert job["issue"] and job["number"] == n and job["action"] == "build"
    assert (job["engine"], job["model"], job["tier"], job["branch"]) == (
        "claude",
        "claude-sonnet-5-5",
        "T1",
        f"vigia/{n}-add",
    )


def test_decisions_tasks_and_blocked_tickets_reach_the_page(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    blocked = gh.add_issue(
        "Wait for it", TICKET + "\nBlocked by: #99\n", ["vigia:ready", "tier:T0", "vigia:building"]
    )
    gh.issues_[blocked]["labels"] = ["tier:T0", "vigia:ready"]
    d = gh.add_issue("Pick a path", BODY.replace("#38, #39", f"#{blocked}"), ["vigia:decision"])
    gh.add_issue("Buy the domain", "Time: 10 min\nDue: 2026-10-27\nOrigin: plan\n", ["vigia:human-task"])
    gh.add_issue("Broken", "no shape at all", ["vigia:decision"])
    repo = mesa.state([ctx])["repos"][0]
    good, broken = repo["decisions"]
    assert good["number"] == d and good["rec"] == "C" and not good["malformed"] and good["answered"] is None
    assert broken["malformed"] and broken["question"] == "Broken"
    assert repo["queue"][0]["blocked_by"] == [d, 99]
    task = repo["human_tasks"][0]
    assert (task["time"], task["due"], task["origin"]) == ("10 min", "2026-10-27", "plan")


def test_the_feed_carries_fields_and_more_events(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([CHANGES, CHANGES, APPROVE])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    for _ in range(5):
        tick(ctx)
    pr = next(iter(gh.prs_))
    gh.prs_[pr]["labels"].remove("vigia:parked")
    tick(ctx)  # records the unpark, and gates again
    feed = mesa.state([ctx])["feed"]
    events = [e["event"] for e in feed]
    assert {"unparked", "parked", "pushed", "build_started", "verdict"} <= set(events)
    v = next(e for e in feed if e["event"] == "verdict" and e["verdict"] == "CHANGES")
    assert v["kind"] == "gate" and v["verdict"] == "CHANGES" and v["p1"] == 1 and len(v["sha"]) == 7
    assert v["engine"] == "codex" and v["repo"] == gh.repo.split("/")[-1] and v["text"]


def test_pr_detail_tells_the_whole_story(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([CHANGES, CHANGES])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    for _ in range(4):  # build, gate P1, fix, gate P1 again
        tick(ctx)
    pr = next(iter(gh.prs_))
    branch = gh.prs_[pr]["branch"]
    work = ctx.root / "outside"
    git("clone", "-q", "-b", branch, str(gh.origin), str(work), cwd=ctx.root)
    git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "by hand",
        cwd=work,
    )
    git("push", "-q", "origin", branch, cwd=work)
    tick(ctx)  # the watcher sees the new head
    d = mesa.pr_detail(ctx, pr)
    kinds = [e["event"] for e in d["timeline"]]
    assert kinds[-1] == "pr_opened" and "head_outside" in kinds and "pushed" in kinds
    verdicts = [e for e in d["timeline"] if e["event"] == "verdict"]
    assert [f["status"] for v in verdicts for f in v["findings"]] == [
        "fixed",
        "fixed",
    ]  # neither is on this head
    assert d["engine_line"].startswith("Engine: claude")
    assert d["tried_total"]["gate_rounds"] == 2 and d["built_by"].startswith("claude")


def test_the_server_serves_a_pr_detail_and_nothing_else(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx)
    pr = next(iter(gh.prs_))
    srv = _serve(ctx)
    try:
        status, kind, body = _get(srv, f"/api/pr/{gh.repo}/{pr}")
        assert status == 200 and json.loads(body)["number"] == pr
        assert _get(srv, f"/api/pr/someone/else/{pr}")[0] == 404
        assert _get(srv, f"/api/pr/{gh.repo}/{pr}/../../etc")[0] == 404
        assert _get(srv, f"/api/pr/{gh.repo}/{pr}", host="evil.example")[0] == 403
    finally:
        srv.shutdown()
