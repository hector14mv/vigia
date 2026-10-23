"""The Mesa: what it shows for each situation, and a server that only reads and only answers its own host."""

import http.client
import json
import threading

from fakes import APPROVE, CHANGES
from test_e2e import TICKET, world  # noqa: F401  (world is a fixture)
from vigia import jobs, mesa
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
