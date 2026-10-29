## Handoff d920b3200255d4e94591f9e8861877a26c2f4d5c
Engine: claude · claude-sonnet-5-5 · effort high · builder · lab anthropic
Ticket: #27 · tier T1
Session: 89 s · exit 0
Tests run by vigia: `uv run --quiet --with pytest --with ruff sh -c 'ruff check . && ruff format --check . && pytest -q'` → passed — .........................                                                [100%]
Files changed: `src/vigia/tickets.py`, `tests/test_tickets.py`

## Summary
- `src/vigia/tickets.py`: new `_fence_lines()` scans line by line for fences of 3+ backticks or tildes, closed by the same character at least as long. `sections()` uses it to ignore `## Heading` lines inside fences (replacing the backtick-only regex); `_filled()` uses it to decide what counts as content.
- A one-line fence (three backticks or three tildes around `pytest -q` on one line) keeps its inner text as content; marker lines with a language tag and empty fences (backtick or tilde) still count as empty.
- `tests/test_tickets.py`: tests for one-line fences, tilde-fenced commands, empty tilde fences, headers inside tilde fences, and mismatched/shorter closing fences.
## Tests run
- `uv run --quiet --with pytest --with ruff sh -c 'ruff check . && ruff format --check . && pytest -q'` — ruff clean, 39 files formatted, all pytest tests pass (169).
- Ran the new tests against the original `tickets.py`: 4 of the new tests fail there, all pass with the fix.
## Deviations
- none
## Risks
- Fence semantics now follow CommonMark more closely (closer must match character and length). A backtick line whose info string contains a backtick but is not a one-line fence (e.g. "```a`b") is treated as plain text.
- An unclosed fence still hides nothing (same as before); its marker line is not counted as content.
- `_fence_lines` computes offsets quadratically in line count; ticket bodies are small, so I left it simple.
## Follow-ups
- none
