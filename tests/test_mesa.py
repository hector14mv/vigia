"""The Mesa: what it shows for each situation, and a server that only reads and only answers its own host."""

import http.client
import json
import re
import shutil
import subprocess
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from fakes import APPROVE, CHANGES, engine, git
from test_decisions import BODY
from test_e2e import TICKET, world  # noqa: F401  (world is a fixture)
from vigia import builder, jobs, mesa
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
    # a PR never depends on the decisions in this vigia, so not reading them leaves the PR as it is
    assert "blocked_by" not in repo["prs"][0]
    assert "blocked_by" not in mesa.pr_detail(ctx, repo["prs"][0]["number"])
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
        assert "Isolate the session" in view["text"] and "Answer on GitHub" in view["text"]
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


def test_a_watcher_running_a_session_in_its_tick_is_busy_not_quiet(world, tmp_path):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    s = mesa.state([ctx])
    then = datetime.fromisoformat(s["generated"].replace("Z", "+00:00")) - timedelta(minutes=8)
    job = {"target": "pr-4", "number": 4, "issue": False, "action": "gate", "since": mesa._iso(then)}
    stale = dict(s, health=dict(s["health"], last_tick=mesa._iso(then)))
    one = dict(stale, repos=[dict(s["repos"][0], slots=1, jobs=[job])])
    shown = _render(tmp_path, one, routes=("turn",))
    assert "busy · gate #4" in shown["phone"]["turn"]["text"]
    assert "watcher busy · gate #4" in shown["desk"]["turn"]["text"]
    # a workspace with two slots runs sessions as jobs, so its watcher is not inside one: a stale tick is quiet
    shown = _render(tmp_path, dict(one, slots=2), routes=("turn",))
    for turn in _views(shown, "turn"):
        assert "busy" not in turn["text"] and "watcher quiet" in turn["text"]


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


