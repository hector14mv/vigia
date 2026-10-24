"""The planner: a goal becomes tickets; launchable T0/T1 go to the queue, T2 and gaps wait for a person."""

import json

from fakes import engine
from test_e2e import TICKET, world  # noqa: F401  (world is a fixture)
from vigia import plan


def _plan(**extra):
    body = {
        "tickets": [
            {"title": "test: cover shout() on empty input", "tier": "T0", "body": TICKET},
            {"title": "feat: charge a card", "tier": "T2", "body": TICKET},
            {"title": "feat: vague idea", "tier": "T1", "body": "## Outcome\nmake it better\n"},
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
