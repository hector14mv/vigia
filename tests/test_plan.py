"""The planner: a goal becomes tickets; launchable T0/T1 go to the queue, T2 and gaps wait for a person."""

import json

import pytest

from fakes import engine
from test_e2e import TICKET, world  # noqa: F401  (world is a fixture)
from vigia import plan
from vigia.config import Config, Labels, default_tiers
from vigia.proc import Result


def _plan(**extra):
    body = {
        "tickets": [
            {
                "title": "test: cover shout() on empty input",
                "tier": "T0",
                "depends_on_earlier": False,
                "body": TICKET,
            },
            {"title": "feat: charge a card", "tier": "T2", "depends_on_earlier": False, "body": TICKET},
            {
                "title": "feat: vague idea",
                "tier": "T1",
                "depends_on_earlier": False,
                "body": "## Outcome\nmake it better\n",
            },
        ],
        "human_tasks": [{"title": "Create the Stripe account", "body": "Sign up at stripe.com."}],
    }
    body.update(extra)
    return body


def test_parse_keeps_valid_entries_and_drops_the_rest():
    text = (
        "```json\n"
        + json.dumps(
            {
                "tickets": [
                    {"title": "a", "tier": "T9", "body": "x"},
                    {"title": "b", "tier": "T1", "body": "y"},
                ],
                "human_tasks": [{"title": ""}, {"title": "c", "body": "d"}],
            }
        )
        + "\n```"
    )
    parsed = plan.parse(text)
    assert [t["title"] for t in parsed["tickets"]] == ["b"] and [
        h["title"] for h in parsed["human_tasks"]
    ] == ["c"]
    assert plan.parse("no plan today") is None


