"""Answering a decision (`/vigia decide B`) and holding back the tickets that wait on one or on another ticket."""

from dataclasses import replace

from fakes import APPROVE
from test_decisions import BODY
from test_e2e import TICKET, world  # noqa: F401  (world is a fixture)
from vigia import decisions, jobs
from vigia.proc import CommandError
from vigia.watch import tick


def _say(gh, n, author, body):
    gh.thread.setdefault(n, []).append({"author": author, "body": body, "ts": ""})


def _decided(ctx):
    return ctx.ledger.events("decided")


def test_the_answer_is_the_first_valid_command_of_an_entitled_author():
    thread = [
        {"author": "stranger", "body": "/vigia decide A"},
        {"author": "owner", "body": "thinking about it, no command here"},
        {"author": "owner", "body": "/vigia decide Z"},  # not an option
        {"author": "owner", "body": "see:\n/vigia decide c  because it ships Monday\n"},
        {"author": "owner", "body": "/vigia decide A"},  # too late: the first valid one stands
    ]
    found = decisions.answer(thread, {"A", "B", "C"}, {"owner"})
    assert found == {"key": "C", "by": "owner", "reason": "because it ships Monday"}
    assert decisions.answer(thread, {"A", "B", "C"}, {"nobody"}) is None
    assert (
        decisions.answer([{"author": "owner", "body": "run /vigia decide B now"}], {"B"}, {"owner"}) is None
    )
    assert decisions.option_keys(BODY) == {"A", "B", "C"}


def test_a_ticket_waits_for_the_decision_that_blocks_it_and_builds_once_it_is_answered(world):  # noqa: F811
    gh, build = world
    ctx, run = build([APPROVE])
    ticket = gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    d = gh.add_issue("Pick a path", BODY.replace("#38, #39", f"#{ticket}"), ["vigia:decision"])

    tick(ctx)
    assert gh.prs_ == {} and run.calls == []  # unanswered: nothing builds
    assert not jobs.still_due(ctx, "build", ticket)[0]

    _say(gh, d, "stranger", "/vigia decide B")  # anyone else's comment counts for nothing
    _say(gh, d, "owner", "/vigia decide Z")  # and so does a letter that is not an option
    tick(ctx)
    assert gh.prs_ == {} and _decided(ctx) == [] and gh.issues_[d]["state"] == "OPEN"

    _say(gh, d, "owner", "/vigia decide B only one login for now")
    tick(ctx)
    row = _decided(ctx)[0]
    assert (row["issue"], row["key"], row["by"], row["reason"]) == (d, "B", "owner", "only one login for now")
    assert gh.issues_[d]["state"] == "CLOSED"
    assert any("option B" in b for b in gh.bodies(ticket))  # the blocked ticket learns the answer
    assert len(gh.prs_) == 1  # unblocked in the same tick
    tick(ctx)
    assert len(_decided(ctx)) == 1  # recorded once


def test_only_the_configured_humans_may_answer(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    ctx.cfg = replace(ctx.cfg, humans=("alice",))
    ticket = gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    d = gh.add_issue("Pick a path", BODY.replace("#38, #39", f"#{ticket}"), ["vigia:decision"])
    _say(gh, d, "owner", "/vigia decide A")  # vigia's own login is not in checks.humans
    tick(ctx)
    assert _decided(ctx) == [] and gh.prs_ == {}
    _say(gh, d, "alice", "/vigia decide A")
    tick(ctx)
    assert _decided(ctx)[0]["by"] == "alice" and len(gh.prs_) == 1


def test_a_malformed_decision_still_blocks_what_it_names(world):  # noqa: F811
    gh, build = world
    ctx, run = build([APPROVE])
    ticket = gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    gh.add_issue("Broken", f"Blocks: #{ticket}\nno options at all", ["vigia:decision"])
    tick(ctx)
    assert gh.prs_ == {} and run.calls == []


def test_a_ticket_waits_for_the_open_tickets_in_its_blocked_by_line(world):  # noqa: F811
    gh, build = world
    ctx, run = build([APPROVE])
    first = gh.add_issue("Groundwork", "not ready yet", ["tier:T0"])
    second = gh.add_issue("Add a helper", TICKET + f"\nBlocked by: #{first}\n", ["vigia:ready", "tier:T0"])
    tick(ctx)
    assert gh.prs_ == {} and run.calls == []
    assert not jobs.still_due(ctx, "build", second)[0]
    gh.issues_[first]["state"] = "CLOSED"
    assert jobs.still_due(ctx, "build", second)[0]
    tick(ctx)
    assert len(gh.prs_) == 1


def test_a_ticket_is_held_when_what_blocks_it_cannot_be_read(world, monkeypatch):  # noqa: F811
    gh, build = world
    ctx, run = build([APPROVE])
    first = gh.add_issue("Groundwork", "not ready yet", ["tier:T0"])
    second = gh.add_issue("Add a helper", TICKET + f"\nBlocked by: #{first}\n", ["vigia:ready", "tier:T0"])
    gh.issues_[first]["state"] = "CLOSED"

    def down(n):
        raise CommandError(["gh", "api"], 1, "", "HTTP 502")

    monkeypatch.setattr(gh, "item_open", down)
    tick(ctx)
    assert gh.prs_ == {} and not jobs.still_due(ctx, "build", second)[0]  # unknown is not free
