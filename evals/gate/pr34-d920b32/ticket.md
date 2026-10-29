## Outcome
A ticket whose section is a fenced command (with backticks or tildes, on one line or several) counts as filled, and a `## Heading` inside any fence never starts a section.

## Scope
`src/vigia/tickets.py` (`sections`, `_filled`) and `tests/test_tickets.py`.
Not touched: the section list, the required lines, `missing()`'s messages.
Must still hold: every test in `tests/test_tickets.py` keeps passing; an empty fence (or one with only a language tag) still counts as empty; template placeholders still count as empty.

## Acceptance criteria
- [ ] `~~~` fences are treated like backtick fences when ignoring headings inside them
- [ ] a Done-test that is only a one-line fenced command, e.g. ```` ```pytest -q``` ````, counts as filled
- [ ] a Done-test that is only a tilde-fenced block with a command inside counts as filled
- [ ] line endings are normalised first: a body with `\r\n` endings (GitHub often stores issue bodies that way) parses exactly like the same body with `\n`
- [ ] an unclosed fence runs to the end of the body, as in CommonMark: no heading after an unclosed opener starts a section
- [ ] tests for each case above

## Done-test
`pytest -q tests/test_tickets.py` passes.

## Risks
Ticket parsing decides what vigia agrees to build. Facts: verified (`grep -n "\`\`\`" src/vigia/tickets.py`): fences are matched only as triple backticks (line 49), and `_filled` drops any line that starts with triple backticks (line 64), which empties a one-line fenced command. Found by the cross-lab gate on #1 (P2s).
