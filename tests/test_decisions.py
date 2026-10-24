"""Decision issues: the fixed body shape the Mesa reads, and the lines a ticket uses to say what blocks it."""

from vigia import decisions

BODY = """\
Question: Should engine sessions keep a GitHub login?
Blocks: #38, #39
Due: 2026-10-26
Origin: decision block in the T2 review of #38

### A. Keep closing paths
Each path found is closed one by one.
### B. Isolate the session
A separate OS user with no login.
### C. Ship it as a guard, and say so
Document the limit in SECURITY.md.

Recommendation: C — it ships Monday and the limit is written down
"""


def test_a_decision_body_reads_into_its_parts():
    d = decisions.parse(BODY)
    assert d["question"] == "Should engine sessions keep a GitHub login?"
    assert [o["key"] for o in d["options"]] == ["A", "B", "C"]
    assert d["options"][1] == {
        "key": "B",
        "title": "Isolate the session",
        "body": "A separate OS user with no login.",
    }
    assert (
        d["options"][2]["body"] == "Document the limit in SECURITY.md."
    )  # the recommendation is not an option's text
    assert (d["rec"], d["rec_why"]) == ("C", "it ships Monday and the limit is written down")
    assert d["blocks"] == [38, 39] and d["due"] == "2026-10-26" and d["origin"].startswith("decision block")


def test_a_body_without_a_question_or_two_options_is_not_a_decision():
    assert decisions.parse("### A. one\n### B. two\n") is None
    assert decisions.parse("Question: which?\n### A. only one\n") is None


def test_a_recommendation_must_name_one_of_the_options():
    d = decisions.parse(BODY.replace("Recommendation: C", "Recommendation: Z"))
    assert d["rec"] is None


def test_blocked_by_reads_the_numbers_of_a_ticket_line():
    assert decisions.blocked_by("## Outcome\nx\nBlocked by: #45 and #46\n") == [45, 46]
    assert decisions.blocked_by("nothing here") == []
