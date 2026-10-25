"""The GitHub adapter against a fake runner: no `gh` binary, no network."""

import json

import pytest

from vigia.gh import GitHub, ci_state
from vigia.proc import CommandError, Result


class Runner:
    """Records every call and answers from a queue of canned results (or exceptions)."""

    def __init__(self, *replies: str | Exception):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def __call__(self, args, *, cwd=None, input=None, **kwargs):
        self.calls.append({"args": list(args), "cwd": cwd, "input": input})
        reply = self.replies.pop(0) if self.replies else ""
        if isinstance(reply, Exception):
            raise reply
        return Result(0, reply, "")


def check_run(status="COMPLETED", conclusion="SUCCESS"):
    return {"__typename": "CheckRun", "status": status, "conclusion": conclusion}


def status_context(state):
    return {"__typename": "StatusContext", "state": state}


# -- ci_state --


@pytest.mark.parametrize("rollup", [None, []])
def test_ci_state_none_without_checks(rollup):
    assert ci_state(rollup) == "none"


def test_ci_state_passing_when_all_runs_finished_green():
    rollup = [check_run(conclusion=c) for c in ("SUCCESS", "NEUTRAL", "SKIPPED")]
    assert ci_state(rollup) == "passing"


def test_ci_state_pending_while_a_run_is_in_progress():
    assert ci_state([check_run(), check_run(status="IN_PROGRESS", conclusion="")]) == "pending"


def test_ci_state_failing_on_a_failed_run():
    assert ci_state([check_run(), check_run(conclusion="FAILURE")]) == "failing"


def test_ci_state_status_context_pending_and_error():
    assert ci_state([status_context("PENDING")]) == "pending"
    assert ci_state([status_context("ERROR")]) == "failing"
    assert ci_state([status_context("SUCCESS")]) == "passing"


@pytest.mark.parametrize("order", [1, -1])
def test_ci_state_failure_wins_over_pending_in_any_order(order):
    rollup = [check_run(status="IN_PROGRESS", conclusion=""), check_run(conclusion="FAILURE")][::order]
    assert ci_state(rollup) == "failing"


# -- merge --


def test_merge_pins_the_head_commit_and_never_deletes_a_branch():
    runner = Runner()
    GitHub("acme/app", runner=runner).merge(7, "abc123")
    assert [c["args"] for c in runner.calls] == [
        ["gh", "pr", "merge", "7", "-R", "acme/app", "--squash", "--match-head-commit", "abc123"]
    ]
    assert "--delete-branch" not in runner.calls[0]["args"]
    assert "-d" not in runner.calls[0]["args"]


# -- pr --


def test_pr_turns_the_gh_payload_into_vigia_dict():
    payload = {
        "number": 12,
        "title": "feat: x",
        "body": None,
        "url": "https://github.com/acme/app/pull/12",
        "state": "OPEN",
        "isDraft": True,
        "headRefOid": "deadbeef",
        "headRefName": "vigia/12",
        "baseRefName": "main",
        "labels": [{"name": "vigia:ready"}, {"name": "T0"}, {"name": "b"}],
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [check_run(status="IN_PROGRESS", conclusion="")],
    }
    runner = Runner(json.dumps(payload))
    pr = GitHub("acme/app", runner=runner).pr(12)
    assert pr["head"] == "deadbeef"
    assert pr["branch"] == "vigia/12"
    assert pr["labels"] == ["T0", "b", "vigia:ready"]
    assert pr["ci"] == "pending"
    assert pr["mergeable"] == "MERGEABLE"
    assert pr["draft"] is True
    assert pr["body"] == ""
    args = runner.calls[0]["args"]
    assert args[:6] == ["gh", "pr", "view", "12", "-R", "acme/app"]
    assert "--json" in args


def test_pr_defaults_when_gh_omits_fields():
    pr = GitHub("acme/app", runner=Runner(json.dumps({"number": 3}))).pr(3)
    assert pr["draft"] is False
    assert pr["labels"] == []
    assert pr["mergeable"] == "UNKNOWN"
    assert pr["ci"] == "none"


