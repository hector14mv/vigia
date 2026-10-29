## Handoff 633e1879a958d1493fcd400924a2cf8ce8bc10dd
Engine: claude · claude-sonnet-5-5 · effort high · builder · lab anthropic
Ticket: #19 · tier T1
Session: 74 s · exit 0
Tests run by vigia: `uv run --quiet --with pytest --with ruff sh -c 'ruff check . && ruff format --check . && pytest -q'` → passed — .................                                                        [100%]
Files changed: `src/vigia/init.py`, `tests/test_init.py`

## Summary
- `src/vigia/init.py`: new `_login()` reads the logged-in GitHub login with `gh api user --jq .login` (through the injected runner; a failed call or output that is empty or not a single token counts as unreadable). `init()` now sets `Config.humans` from it: `[login]` by default.
- Interactive mode asks "Who may add vigia:human-ok on T2 changes? (comma-separated GitHub logins)" after the test-command prompt, with the login as the default; an empty answer keeps the default, otherwise the answer is split on commas (whitespace trimmed, empties dropped). `--yes` asks nothing.
- If the final list is empty (login unreadable and no answer), init writes `humans = []` and prints one line saying anyone with write access can then approve T2.
- `tests/test_init.py`: the fake runner answers `gh api user --jq .login` (`login=None` simulates failure). New tests: `--yes` writes the login; interactive default is the login and an empty answer keeps it; two logins typed; login unreadable gives `[]` plus exactly one warning; unreadable and empty interactive answer stays `[]`.
## Tests run
- `uv run --quiet --with pytest --with ruff sh -c 'ruff check . && ruff format --check . && pytest -q'` — ruff check passed, 38 files already formatted, all tests passed.
## Deviations
- none (the done-test in a scratch clone with a real `gh` was not run: no real `gh` calls allowed here; covered by the fake-runner test instead).
## Risks
- Behaviour change: every new `vigia init` now restricts T2 approval to one login by default; anyone else adding `vigia:human-ok` will be ignored by `watch.human_approved` (untouched).
- The interactive warning also prints when the login was unreadable but the user then typed nothing; if they typed logins, no warning.
- Login validation is minimal (one token, no whitespace); typed logins are not checked against GitHub.
## Follow-ups
- none
