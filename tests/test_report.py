"""The engine table, from the ledger."""

from fakes import APPROVE, CHANGES
from test_e2e import TICKET, world  # noqa: F401  (world is a fixture)
from vigia import report
from vigia.watch import tick


def test_the_report_counts_rounds_p1s_and_merges_per_model(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([CHANGES, APPROVE, APPROVE])
    gh.add_issue("One", TICKET, ["vigia:ready", "tier:T0"])
    for _ in range(5):  # build, gate P1, fix, gate approve, merge
        tick(ctx)
    gh.add_issue("Two", TICKET, ["vigia:ready", "tier:T0"])
    for _ in range(3):  # build, gate approve, merge
        tick(ctx)
    data = report.build({gh.repo: ctx.ledger}, days=7)
    (b,) = data["builders"]
    assert b["model"] == "claude · claude-sonnet-5-5" and (b["opened"], b["merged"]) == (2, 2)
    assert b["first_round_p1_rate"] == 0.5 and b["gate_rounds_per_merge"] == 1.5
    (r,) = data["reviewers"]
    assert r["model"] == "codex · gpt-6.1-sol" and (r["verdicts"], r["p1"]) == (3, 1)
    assert data["totals"]["merged"] == 2 and data["totals"]["fix_rounds"] == 1
    text = report.render(data)
    assert "claude · claude-sonnet-5-5" in text and "50%" in text


def test_an_empty_ledger_reports_nothing_without_failing(tmp_path):
    from vigia.ledger import Ledger

    data = report.build({"a/b": Ledger(tmp_path / "l.jsonl")})
    assert data["builders"] == [] and data["totals"]["merged"] == 0
    assert "0 merged" in report.render(data)