# -- prs --


class RoutedRunner:
    """Answers by gh subcommand, so a test can make `pr list` and `pr view` disagree."""

    def __init__(self, listing: list[dict], views: dict[int, dict]):
        self.listing = listing
        self.views = views
        self.calls: list[list[str]] = []

    def __call__(self, args, *, cwd=None, input=None, **kwargs):
        self.calls.append(list(args))
        if args[:3] == ["gh", "pr", "list"]:
            return Result(0, json.dumps(self.listing), "")
        if args[:3] == ["gh", "pr", "view"]:
            return Result(0, json.dumps(self.views[int(args[3])]), "")
        raise AssertionError(f"unexpected call {args}")


def test_prs_reads_ci_from_pr_view_when_pr_list_lags():
    # `pr list` still says the run is going; `pr view` already has the result.
    stale = {"number": 2, "statusCheckRollup": [check_run(status="IN_PROGRESS", conclusion="")]}
    fresh = {"number": 2, "statusCheckRollup": [check_run()], "labels": [{"name": "vigia:ready"}]}
    runner = RoutedRunner([stale], {2: fresh})
    [pr] = GitHub("acme/app", runner=runner).prs("vigia:ready")
    assert pr["ci"] == "passing"
    assert pr["labels"] == ["vigia:ready"]
    list_args = next(c for c in runner.calls if c[:3] == ["gh", "pr", "list"])
    assert "--label" in list_args
    assert "statusCheckRollup" not in list_args[list_args.index("--json") + 1]
    view_args = next(c for c in runner.calls if c[:3] == ["gh", "pr", "view"])
    assert view_args[:6] == ["gh", "pr", "view", "2", "-R", "acme/app"]


def test_prs_returns_every_listed_pr_sorted_by_number_in_the_pr_dict_shape():
    views = {
        n: {"number": n, "headRefOid": f"sha{n}", "labels": [{"name": "vigia:ready"}]} for n in (9, 3, 7)
    }
    runner = RoutedRunner([{"number": 9}, {"number": 3}, {"number": 7}], views)
    gh = GitHub("acme/app", runner=runner)
    result = gh.prs("vigia:ready")
    assert [p["number"] for p in result] == [3, 7, 9]
    assert result == [gh.pr(3), gh.pr(7), gh.pr(9)]


def test_prs_drops_a_listed_pr_whose_fresh_labels_no_longer_carry_the_label():
    views = {
        2: {"number": 2, "labels": [{"name": "T0"}]},  # label removed after the listing
        3: {"number": 3, "labels": [{"name": "vigia:ready"}]},
    }
    runner = RoutedRunner([{"number": 2}, {"number": 3}], views)
    assert [p["number"] for p in GitHub("acme/app", runner=runner).prs("vigia:ready")] == [3]


def test_prs_drops_a_listed_pr_that_is_no_longer_open():
    views = {
        2: {"number": 2, "state": "MERGED", "labels": [{"name": "vigia:ready"}]},
        3: {"number": 3, "state": "OPEN", "labels": [{"name": "vigia:ready"}]},
    }
    runner = RoutedRunner([{"number": 2}, {"number": 3}], views)
    assert [p["number"] for p in GitHub("acme/app", runner=runner).prs("vigia:ready")] == [3]


def test_prs_is_empty_when_nothing_carries_the_label():
    runner = RoutedRunner([], {})
    assert GitHub("acme/app", runner=runner).prs("vigia:ready") == []
    assert len(runner.calls) == 1


# -- remove_label --


def test_remove_label_url_encodes_the_label():
    runner = Runner()
    GitHub("acme/app", runner=runner).remove_label(5, "vigia:ready")
    args = runner.calls[0]["args"]
    assert args[:4] == ["gh", "api", "--method", "DELETE"]
    assert args[4] == "repos/acme/app/issues/5/labels/vigia%3Aready"


