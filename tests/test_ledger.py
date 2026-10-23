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


def test_append_after_a_torn_tail_keeps_the_new_event(tmp_path):
    lg = ledger(tmp_path)
    lg.append("a")
    with lg.path.open("ab") as fh:
        fh.write(b'{"event":"interrupted"')
    lg.append("verdict", pr=1, kind="gate", verdict="CHANGES", p1=1)
    assert [r["event"] for r in lg.rows()] == ["a", "verdict"]
    assert lg.last("verdict", pr=1)["p1"] == 1


def test_a_write_cut_inside_a_multibyte_character_is_skipped(tmp_path):
    lg = ledger(tmp_path)
    lg.append("a")
    with lg.path.open("ab") as fh:
        fh.write(b'{"event":"torn","reason":"\xc3')
    assert [r["event"] for r in lg.rows()] == ["a"]
    assert lg.events("torn") == []
    assert lg.last("a")["event"] == "a"
    lg.append("b", text="café")
    assert [r["event"] for r in lg.rows()] == ["a", "b"]
    assert lg.last("b")["text"] == "café"
