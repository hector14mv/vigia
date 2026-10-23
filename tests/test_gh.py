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