def test_remove_label_ignores_a_404():
    err = CommandError(["gh", "api"], 1, "", "gh: Not Found (HTTP 404)")
    GitHub("acme/app", runner=Runner(err)).remove_label(5, "gone")


def test_remove_label_reraises_other_errors():
    err = CommandError(["gh", "api"], 1, "", "gh: Server Error (HTTP 500)")
    with pytest.raises(CommandError):
        GitHub("acme/app", runner=Runner(err)).remove_label(5, "vigia:ready")


# -- create_pr --


def test_create_pr_returns_the_number_from_the_printed_url():
    out = "Creating pull request for vigia/4 into main\n\nhttps://github.com/acme/app/pull/42\n"
    runner = Runner(out)
    number = GitHub("acme/app", runner=runner).create_pr("vigia/4", "main", "feat: t", "the body")
    assert number == 42
    call = runner.calls[0]
    assert call["input"] == "the body"
    assert call["args"][:4] == ["gh", "pr", "create", "-R"]
    assert "vigia/4" in call["args"]
    assert "main" in call["args"]


def test_open_issues_reads_every_page_and_leaves_out_pull_requests():
    pages = [
        [{"number": 1, "title": "a", "html_url": "u1", "labels": [{"name": "tier:T1"}]}],
        [{"number": 2, "title": "pr", "html_url": "u2", "labels": [], "pull_request": {}}],
    ]
    run = Runner(json.dumps(pages))
    issues = GitHub("o/r", runner=run).open_issues()
    assert issues == [{"number": 1, "title": "a", "url": "u1", "labels": ["tier:T1"], "created": None}]
    assert "--paginate" in run.calls[0]["args"] and "--slurp" in run.calls[0]["args"]


def test_issues_by_reads_every_page_in_any_state_and_leaves_out_pull_requests():
    pages = [
        [{"number": 1, "title": "a", "body": "x", "html_url": "u1", "state": "closed"}],
        [{"number": 2, "title": "pr", "body": "", "html_url": "u2", "pull_request": {}}],
    ]
    run = Runner(json.dumps(pages))
    issues = GitHub("o/r", runner=run).issues_by("vigia-bot", "2026-10-25T10:00:00Z")
    assert issues == [{"number": 1, "title": "a", "body": "x", "url": "u1"}]
    args = " ".join(run.calls[0]["args"])
    assert "state=all" in args and "creator=vigia-bot" in args and "since=2026-10-25T10:00:00Z" in args
    assert "--paginate" in run.calls[0]["args"]


def test_merged_at_reads_githubs_own_time_and_none_when_not_merged():
    run = Runner("2026-10-25T10:00:00Z\n", "\n")
    gh = GitHub("o/r", runner=run)
    assert gh.merged_at(7) == "2026-10-25T10:00:00Z" and gh.merged_at(8) is None
    assert run.calls[0]["args"][-4:] == ["--json", "mergedAt", "-q", ".mergedAt"]


def test_issues_by_without_a_lower_bound_asks_for_every_issue():
    run = Runner("[]")
    GitHub("o/r", runner=run).issues_by("vigia-bot", None)
    assert "since=" not in " ".join(run.calls[0]["args"])


# -- ticket / permission: what decides whether a ready issue may be built --


def graphql_issue(issue):
    return json.dumps({"data": {"repository": {"issue": issue}}})


def labeled(at, login="owner", kind="User", name="vigia:ready"):
    return {
        "__typename": "LabeledEvent",
        "id": f"LE_{at}",
        "createdAt": f"2026-10-24T{at}:00Z",
        "label": {"name": name} if name else None,
        "actor": {"__typename": kind, "login": login} if login else None,
    }


def timeline_page(nodes, before=None, edited=None, labels=()):
    return graphql_issue(
        {
            "lastEditedAt": edited,
            "labels": {"nodes": [{"name": name} for name in labels]},
            "timelineItems": {
                "pageInfo": {"hasPreviousPage": before is not None, "startCursor": before},
                "nodes": nodes,
            },
        }
    )