def test_decisions_and_tasks_reach_the_page(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    named = gh.add_issue("Wait for it", TICKET, ["vigia:ready", "tier:T0"])
    d = gh.add_issue("Pick a path", BODY.replace("#38, #39", f"#{named}"), ["vigia:decision"])
    gh.add_issue("Buy the domain", "Time: 10 min\nDue: 2026-10-27\nOrigin: plan\n", ["vigia:human-task"])
    gh.add_issue("Broken", "no shape at all", ["vigia:decision"])
    repo = mesa.state([ctx])["repos"][0]
    good, broken = repo["decisions"]
    assert good["number"] == d and good["rec"] == "C" and not good["malformed"] and good["answered"] is None
    assert broken["malformed"] and broken["question"] == "Broken"
    assert good["blocks"] == [named]  # the issue's own Blocks: line, as text
    task = repo["human_tasks"][0]
    assert (task["time"], task["due"], task["origin"]) == ("10 min", "2026-10-27", "plan")


def _a_decision_and_the_ticket_it_names(gh, url=True):
    """A ready ticket whose body says `Blocked by: #99`, and an open decision whose `Blocks:` names that ticket."""
    ticket = gh.add_issue("Wait for it", TICKET + "\nBlocked by: #99\n", ["vigia:ready", "tier:T0"])
    d = gh.add_issue("Pick a path", BODY.replace("#38, #39", f"#{ticket}"), ["vigia:decision"])
    if url:
        gh.issues_[d]["url"] = f"https://github.com/{gh.repo}/issues/{d}"
    return ticket, d


def test_the_page_never_offers_a_command_vigia_does_not_read(world, tmp_path):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    _, d = _a_decision_and_the_ticket_it_names(gh)
    decide = f"decide/{gh.repo}/{d}"
    # nothing in this vigia reads `/vigia decide`, so nothing on the page may offer it, clipboard texts included
    assert "vigia decide" not in mesa.PAGE.read_text(encoding="utf-8")
    shown = _render(tmp_path, mesa.state([ctx]), routes=("turn", decide, f"{decide}!open-talk", "feed"))
    for route in ("turn", decide, f"{decide}!open-talk", "feed"):
        for view in _views(shown, route):
            assert "vigia decide" not in view["html"], route
            for claim in ("next tick", "unblocks", "copies the answer", "waits for this answer"):
                assert claim not in view["text"], (route, claim)
    for turn, view, talk in zip(
        _views(shown, "turn"), _views(shown, decide), _views(shown, f"{decide}!open-talk"), strict=True
    ):
        assert "Should engine sessions keep a GitHub login?" in turn["text"]
        assert "Why C" in view["text"] and "◆ Recommended" in view["text"]
        assert "vigia does not read the answer yet" in view["text"]
        assert "Nothing is decided until you answer on the issue." in talk["text"]
        assert "When we agree, I answer on the issue." in talk["text"]  # the brief itself


def test_blocked_by_and_blocks_lines_hold_nothing_back(world, tmp_path):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    ticket, d = _a_decision_and_the_ticket_it_names(gh)
    repo = mesa.state([ctx])["repos"][0]
    assert [q["number"] for q in repo["queue"]] == [ticket] and "blocked_by" not in repo["queue"][0]
    assert repo["decisions"][0]["blocks"] == [ticket]
    decide = f"decide/{gh.repo}/{d}"
    shown = _render(tmp_path, mesa.state([ctx]), routes=("turn", "factory", decide))
    held = ("waits on", "waiting on a decision", "decision #", "blocks 1 ticket", "will not build")
    for route in ("turn", "factory", decide):
        for view in _views(shown, route):
            for claim in held:
                assert claim not in view["text"], (route, claim)
    for turn, factory in zip(_views(shown, "turn"), _views(shown, "factory"), strict=True):
        assert "1 free · 1 ticket ready" in factory["text"] and "○ ready" in factory["text"]
        # the decision is yours to answer, but it does not stop the factory: Only you, quoting the issue's own line
        assert "Blocking the factory" not in turn["text"] and "Only you · 1" in turn["text"]
        assert f"the issue says it blocks #{ticket}" in turn["text"]
    assert "○ ready · T0" in shown["desk"][decide]["text"]  # the ticket the decision names, as it really is
    # and that is what vigia does: the builder takes the ticket regardless of either line
    assert builder.next_issue(ctx)["number"] == ticket


def test_the_answer_is_a_link_to_the_decision_issue_on_github(world, tmp_path):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    _, d = _a_decision_and_the_ticket_it_names(gh)
    _, bare = _a_decision_and_the_ticket_it_names(gh, url=False)  # GitHub did not say the issue's url
    issue = f"https://github.com/{gh.repo}/issues/"
    for n in (d, bare):
        shown = _render(tmp_path, mesa.state([ctx]), routes=(f"decide/{gh.repo}/{n}",))
        for view in _views(shown, f"decide/{gh.repo}/{n}"):
            answer = re.search(r'<a ([^>]*data-k="answer-gh"[^>]*)>(.*?)</a>', view["html"])
            assert answer and "Answer on GitHub" in answer.group(2)
            assert f'href="{issue}{n}#new_comment_field"' in answer.group(1)
            assert 'target="_blank"' in answer.group(1)


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
    # newest first: no later review came back clean, so neither round's P1 is confirmed fixed
    assert [f["status"] for v in verdicts for f in v["findings"]] == ["open", "open"]
    assert d["engine_line"].startswith("Engine: claude")
    assert d["tried_total"]["gate_rounds"] == 2 and d["built_by"].startswith("claude")


def test_a_finding_is_fixed_only_once_a_later_review_has_looked_again(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([CHANGES])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    for _ in range(2):  # build, gate P1 on the first head
        tick(ctx)
    pr = next(iter(gh.prs_))

    def statuses():
        d = mesa.pr_detail(ctx, pr)
        return [f["status"] for e in d["timeline"] if e["event"] == "verdict" for f in e["findings"]]

    assert statuses() == ["open"]
    # a person pushes a new head without touching the affected code: nobody has verified it yet
    ctx.ledger.append("pushed", pr=pr, sha="b" * 40, from_sha=gh.prs_[pr]["head"])
    assert statuses() == ["open"]
    # a review with no usable verdict looked at nothing
    ctx.ledger.append(
        "verdict", pr=pr, kind="gate", sha="b" * 40, tier="T0", verdict="UNAVAILABLE", findings=[]
    )
    assert statuses() == ["open"]
    # a review of another kind did not re-check the gate's finding either
    ctx.ledger.append(
        "verdict", pr=pr, kind="review", sha="b" * 40, tier="T0", verdict="APPROVE", findings=[]
    )
    assert statuses() == ["open"]
    ctx.ledger.append("verdict", pr=pr, kind="gate", sha="b" * 40, tier="T0", verdict="APPROVE", findings=[])
    assert statuses() == ["fixed"]


def test_another_review_that_still_blocks_does_not_resolve_an_earlier_finding(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([CHANGES])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    for _ in range(2):  # build, gate P1 on the first head
        tick(ctx)
    pr = next(iter(gh.prs_))
    first = gh.prs_[pr]["head"]
    other = [{"severity": "P1", "file": "other.py", "line": 9, "title": "A different blocker"}]
    ctx.ledger.append("pushed", pr=pr, sha="b" * 40, from_sha=first)
    ctx.ledger.append(
        "verdict", pr=pr, kind="gate", sha="b" * 40, tier="T0", verdict="CHANGES", p1=1, findings=other
    )

    def shown():
        d = mesa.pr_detail(ctx, pr)
        return [
            (f["title"], f["status"]) for e in d["timeline"] if e["event"] == "verdict" for f in e["findings"]
        ]

    assert shown()[0] == ("A different blocker", "open") and shown()[1][1] == "open"
    # a clean review on head b confirms the first head's P1, but not the P1 that was found on b itself
    ctx.ledger.append(
        "verdict", pr=pr, kind="gate", sha="b" * 40, tier="T0", verdict="CHANGES", p1=0, findings=[]
    )
    assert [st for _, st in shown()] == ["open", "fixed"]
    # P2-only findings are not blockers: a review on a newer head that reports only those confirms the fixes
    ctx.ledger.append("pushed", pr=pr, sha="c" * 40, from_sha="b" * 40)
    ctx.ledger.append(
        "verdict",
        pr=pr,
        kind="gate",
        sha="c" * 40,
        tier="T0",
        verdict="CHANGES",
        p1=0,
        findings=[{"severity": "P2", "file": "x.py", "line": 1, "title": "Naming"}],
    )
    assert [st for t, st in shown() if t != "Naming"] == ["fixed", "fixed"]


def test_an_open_pr_screen_follows_the_watcher(world, tmp_path):  # noqa: F811
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    gh, build = world
    ctx, _ = build([CHANGES])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    for _ in range(2):
        tick(ctx)
    pr = next(iter(gh.prs_))
    state = mesa.state([ctx], _now(ctx))
    first = mesa.pr_detail(ctx, pr)
    # the watcher moves on while the screen is open: a new head, a new reason
    second = dict(first, head="f" * 40, reason="the watcher pushed a fix and is waiting for a new review")
    assert second["reason"] not in json.dumps(first)
    live = tmp_path / "live.json"
    live.write_text(json.dumps({"state": [state], "pr": [first, second]}), encoding="utf-8")
    script = Path(__file__).with_name("mesa_render.js")
    out = subprocess.run(
        [node, str(script), "--live", str(mesa.PAGE), str(live), f"pr/{gh.repo}/{pr}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    shown = json.loads(out.stdout)
    assert first["reason"] in shown["before"]["text"] and second["reason"] not in shown["before"]["text"]
    assert second["reason"] in shown["after"]["text"] and first["reason"] not in shown["after"]["text"]


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


def test_a_failed_refresh_keeps_the_detail_but_says_so_and_stops_offering_the_old_head(world, tmp_path):  # noqa: F811
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    gh, build = world
    ctx, _ = build([APPROVE, APPROVE])
    gh.add_issue("Touch billing", TICKET, ["vigia:ready", "tier:T2"])
    for _ in range(4):
        tick(ctx)
    pr = next(iter(gh.prs_))
    first = mesa.state([ctx], _now(ctx))
    assert _pr(first, pr)["waiting_for"] == "human"
    detail = mesa.pr_detail(ctx, pr)
    # the watcher moves to a new head while the screen is open, and the detail endpoint then fails
    moved = json.loads(json.dumps(first))
    _pr(moved, pr)["head"] = "f" * 40
    live = tmp_path / "live.json"
    live.write_text(
        json.dumps(
            {"state": [first, moved], "pr": [detail, {"__status": 502, "error": "GitHub answered 502."}]}
        ),
        encoding="utf-8",
    )
    script = Path(__file__).with_name("mesa_render.js")
    out = subprocess.run(
        [node, str(script), "--live", str(mesa.PAGE), str(live), f"pr/{gh.repo}/{pr}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    shown = json.loads(out.stdout)
    assert (
        "Approve this head" in shown["before"]["text"]
        and "last detail the Mesa could read" not in shown["before"]["text"]
    )
    after = shown["after"]["text"]
    assert "the head has moved since" in after and "GitHub answered 502." in after
    assert "fffffff" in after and detail["head"][:7] in after
    assert "Approve this head" not in after and "Approve on GitHub" not in after


def _render_live(node, tmp_path, live, gh, pr):
    path = tmp_path / "live.json"
    path.write_text(json.dumps(live), encoding="utf-8")
    script = Path(__file__).with_name("mesa_render.js")
    out = subprocess.run(
        [node, str(script), "--live", str(mesa.PAGE), str(path), f"pr/{gh.repo}/{pr}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_an_approval_is_withdrawn_when_the_state_stops_waiting_for_a_person(world, tmp_path):  # noqa: F811
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    gh, build = world
    ctx, _ = build([APPROVE, APPROVE])
    gh.add_issue("Touch billing", TICKET, ["vigia:ready", "tier:T2"])
    for _ in range(4):
        tick(ctx)
    pr = next(iter(gh.prs_))
    first = mesa.state([ctx], _now(ctx))
    assert _pr(first, pr)["waiting_for"] == "human"
    detail = mesa.pr_detail(ctx, pr)
    fail = {"__status": 502, "error": "GitHub answered 502."}
    # the same head, now parked and no longer waiting for a label, and the detail refresh fails
    parked = json.loads(json.dumps(first))
    _pr(parked, pr).update(waiting_for=None, parked=True, next="wait")
    # the PR is gone from the state's list
    gone = json.loads(json.dumps(first))
    gone["repos"][0]["prs"] = [p for p in gone["repos"][0]["prs"] if p["number"] != pr]
    for later in (parked, gone):
        shown = _render_live(node, tmp_path, {"state": [first, later], "pr": [detail, fail]}, gh, pr)
        assert "Approve this head" in shown["before"]["text"]
        after = shown["after"]["text"]
        assert "Approve this head" not in after and "Approve on GitHub" not in after


def test_a_detail_whose_head_moved_never_offers_the_old_head_while_it_refreshes(world, tmp_path):  # noqa: F811
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    gh, build = world
    ctx, _ = build([APPROVE, APPROVE])
    gh.add_issue("Touch billing", TICKET, ["vigia:ready", "tier:T2"])
    for _ in range(4):
        tick(ctx)
    pr = next(iter(gh.prs_))
    first = mesa.state([ctx], _now(ctx))
    detail = mesa.pr_detail(ctx, pr)
    moved = json.loads(json.dumps(first))
    _pr(moved, pr)["head"] = "f" * 40
    live = tmp_path / "live.json"  # the state moves to a new head, and the detail's refresh never answers
    live.write_text(json.dumps({"state": [first, moved], "pr": [detail, {"__hang": True}]}), encoding="utf-8")
    script = Path(__file__).with_name("mesa_render.js")
    out = subprocess.run(
        [node, str(script), "--live", str(mesa.PAGE), str(live), f"pr/{gh.repo}/{pr}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    after = json.loads(out.stdout)["after"]["text"]
    assert "the head has moved since" in after and "fffffff" in after
    assert "Approve this head" not in after and "Approve on GitHub" not in after


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _unreadable_github(gh):
    """Make every GitHub read fail the way an expired login or a rate limit does; returns the list of attempts."""
    attempts = []

    def failing(*args, **kwargs):
        attempts.append(1)
        raise CommandError(["gh", "pr", "list"], 1, "", "HTTP 403: API rate limit exceeded")

    gh.prs = failing
    return attempts


def test_a_failing_github_is_asked_less_and_less_often_and_a_good_answer_resets_it(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    clock = _Clock()
    cache = mesa._Cache([ctx], 15, clock=clock)

    def body_at(moment):
        clock.now = moment
        return json.loads(cache.get())

    healthy, reads_ok = gh.prs, []

    def counting(*args, **kwargs):
        reads_ok.append(1)
        return healthy(*args, **kwargs)

    gh.prs = counting
    first = body_at(0)
    assert "error" not in first["repos"][0] and len(reads_ok) == 1
    assert body_at(14) == first and len(reads_ok) == 1  # the page polls every 15 s: served from the cache
    second = body_at(15)
    assert len(reads_ok) == 2  # ... and GitHub is read again after it

    attempts = _unreadable_github(gh)
    failed = body_at(30)  # 15 s after the last read
    assert "rate limit" in failed["repos"][0]["error"] and len(attempts) == 1
    assert failed["repos"][0]["prs"] is None  # still unknown, never empty
    assert failed["repos"][0]["last_ok"] == second["generated"]
    # each failed refresh doubles the wait: 30 s, 60 s, 120 s, 240 s, then 300 s at most
    waits, last, reads = [], 30.0, 1
    for _ in range(7):
        probe = last
        while True:
            probe += 1
            body_at(probe)
            if len(attempts) > reads:
                break
        waits.append(probe - last)
        last, reads = probe, len(attempts)
    assert waits == [30, 60, 120, 240, 300, 300, 300]
    # GitHub answers again: the next attempt shows it, and the page is back to 15 s
    gh.prs = counting
    reads_ok.clear()
    clock.now = last + 299  # still inside the wait: the old failure is served, GitHub is not asked
    assert "error" in json.loads(cache.get())["repos"][0] and not reads_ok
    clock.now = last + 300
    recovered = json.loads(cache.get())
    assert "error" not in recovered["repos"][0] and recovered["repos"][0]["prs"] is not None
    assert cache.last_ok[gh.repo] == recovered["generated"] and len(reads_ok) == 1
    clock.now += 14
    assert json.loads(cache.get()) == recovered and len(reads_ok) == 1
    clock.now += 1
    json.loads(cache.get())
    assert len(reads_ok) == 2  # back to a read every 15 s


def test_the_backoff_counts_from_when_a_slow_refresh_finished_not_from_when_it_started(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    clock = _Clock()
    cache = mesa._Cache([ctx], 15, clock=clock)
    attempts = []

    def slow_failure(*args, **kwargs):
        attempts.append(1)
        clock.now += 45  # GitHub takes 45 s to say no
        raise CommandError(["gh", "pr", "list"], 1, "", "HTTP 502: bad gateway")

    gh.prs = slow_failure
    assert "bad gateway" in json.loads(cache.get())["repos"][0]["error"] and clock.now == 45
    clock.now = 45 + 29  # the 30 s backoff started when the answer arrived, so it has not run out
    cache.get()
    assert len(attempts) == 1
    clock.now = 45 + 30
    cache.get()
    assert len(attempts) == 2
    # a refresh that raises an unexpected error is timed the same way
    broken = []

    def slow_crash(*args, **kwargs):
        broken.append(1)
        clock.now += 400  # longer than the longest backoff
        raise RuntimeError("boom")

    fresh = mesa._Cache([ctx], 15, clock=clock)
    fresh.entries.clear()
    mesa_state, mesa.state = mesa.state, slow_crash
    try:
        start = clock.now
        with pytest.raises(RuntimeError):
            fresh.get()
        clock.now = start + 400 + 29
        with pytest.raises(RuntimeError):
            fresh.get()
        assert len(broken) == 1
        clock.now = start + 400 + 30
        with pytest.raises(RuntimeError):
            fresh.get()
        assert len(broken) == 2
    finally:
        mesa.state = mesa_state


def test_a_partial_read_failure_backs_off_and_does_not_count_as_a_good_read(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    clock = _Clock()
    cache = mesa._Cache([ctx], 15, clock=clock)
    first = json.loads(cache.get())
    assert not first["repos"][0]["unread"]
    healthy, attempts = gh.issues, []

    def human_tasks_unreadable(label):
        if label == "vigia:human-task":
            attempts.append(1)
            raise CommandError(["gh", "issue", "list"], 1, "", "HTTP 403: API rate limit exceeded")
        return healthy(label)

    gh.issues = human_tasks_unreadable
    clock.now = 15
    partial = json.loads(cache.get())["repos"][0]
    assert "error" not in partial and partial["human_tasks"] is None and partial["unread"] == ["human tasks"]
    assert partial["last_ok"] == first["generated"]  # the failed read did not move the last good one
    assert cache.last_ok[gh.repo] == first["generated"]
    # the page polls every 15 s: inside the 30 s backoff GitHub is not asked again
    for moment in (16, 30, 44):
        clock.now = moment
        cache.get()
    assert len(attempts) == 1
    clock.now = 45
    cache.get()
    assert len(attempts) == 2
    # once every list reads again, the cache is back to 15 s and the last good read moves
    gh.issues = healthy
    clock.now = 45 + 60
    recovered = json.loads(cache.get())
    assert cache.last_ok[gh.repo] == recovered["generated"] and not recovered["repos"][0]["unread"]


def test_an_unreadable_repo_says_when_it_was_last_read_ok(world, tmp_path):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    clock = _Clock()
    cache = mesa._Cache([ctx], 15, clock=clock)
    first = json.loads(cache.get())
    _unreadable_github(gh)
    clock.now = 15
    failed = json.loads(cache.get())
    assert failed["repos"][0]["last_ok"] == first["generated"]
    shown = _render(tmp_path, failed, routes=("turn", "factory"))
    stamp = mesa.datetime.fromisoformat(first["generated"].replace("Z", "+00:00")).astimezone()
    for view in (*_views(shown, "turn"), *_views(shown, "factory")):
        assert "Last read OK at" in view["text"] and f"{stamp:%H:%M}" in view["text"]
    # a Mesa that never read GitHub OK says so instead of inventing a time
    never = mesa._Cache([ctx], 15, clock=clock)
    clock.now = 0
    assert json.loads(never.get())["repos"][0]["last_ok"] is None
    never_shown = _render(tmp_path, json.loads(never.get()), routes=("turn",))
    assert "has not read GitHub OK since it started" in never_shown["desk"]["turn"]["text"]


def test_the_backoff_stays_capped_after_a_thousand_failed_refreshes(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    clock = _Clock()
    cache = mesa._Cache([ctx], 15, clock=clock)
    attempts = _unreadable_github(gh)
    json.loads(cache.get())
    cache.entries["state"].fails = 1100  # 30 * 2 ** 1099 overflows a float
    assert cache._wait(cache.entries["state"]) == mesa.BACKOFF_MAX
    clock.now = mesa.BACKOFF_MAX
    assert "error" in json.loads(cache.get())["repos"][0]  # still answers, and asks GitHub again
    assert len(attempts) == 2


def test_a_partial_failure_says_when_it_was_last_read_ok(world, tmp_path):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    clock = _Clock()
    cache = mesa._Cache([ctx], 15, clock=clock)
    first = json.loads(cache.get())
    healthy = gh.issues

    def human_tasks_unreadable(label):
        if label == "vigia:human-task":
            raise CommandError(["gh", "issue", "list"], 1, "", "HTTP 403: API rate limit exceeded")
        return healthy(label)

    gh.issues = human_tasks_unreadable
    clock.now = 15
    partial = json.loads(cache.get())
    assert partial["repos"][0]["unread"] == ["human tasks"] and "error" not in partial["repos"][0]
    shown = _render(tmp_path, partial, routes=("turn",))
    stamp = mesa.datetime.fromisoformat(first["generated"].replace("Z", "+00:00")).astimezone()
    for view in _views(shown, "turn"):
        assert "Could not read: human tasks" in view["text"]
        assert "Last read OK at" in view["text"] and f"{stamp:%H:%M}" in view["text"]
        assert "asks it less often" in view["text"]


def test_an_unexpected_error_in_the_server_answers_500_with_the_error_and_backs_off(world, monkeypatch):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    calls = []

    def broken(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("the state broke: boom")

    monkeypatch.setattr(mesa, "state", broken)
    srv = _serve(ctx)
    try:
        status, kind, body = _get(srv, "/api/state")
        assert status == 500 and kind == "application/json; charset=utf-8"
        assert "boom" in json.loads(body)["error"]
        status, _, body = _get(srv, "/api/state")  # inside the wait: the same error, without building again
        assert status == 500 and "boom" in json.loads(body)["error"] and len(calls) == 1
        assert _get(srv, "/")[0] == 200  # the page is still served
    finally:
        srv.shutdown()


def _poll_after(tmp_path, gh, answers):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    path = tmp_path / "live.json"
    path.write_text(json.dumps({"state": answers, "pr": [{}]}), encoding="utf-8")
    script = Path(__file__).with_name("mesa_render.js")
    out = subprocess.run(
        [node, str(script), "--live", str(mesa.PAGE), str(path), "turn"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_page_says_the_server_failed_apart_from_not_reaching_it(world, tmp_path):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    good = mesa.state([ctx])
    failed = _poll_after(tmp_path, gh, [good, {"__status": 500, "error": "the state broke: boom"}])["after"][
        "text"
    ]
    assert "The Mesa server failed" in failed and "the state broke: boom" in failed
    assert "cannot reach vigia" not in failed and "Last good answer at" in failed
    # a proxy's 502 (the Mesa is not there) is still "cannot reach"
    gone = _poll_after(tmp_path, gh, [good, {"__status": 502, "error": "bad gateway"}])["after"]["text"]
    assert "The Mesa cannot reach vigia" in gone and "server failed" not in gone


def test_a_proposed_ticket_shows_with_its_tier_even_past_the_first_hundred(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    n = gh.add_issue("feat: planned thing", TICKET, ["tier:T1", "vigia:proposed"])
    gh.add_issue("feat: not proposed", TICKET, ["tier:T0", "vigia:ready"])
    gh.issues = lambda label: []  # what `gh issue list --limit 100` returns for a ticket on a later page
    repo = mesa.state([ctx])["repos"][0]
    assert [(p["number"], p["title"], p["tier"]) for p in repo["proposed"]] == [
        (n, "feat: planned thing", "T1")
    ]
    assert "proposed tickets" not in repo["unread"]


def test_proposed_tickets_that_cannot_be_read_are_unknown_not_none(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    gh.add_issue("feat: planned thing", TICKET, ["tier:T1", "vigia:proposed"])

    def down():
        raise CommandError(["gh", "api"], 1, "", "HTTP 502")

    gh.open_issues = down
    repo = mesa.state([ctx])["repos"][0]
    assert repo["proposed"] is None and "proposed tickets" in repo["unread"]


def _until(condition, seconds=10.0):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def _slow_github(gh):
    """Make the PR list wait until the test lets it answer, the way a cold GitHub takes seconds; returns
    (release, calls)."""
    release, calls, healthy = threading.Event(), [], gh.prs

    def slow(label):
        calls.append(1)
        assert release.wait(10), "the test never let GitHub answer"
        return healthy(label)

    gh.prs = slow
    return release, calls


def test_a_mesa_that_has_not_read_github_yet_answers_at_once_with_what_this_machine_knows(world, tmp_path):  # noqa: F811
    gh, build = world
    ctx, _ = build([APPROVE])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    for _ in range(3):  # build, gate, merge
        tick(ctx)
    assert ctx.ledger.last("merged")
    release, calls = _slow_github(gh)
    cache = mesa._Cache([ctx], 15, spawn=mesa._thread)
    try:
        started = time.monotonic()
        first = json.loads(cache.get())
        again = json.loads(cache.get())
        assert time.monotonic() - started < 1  # GitHub has not answered, and nobody waited for it
        repo = first["repos"][0]
        assert repo["pending"] is True and "error" not in repo
        for name in ("prs", "queue", "parked_issues", "refused", "human_tasks", "proposed", "decisions"):
            assert repo[name] is None, name  # not read yet: unknown, never empty
        assert any(e["event"] == "merged" for e in first["feed"])  # the ledger already knows what happened
        assert repo["labels"]["ready"] == "vigia:ready" and first["health"]["last_tick"]
        assert again["repos"][0]["pending"] and len(calls) == 1  # one refresh at a time
        # the page shows it as being read: never "nothing needs you", never "could not read"
        shown, short = _render(tmp_path, first, routes=("turn", "factory", "decide")), gh.repo.split("/")[-1]
        for view in (*_views(shown, "turn"), *_views(shown, "factory"), *_views(shown, "decide")):
            assert (
                f"Reading GitHub for {short}" in view["text"] or "Reading its pull requests" in view["text"]
            )
            for wrong in (
                "Nothing needs you",
                "Could not read",
                "cannot read",
                "No open decisions",
                "PRs unknown",
                "queue unknown",
                "Decisions unknown",
                "nothing ready to build",
            ):
                assert wrong not in view["text"], wrong
            assert "col-empty c-danger" not in view["html"]  # no board column says "unknown"
        for view in _views(shown, "factory"):
            assert (
                "Reading its pull requests from GitHub" in view["text"]
                and "reading the queue" in view["text"]
            )
        release.set()
        assert cache.built.wait(10)
        read = json.loads(cache.get())["repos"][0]
        assert "pending" not in read and read["prs"] == [] and read["queue"] == []
        # the page asks again soon while it reads, and then shows what GitHub said
        after = _poll_after(tmp_path, gh, [first, json.loads(cache.get())])
        assert "Reading GitHub for" in after["before"]["text"]
        assert (
            "Reading GitHub" not in after["after"]["text"] and "Nothing needs you" in after["after"]["text"]
        )
    finally:
        release.set()


def test_a_request_never_waits_for_a_refresh_or_for_a_pr_detail(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx)
    pr = next(iter(gh.prs_))
    clock = _Clock()
    cache = mesa._Cache([ctx], 15, clock=clock, spawn=mesa._thread)
    cache.refresh()
    assert cache.built.wait(10)
    first = cache.get()
    assert cache.entries["state"].at == 0
    release, calls = _slow_github(gh)
    healthy_pr, detail_asked = gh.pr, threading.Event()

    def slow_pr(n):
        detail_asked.set()
        assert release.wait(10)
        return healthy_pr(n)

    gh.pr = slow_pr
    try:
        clock.now = 15  # the state is due a refresh: it starts, and this answer is the one before it
        started = time.monotonic()
        assert cache.get() == first
        _until(lambda: calls)  # the refresh is reading GitHub
        assert cache.get() == first and len(calls) == 1  # it is running, so no second one starts
        # a PR detail being read (its reader waits) holds up nobody else
        reader = threading.Thread(target=cache.pr, args=(gh.repo, pr), daemon=True)
        reader.start()
        assert detail_asked.wait(10)
        assert cache.get() == first
        assert time.monotonic() - started < 1
        later = gh.add_issue("Add another helper", TICKET, ["vigia:ready", "tier:T0"])  # read after the PRs
        release.set()
        reader.join(10)
        assert not reader.is_alive()
        _until(lambda: not cache.refreshing)
        assert cache.entries["state"].at == 15  # the refreshed state, read at 15 s
        assert [q["number"] for q in json.loads(cache.get())["repos"][0]["queue"]] == [later]
    finally:
        release.set()


def test_the_first_request_shows_a_github_that_answers_at_once(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    srv = _serve(ctx)
    try:
        status, _, body = _get(srv, "/api/state")
        assert status == 200 and "pending" not in json.loads(body)["repos"][0]  # no flash of "Reading GitHub"
    finally:
        srv.shutdown()
