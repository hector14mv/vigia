"""Reading a handoff's `## Follow-ups` section."""

import pytest

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


def test_title_is_cut_to_70_at_a_word_boundary_and_ends_with_an_ellipsis():
    assert comments.follow_up_title("short") == "short"
    exact = "x" * 70
    assert comments.follow_up_title(exact) == exact
    assert comments.follow_up_title(exact + " tail") == "x" * 69 + "…"  # no space to cut at
    cut = comments.follow_up_title("word " * 20)
    assert cut == "word " * 13 + "word…"
    assert comments.follow_up_title("y" * 100) == "y" * 69 + "…"  # no space to cut at
    # the cut lands on a word boundary and trailing punctuation goes before the "…"
    assert comments.follow_up_title("aaaa, " * 12) == "aaaa, " * 10 + "aaaa…"


def test_title_is_never_longer_than_the_limit():
    for n in range(60, 130):
        for text in ("w" * n, "ab, " * n, "word-" * n):
            assert len(comments.follow_up_title(text)) <= comments.TITLE_MAX


def test_the_issue_body_quotes_links_and_asks_for_a_rewrite():
    title, body = comments.follow_up_issue("fix this and that", 7, "https://x/pull/7", "b" * 40)
    assert title == "fix this and that"
    assert "> fix this and that" in body and "https://x/pull/7" in body and "b" * 40 in body
    assert "ticket template" in body and "`vigia:ready`" in body


def _handoffs(*engine_lines: str) -> list[dict]:
    return [{"author": "bot", "body": f"## Handoff {SHA}\n{line}\n\nNotes."} for line in engine_lines]


@pytest.mark.parametrize(
    "line, lab",
    [
        ("Engine: claude · claude-opus-5-5 · effort high · builder · lab anthropic", "anthropic"),
        ("Engine: codex · gpt-6.1-sol · effort medium · builder · lab openai", "openai"),
        ("Engine: claude · x", "anthropic"),
        ("Engine: codex", "openai"),
        (
            "Engine: claude · claude-opus-5-5 · by hand · lab openai",
            "anthropic",
        ),  # trailing text is never read
        ("Engine: opencode · github-copilot/kimi-k3 · builder · lab anthropic", "moonshotai"),
        ("Engine: opencode · anthropic/claude-x · effort high · fixer · lab anthropic", "anthropic"),
        ("Engine: opencode\tgithub-copilot/gpt-6-sol", None),  # the model field follows a `·`
        ("Engine: opencode", None),  # no model
        ("Engine: opencode · builder · lab anthropic", None),  # no model
        ("Engine: opencode · kimi-k3 · builder · lab moonshotai", None),  # no provider
        ("Engine: opencode · somelab/mystery-1 · builder · lab anthropic", None),  # unknown family
        ("Engine: opencode-x · anthropic/claude-x · builder", None),
        ("Engine: gemini · google/gemini-x", None),  # not an engine vigia knows
    ],
)
def test_engine_family_reads_the_lab_from_the_engine_and_model(line, lab):
    assert comments.engine_family(_handoffs(line), "bot") == lab


def test_engine_family_takes_the_first_handoff_even_without_a_lab():
    rows = _handoffs("Engine: opencode · somelab/mystery-1 · builder", "Engine: claude · claude-opus-5-5")
    assert comments.engine_family(rows, "bot") is None
    assert comments.engine_family(_handoffs("Engine: claude · x", "Engine: codex · y"), "bot") == "anthropic"
    assert comments.engine_family([{"author": "bot", "body": f"## Handoff {SHA}\nNo engine."}], "bot") is None
