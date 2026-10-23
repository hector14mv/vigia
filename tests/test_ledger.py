from itertools import count

from vigia.ledger import Ledger


def ledger(tmp_path) -> Ledger:
    ticks = count()
    return Ledger(tmp_path / ".vigia" / "ledger.jsonl", clock=lambda: f"2026-10-24T10:00:{next(ticks):02d}Z")


def test_append_creates_and_reads_back(tmp_path):
    lg = ledger(tmp_path)
    lg.append("pr_opened", pr=3, sha="a" * 40)
    lg.append("verdict", pr=3, kind="gate", verdict="APPROVE")
    rows = lg.rows()
    assert [r["event"] for r in rows] == ["pr_opened", "verdict"]
    assert rows[0]["ts"] == "2026-10-24T10:00:00Z"


def test_appending_never_rewrites_earlier_lines(tmp_path):
    lg = ledger(tmp_path)
    lg.append("a", n=1)
    before = lg.path.read_bytes()
    lg.append("b", text="line one\nline two")  # newlines inside values stay escaped on one line
    after = lg.path.read_bytes()
    assert after.startswith(before)
    assert after.count(b"\n") == 2


def test_torn_and_foreign_lines_are_skipped(tmp_path):
    lg = ledger(tmp_path)
    lg.append("a")
    with lg.path.open("a") as fh:
        fh.write('{"half": \n["not", "a", "row"]\n\n')
    lg.append("b")
    assert [r["event"] for r in lg.rows()] == ["a", "b"]


def test_events_and_last_filter_by_fields(tmp_path):
    lg = ledger(tmp_path)
    lg.append("verdict", pr=1, kind="gate", sha="x")
    lg.append("verdict", pr=2, kind="gate", sha="y")
    lg.append("verdict", pr=1, kind="review", sha="x")
    assert len(lg.events("verdict", pr=1)) == 2
    assert lg.last("verdict", pr=1)["kind"] == "review"
    assert lg.last("merged") is None


def test_missing_file_reads_empty(tmp_path):
    assert ledger(tmp_path).rows() == []
