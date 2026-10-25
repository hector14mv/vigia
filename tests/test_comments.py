"""Reading a handoff's `## Follow-ups` section."""

from vigia import comments

SHA = "a" * 40


def handoff(follow_ups: str) -> str:
    return f"## Handoff {SHA}\nEngine: claude · x\n\n## Risks\n- r\n\n{follow_ups}"


def test_bullets_are_read_and_none_is_skipped_in_any_case():
    body = handoff("## Follow-ups\n- first thing\n* second thing\n- None\n- none.\n- NONE\n")
    assert comments.follow_ups(body) == ["first thing", "second thing"]


def test_wrapped_lines_join_their_bullet_and_the_section_ends_at_the_next_heading():
    body = handoff("## Follow-ups\n- first\n  continues here\n- second\n\n## Other\n- not a follow-up\n")
    assert comments.follow_ups(body) == ["first continues here", "second"]


def test_a_bullet_outside_the_section_is_not_a_follow_up():
    assert comments.follow_ups("## Risks\n- r\n## Follow-ups\n") == []
    assert comments.follow_ups("- stray bullet\n") == []


def test_indented_markdown_bullets_count():
    body = "## Follow-ups\n  - first task\n   * second task\n"
    assert comments.follow_ups(body) == ["first task", "second task"]


def test_handoffs_keeps_only_the_login_s_handoff_comments():
    rows = [
        {"author": "bot", "body": f"## Handoff {SHA}\nx"},
        {"author": "mallory", "body": f"## Handoff {SHA}\ny"},
        {"author": "bot", "body": "just a comment"},
    ]
    assert comments.handoffs(rows, "bot") == [f"## Handoff {SHA}\nx"]


def test_title_is_cut_to_70_at_a_word_boundary():
    assert comments.follow_up_title("short") == "short"
    exact = "x" * 70
    assert comments.follow_up_title(exact) == exact
    assert comments.follow_up_title(exact + " tail") == exact  # the cut lands on a boundary
    cut = comments.follow_up_title("word " * 20)
    assert len(cut) <= 70 and cut == "word " * 13 + "word"
    assert comments.follow_up_title("y" * 100) == "y" * 70  # no space to cut at


def test_the_issue_body_quotes_links_and_asks_for_a_rewrite():
    title, body = comments.follow_up_issue("fix this and that", 7, "https://x/pull/7", "b" * 40)
    assert title == "fix this and that"
    assert "> fix this and that" in body and "https://x/pull/7" in body and "b" * 40 in body
    assert "ticket template" in body and "`vigia:ready`" in body