def test_filing_sends_only_launchable_t0_t1_to_the_queue(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    filed = plan.file_plan(ctx, plan.parse(json.dumps(_plan())), "add tests")
    labels = {f["title"]: set(f["labels"]) for f in filed}
    assert labels["test: cover shout() on empty input"] == {"tier:T0", "vigia:ready"}
    assert labels["feat: charge a card"] == {"tier:T2", "vigia:proposed"}  # T2 waits for a person
    assert labels["feat: vague idea"] == {"tier:T1", "vigia:proposed"}  # the template check found gaps
    assert labels["Create the Stripe account"] == {"vigia:human-task"}
    vague = next(i for i in gh.issues_.values() if i["title"] == "feat: vague idea")
    assert "template check found gaps" in vague["body"]
    assert len(ctx.ledger.events("planned")) == 4


def test_a_repo_from_an_older_init_gets_the_planner_labels_before_anything_is_filed(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    gh.known_labels = {"vigia:ready", "tier:T0", "tier:T1", "tier:T2"}  # what an older `vigia init` created
    filed = plan.file_plan(ctx, plan.parse(json.dumps(_plan())), "g")
    assert len(filed) == 4 and {"vigia:proposed", "vigia:human-task"} <= gh.known_labels


def test_propose_only_files_nothing_as_ready(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    filed = plan.file_plan(ctx, plan.parse(json.dumps(_plan())), "g", auto_ready=False)
    assert not any("vigia:ready" in f["labels"] for f in filed)


def test_a_planner_session_reads_the_base_branch_and_files_the_plan(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    gh.add_issue("an open issue already", TICKET, [])
    run = engine([_plan()])
    ctx.engines._run = run
    result, filed = plan.plan(ctx, "Cover the helpers with tests", limit=2)
    assert len(result["tickets"]) == 2 and len(filed) == 3  # two tickets (limit) and the human task
    prompt = run.calls[-1]["prompt"]
    assert "Cover the helpers with tests" in prompt and "textkit.py" in prompt
    assert "an open issue already" in prompt and "## Outcome" in prompt
    assert run.calls[-1]["role"] == "reviewer"  # read-only sandbox
    assert not any(ctx.root.iterdir())


def test_dry_run_files_nothing(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    ctx.engines._run = engine([_plan()])
    before = len(gh.issues_)
    result, filed = plan.plan(ctx, "anything", dry_run=True)
    assert filed == [] and len(gh.issues_) == before and result["tickets"]


def test_the_template_header_never_lands_in_a_ticket():
    from pathlib import Path

    header = '---\nname: vigia ticket\nabout: x\nlabels: ["vigia:ready"]\n---\n\n'
    tickets = [{"title": "t", "tier": "T0", "body": header + TICKET}]
    parsed = plan.parse(json.dumps({"tickets": tickets, "human_tasks": []}))
    assert parsed["tickets"][0]["body"].startswith("## Outcome")
    assert "name: vigia ticket" not in plan._skill(Path("/nonexistent"))


def test_a_ticket_that_needs_an_earlier_one_merged_waits_for_a_person(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    planned = [
        {"title": "feat: add a helper", "tier": "T1", "depends_on_earlier": False, "body": TICKET},
        {"title": "refactor: migrate callers", "tier": "T1", "depends_on_earlier": True, "body": TICKET},
        {"title": "docs: unrelated note", "tier": "T0", "depends_on_earlier": False, "body": TICKET},
        {"title": "test: no word on order", "tier": "T0", "body": TICKET},  # silence counts as dependent
    ]
    filed = plan.file_plan(ctx, plan.parse(json.dumps({"tickets": planned, "human_tasks": []})), "g")
    labels = {f["title"]: set(f["labels"]) for f in filed}
    assert labels["feat: add a helper"] == {"tier:T1", "vigia:ready"}
    assert labels["refactor: migrate callers"] == {"tier:T1", "vigia:proposed"}
    assert labels["docs: unrelated note"] == {"tier:T0", "vigia:ready"}
    assert labels["test: no word on order"] == {"tier:T0", "vigia:proposed"}
    helper = filed[0]["number"]
    migrate = next(i for i in gh.issues_.values() if i["title"] == "refactor: migrate callers")
    assert f"Planned to follow #{helper}" in migrate["body"]
    assert [i["title"] for i in gh.issues("vigia:ready")] == ["feat: add a helper", "docs: unrelated note"]


def test_the_first_ticket_never_waits_on_an_earlier_one():
    planned = [{"title": "a", "tier": "T0", "depends_on_earlier": True, "body": TICKET}]
    assert plan.parse(json.dumps({"tickets": planned, "human_tasks": []}))["tickets"][0]["after"] is False


@pytest.mark.parametrize("ready", ["vigia:proposed", "Vigia:Proposed", "vigia:human-task", "tier:T2"])
def test_a_ready_label_that_is_a_planner_or_tier_label_files_nothing(world, ready):  # noqa: F811
    gh, build = world
    cfg = Config(repo=gh.repo, base="main", tiers=default_tiers("claude"), labels=Labels(ready=ready))
    ctx, _ = build([], cfg=cfg)
    before = len(gh.issues_)
    with pytest.raises(SystemExit, match="reserved"):
        plan.file_plan(ctx, plan.parse(json.dumps(_plan())), "g")
    assert len(gh.issues_) == before


def test_a_failed_planner_session_files_nothing_even_with_a_plan_in_its_output(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([])

    def hung(cmd, cwd, input, timeout, log, env=None, keep_fds=()):
        out = json.dumps({"is_error": False, "result": json.dumps(_plan())})
        return Result(-15, out, "", timed_out=True)

    ctx.engines._run = hung
    before = len(gh.issues_)
    with pytest.raises(SystemExit, match="no plan"):
        plan.plan(ctx, "anything")
    assert len(gh.issues_) == before
    (failed,) = ctx.ledger.events("plan_failed")
    assert failed["timed_out"] is True and failed["code"] == -15


def test_a_ticket_after_a_dropped_one_still_waits_for_it(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    planned = [
        {"title": "feat: add the helper", "tier": "T1", "depends_on_earlier": False, "body": " "},  # dropped
        {
            "title": "refactor: callers use the helper",
            "tier": "T1",
            "depends_on_earlier": True,
            "body": TICKET,
        },
    ]
    parsed = plan.parse(json.dumps({"tickets": planned, "human_tasks": []}))
    assert [t["title"] for t in parsed["tickets"]] == ["refactor: callers use the helper"]
    assert parsed["tickets"][0]["after"] is True  # its prerequisite was never filed
    filed = plan.file_plan(ctx, parsed, "g")
    assert "vigia:ready" not in filed[0]["labels"]
    assert "returned malformed" in gh.issues_[filed[0]["number"]]["body"]


def test_a_planned_ticket_already_open_is_not_filed_again(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    gh.add_issue("test: cover shout() on empty input", TICKET, ["tier:T0"])
    before = len(gh.issues_)
    plan.file_plan(ctx, plan.parse(json.dumps(_plan())), "g")
    titles = [i["title"] for i in gh.issues_.values()]
    assert titles.count("test: cover shout() on empty input") == 1 and len(gh.issues_) == before + 3
    assert ctx.ledger.events("plan_skipped")[0]["reason"] == "an open issue has this title"
