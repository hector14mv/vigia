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
