from vigia.tickets import TEMPLATE, linked_issue, missing, sections, slug, tier

GOOD = """\
## Outcome
`slugify("Héllo")` returns `"hello"`.

## Scope
`textkit/slug.py` and its tests.
Not touched: the public `truncate()` API.
Must still hold: ASCII input is returned unchanged.

## Acceptance criteria
- [ ] accents are stripped
- [ ] tests cover it

## Done-test
`python -m pytest -q` passes.

## Risks
None: pure function.
"""


def test_a_complete_ticket_is_launchable():
    assert missing(GOOD) == []


def test_the_blank_template_is_not_launchable():
    gaps = missing(TEMPLATE)
    assert any("Outcome" in g for g in gaps)
    assert any("Not touched:" in g for g in gaps)
    assert any("Must still hold:" in g for g in gaps)


def test_missing_section_and_line_are_named():
    body = GOOD.replace("## Risks\nNone: pure function.\n", "").replace(
        "Must still hold: ASCII input is returned unchanged.\n", ""
    )
    gaps = missing(body)
    assert "section `## Risks`" in gaps
    assert "`Must still hold:` line in `## Scope`" in gaps


def test_headers_inside_code_fences_do_not_count():
    body = "```\n## Outcome\nx\n```\n"
    assert "Outcome" not in sections(body)


def test_section_titles_ignore_parenthetical_notes():
    body = GOOD.replace("## Done-test", "## Done-test (what a human runs)")
    assert missing(body) == []


def test_tier_needs_exactly_one_label():
    assert tier(["tier:T1", "bug"]) == "T1"
    assert tier(["tier:T1", "tier:T2"]) is None
    assert tier(["bug"]) is None
    assert tier(["tier:T9"]) is None


def test_slug_and_linked_issue():
    assert slug("Add truncate() — with ellipsis!") == "add-truncate-with-ellipsis"
    assert slug("!!!") == "change"
    assert linked_issue("Closes #12\n\nmore") == 12
    assert linked_issue("fixes #3") == 3
    assert linked_issue("see #4") is None


def test_fenced_section_content_counts_but_fenced_headers_do_not():
    body = GOOD.replace("`python -m pytest -q` passes.", "```sh\npytest -q\n```")
    assert missing(body) == []
    assert sections(body)["Done-test"].endswith("pytest -q\n```")
    nested = GOOD.replace("`python -m pytest -q` passes.", "```md\n## Risks\nfake\n```\nrun it")
    assert sections(nested)["Risks"] == "None: pure function."


def test_an_empty_fence_is_not_content():
    body = GOOD.replace("`python -m pytest -q` passes.", "```sh\n```")
    assert "section `## Done-test` is empty" in missing(body)


def _with_done_test(content: str) -> str:
    return GOOD.replace("`python -m pytest -q` passes.", content)


def test_a_one_line_fenced_command_is_content():
    assert missing(_with_done_test("```pytest -q```")) == []
    assert missing(_with_done_test("~~~pytest -q~~~")) == []


def test_a_tilde_fenced_command_is_content():
    assert missing(_with_done_test("~~~sh\npytest -q\n~~~")) == []
    assert missing(_with_done_test("~~~\npytest -q\n~~~")) == []


def test_an_empty_tilde_fence_is_not_content():
    for fence in ("~~~sh\n~~~", "~~~\n~~~", "~~~~\n~~~~"):
        assert "section `## Done-test` is empty" in missing(_with_done_test(fence))


def test_headers_inside_tilde_fences_do_not_count():
    assert "Outcome" not in sections("~~~\n## Outcome\nx\n~~~\n")
    nested = _with_done_test("~~~md\n## Risks\nfake\n~~~\nrun it")
    assert sections(nested)["Risks"] == "None: pure function."


def test_a_fence_closes_only_on_its_own_character_and_length():
    body = _with_done_test("~~~\n```\n## Risks\nfake\n```\n~~~\nrun it")
    assert sections(body)["Risks"] == "None: pure function."
    longer = _with_done_test("````\n```\n## Risks\nfake\n```\n````\nrun it")
    assert sections(longer)["Risks"] == "None: pure function."


def test_an_unclosed_fence_runs_to_the_end_of_the_body():
    for fence in ("```", "~~~", "````"):
        assert sections(f"{fence}\n## Outcome\nx\n") == {}
        assert sections(f"{fence}\r\n## Outcome\r\nx\r\n") == {}
    body = GOOD.split("## Risks")[0] + "```sh\npytest -q\n## Risks\nfake"
    assert "Risks" not in sections(body)
    assert "section `## Risks`" in missing(body)
    assert sections(body)["Done-test"].endswith("fake")
    bare = GOOD.split("`python -m pytest -q` passes.")[0] + "~~~sh\n"
    assert "section `## Done-test` is empty" in missing(bare)


def test_crlf_endings_do_not_hide_fence_delimiters():
    assert "Outcome" not in sections("```\r\n## Outcome\r\nx\r\n```\r\n")
    assert "Outcome" not in sections("~~~\r\n## Outcome\r\nx\r\n~~~\r\n")
    crlf = GOOD.replace("\n", "\r\n")
    assert missing(crlf) == []
    nested = _with_done_test("```md\n## Risks\nfake\n```\nrun it").replace("\n", "\r\n")
    assert sections(nested)["Risks"] == "None: pure function."


def test_crlf_fenced_commands_are_content_and_empty_fences_are_not():
    assert missing(_with_done_test("~~~pytest -q~~~").replace("\n", "\r\n")) == []
    assert missing(_with_done_test("```sh\npytest -q\n```").replace("\n", "\r\n")) == []
    empty = _with_done_test("```sh\n```").replace("\n", "\r\n")
    assert "section `## Done-test` is empty" in missing(empty)
