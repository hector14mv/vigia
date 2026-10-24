"""The Mesa: what it shows for each situation, and a server that only reads and only answers its own host."""

import http.client
import json
import shutil
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from fakes import APPROVE, CHANGES, engine
from test_e2e import TICKET, world  # noqa: F401  (world is a fixture)
from vigia import jobs, mesa
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


def _render(tmp_path, state):
    """What the page shows for `state`, by running its own script under Node with a minimal DOM."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    data = tmp_path / "state.json"
    data.write_text(json.dumps(state), encoding="utf-8")
    script = Path(__file__).with_name("mesa_render.js")
    out = subprocess.run(
        [node, str(script), str(mesa.PAGE), str(data)], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def test_a_repo_that_cannot_be_read_is_shown_as_unknown_not_as_empty(world, tmp_path):  # noqa: F811
    gh, build = world
    ctx, _ = build([])

    def expired(label):
        raise CommandError(["gh", "pr", "list"], 1, "", "HTTP 401: Bad credentials")

    gh.prs = expired
    repo = mesa.state([ctx])["repos"][0]
    assert "Bad credentials" in repo["error"]
    assert (
        repo["prs"] is None and repo["queue"] is None and repo["parked_issues"] is None
    )  # unknown, not zero
    shown = _render(tmp_path, mesa.state([ctx]))
    assert "Nothing needs you" not in shown["turn"] and "Cannot read" in shown["turn"]
    assert "No open pull requests" not in shown["factory"] and "unknown" in shown["factory"]


def test_a_ticket_parked_before_it_had_a_pr_needs_you(world, tmp_path):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    ctx.engines._run = engine([], edits=lambda cwd, k: None)
    n = gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx)  # the builder leaves no changes: the issue is parked, no PR is opened
    s = mesa.state([ctx])
    assert s["repos"][0]["parked_issues"] == [
        {"number": n, "title": "Add a helper", "url": "", "reason": "the builder left no changes"}
    ]
    assert s["feed"][0]["event"] == "build_failed" and "left no changes" in s["feed"][0]["text"]
    shown = _render(tmp_path, s)
    assert "Nothing needs you" not in shown["turn"]
    assert f"#{n} Add a helper" in shown["turn"] and "the builder left no changes" in shown["turn"]


def test_a_workspace_counts_its_shared_limit_not_the_sum_of_its_repos(world, tmp_path):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    s = mesa.state([ctx], slots=2)
    assert s["slots"] == 2
    two = dict(
        s["repos"][0], slots=1, jobs=[{"target": "pr-1", "number": 1, "issue": False, "action": "gate"}] * 2
    )
    other = dict(s["repos"][0], repo="acme/other", slots=1, jobs=[])
    assert "sessions <b>2/2</b>" in _render(tmp_path, dict(s, repos=[two, other]))["pulse"]
    alone = dict(s, slots=None, repos=[two, other])  # no workspace: each repo's own watch.slots adds up
    assert "sessions <b>2/3</b>" in _render(tmp_path, alone)["pulse"]


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
    shown = _render(tmp_path, mesa.state([ctx]))
    assert "Nothing needs you" not in shown["turn"] and "Could not read: human tasks" in shown["turn"]


def test_a_refused_ticket_stays_until_its_issue_resolves_it_whatever_its_age(world, tmp_path):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    n = gh.add_issue("Too vague", "## Outcome\nmake it better\n", ["tier:T1"])
    ctx.ledger.append("refused", issue=n, gaps=["no Scope section"])
    later = datetime.fromisoformat(ctx.ledger.rows()[-1]["ts"].replace("Z", "+00:00")) + timedelta(days=30)
    repo = mesa.state([ctx], later)["repos"][0]
    assert [i["number"] for i in repo["refused"]] == [n]  # a month later, still there
    gh.issues_[n]["state"] = "CLOSED"
    assert mesa.state([ctx], later)["repos"][0]["refused"] == []  # gone once the issue resolves it


def test_refused_tickets_past_the_check_limit_are_counted_not_dropped(world, tmp_path):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    for k in range(mesa.REFUSED_CHECKS + 2):
        n = gh.add_issue(f"Vague {k}", "x", ["tier:T1"])
        ctx.ledger.append("refused", issue=n, gaps=["no Outcome section"])
    repo = mesa.state([ctx])["repos"][0]
    assert len(repo["refused"]) == mesa.REFUSED_CHECKS and repo["refused_unchecked"] == 2
    assert "2 older refused tickets not checked" in _render(tmp_path, mesa.state([ctx]))["turn"]
