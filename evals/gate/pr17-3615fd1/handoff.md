## Handoff 3615fd135c96b0115d8f66f4e3cd19c5975f8e6e
Engine: claude · claude-sonnet-5-5 · effort high · builder · lab anthropic
Ticket: #9 · tier T1
Session: 83 s · exit 0
Tests run by vigia: `uv run --quiet --with pytest --with ruff sh -c 'ruff check . && ruff format --check . && pytest -q'` → passed — ..........                                                               [100%]
Files changed: `README.md`, `src/vigia/gh.py`, `tests/test_gh.py`

## Summary
- `src/vigia/gh.py`: `GitHub.prs(label)` now asks `gh pr list --label <label> --json number` only for the PR numbers, sorts them, and returns `self.pr(n)` for each, so head, labels, mergeable and CI come from `gh pr view` (the same call `GitHub.pr()` makes). The dict shape is unchanged. `rules.py`, `watch.py` and `_pr()` are untouched.
- `tests/test_gh.py`: new `RoutedRunner` fake that answers by subcommand. Three tests: `pr list` and `pr view` disagree on the CI rollup (list says in progress, view says complete) and `prs()` returns the view's `passing`; several PRs listed out of order come back sorted by number, none skipped, and equal to `pr(n)`; an empty list makes only one `gh` call.
- `README.md`: removed the "Limitations of v0.1" bullet that documented the `gh pr list` lag (issue #9), since it is no longer true.
## Tests run
- `uv run --quiet --with pytest --with ruff sh -c 'ruff check . && ruff format --check . && pytest -q'`: ruff check passed, 36 files already formatted, all pytest tests passed (the first run failed on a ruff SIM300 yoda-assertion in my new test; fixed and re-run clean).
## Deviations
- Edited `README.md` (not named in the ticket's Scope) only to delete the limitation line this fix makes false.
- `prs()` does not re-filter the `gh pr view` results by label or state: membership is decided by `gh pr list`, as the ticket says ("takes the PR numbers from `gh pr list`").
## Risks
- One extra `gh pr view` call per open labelled PR per tick (as the ticket accepts). A PR that is closed or loses the label between the list and the view calls is still returned, carrying its fresh `state`/`labels`; `watch.py` is unchanged, so check that it copes (merging still re-reads with `GitHub.pr()` and `--match-head-commit`).
- If `gh pr view` fails for one PR, `prs()` raises `CommandError` for the whole call (previously one call could not fail per PR).
## Follow-ups
- none