def test_ticket_reads_only_the_text():
    runner = Runner(graphql_issue({"title": "feat: x", "body": None}))
    assert GitHub("acme/app", runner=runner).ticket(7) == {"title": "feat: x", "body": ""}
    [call] = runner.calls
    args = call["args"]
    assert args[:3] == ["gh", "api", "graphql"]
    assert "owner=acme" in args and "name=app" in args and "number=7" in args


def test_ticket_of_something_that_is_not_an_issue_is_an_error():
    runner = Runner(json.dumps({"data": {"repository": {"issue": None}}}))
    with pytest.raises(CommandError):
        GitHub("acme/app", runner=runner).ticket(7)


def test_approval_is_the_latest_ready_event_and_what_came_after_it():
    nodes = [
        labeled("09:00"),
        {"__typename": "RenamedTitleEvent", "createdAt": "2026-10-24T09:10:00Z"},
        labeled("09:20", login="helper", kind="Bot"),
        {"__typename": "RenamedTitleEvent", "createdAt": "2026-10-24T09:30:00Z"},
        labeled("09:40", name="tier:T0"),
        labeled("09:50", login=None, name=None),
    ]
    runner = Runner(timeline_page(nodes, edited="2026-10-24T10:00:00Z", labels=("tier:T0", "vigia:ready")))
    seen = GitHub("acme/app", runner=runner).approval(7, "vigia:ready")
    assert seen == {
        "ready": {"id": "LE_09:20", "actor": "helper", "user": False, "ts": "2026-10-24T09:20:00Z"},
        "renamed": True,
        "edited": "2026-10-24T10:00:00Z",
        "present": True,
    }
    [call] = runner.calls
    query = next(a for a in call["args"] if a.startswith("query="))
    assert "lastEditedAt" in query and "LABELED_EVENT" in query and "RENAMED_TITLE_EVENT" in query
    assert "labels(first: 100)" in query
    assert not any(a.startswith("before=") for a in call["args"])


def test_approval_pages_back_past_other_label_events_to_the_ready_one():
    churn = [labeled(f"10:{m:02d}", name="needs-info") for m in range(50)]
    runner = Runner(
        timeline_page(churn, before="C2", edited=None, labels=("vigia:ready",)),  # the newest: no ready event
        timeline_page(churn, before="C1"),  # nor the one before it
        timeline_page([labeled("09:00"), *churn[:3]], before="C0"),  # here it is
    )
    seen = GitHub("acme/app", runner=runner).approval(7, "vigia:ready")
    assert seen == {
        "ready": {"id": "LE_09:00", "actor": "owner", "user": True, "ts": "2026-10-24T09:00:00Z"},
        "renamed": False,
        "edited": None,
        "present": True,  # read from the newest page, whatever the older ones hold
    }
    assert [next((a for a in c["args"] if a.startswith("before=")), None) for c in runner.calls] == [
        None,
        "before=C2",
        "before=C1",
    ]


def test_approval_without_a_ready_event_stops_at_the_oldest_page():
    runner = Runner(timeline_page([labeled("09:00", name="tier:T0")]))
    seen = GitHub("acme/app", runner=runner).approval(7, "vigia:ready")
    assert seen["ready"] is None and len(runner.calls) == 1


def test_approval_of_something_that_is_not_an_issue_is_an_error():
    with pytest.raises(CommandError):
        GitHub("acme/app", runner=Runner(graphql_issue(None))).approval(7, "vigia:ready")


def test_permission_reads_the_collaborator_permission_endpoint():
    runner = Runner(json.dumps({"permission": "read", "role_name": "triage"}))
    assert GitHub("acme/app", runner=runner).permission("someone") == "read"
    assert (
        runner.calls[0]["args"][:4]
        == [
            "gh",
            "api",
            "--method",
            "GET",
        ]
        and runner.calls[0]["args"][4] == "repos/acme/app/collaborators/someone/permission"
    )
    assert GitHub("acme/app", runner=Runner("{}")).permission("someone") == "none"
