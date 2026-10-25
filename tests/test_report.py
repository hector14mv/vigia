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


def test_the_header_says_last_day_for_a_one_day_window_and_last_n_days_otherwise(tmp_path):
    from vigia.ledger import Ledger

    data = report.build({"a/b": Ledger(tmp_path / "l.jsonl")})
    one = report.render({**data, "days": 1}).splitlines()[0]
    seven = report.render({**data, "days": 7}).splitlines()[0]
    assert one == "vigia report · last day · a/b"
    assert seven == "vigia report · last 7 days · a/b"


def test_a_merge_in_the_window_is_credited_to_a_builder_that_opened_the_pr_before_it(tmp_path):
    from datetime import UTC, datetime

    from vigia.ledger import Ledger

    stamps = iter(["2026-10-16T10:00:00Z", "2026-10-16T11:00:00Z", "2026-10-24T10:00:00Z"])
    ledger = Ledger(tmp_path / "l.jsonl", clock=lambda: next(stamps))
    ledger.append("pr_opened", pr=5, issue=1, engine="claude", model="claude-sonnet-5-5", tier="T0")
    ledger.append("verdict", pr=5, kind="gate", sha="a" * 40, verdict="APPROVE", engine="codex", model="m")
    ledger.append("merged", pr=5, sha="a" * 40, tier="T0")
    data = report.build({"a/b": ledger}, days=7, now=datetime(2026, 10, 25, tzinfo=UTC))
    assert data["totals"]["merged"] == 1
    (b,) = data["builders"]
    assert (b["opened"], b["merged"], b["gate_rounds_per_merge"]) == (0, 1, 1.0)
    assert b["median_minutes_to_merge"] == 8 * 24 * 60
