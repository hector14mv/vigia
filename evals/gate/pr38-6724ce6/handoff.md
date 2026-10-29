## Handoff 6724ce6188304462214b7302bdf9002a540c169e
Engine: claude · claude-opus-5-5 · effort high · fixer (round 2, gate) · lab anthropic
Ticket: #33 · tier T2
Session: 150 s · exit 0
Tests run by vigia: `uv run --quiet --with pytest --with ruff sh -c 'ruff check . && ruff format --check . && pytest -q'` → passed — ................................................                         [100%]
Files changed: `src/vigia/engines.py`, `tests/test_engines.py`

## Summary
- P1 `src/vigia/engines.py:37` (ALL_PROXY/all_proxy dropped from the session allowlist) → added `ALL_PROXY` and `all_proxy` to `SESSION_ENV` next to the other proxy variables in `src/vigia/engines.py`; `tests/test_engines.py::test_session_env_is_an_allowlist` now feeds `HTTPS_PROXY`, `ALL_PROXY` and `all_proxy` and asserts each reaches the session environment unchanged.
## Tests run
- `uv run --quiet --with pytest pytest -q tests/test_engines.py -k allowlist` with the `src/vigia/engines.py` change temporarily reverted → FAILED (`KeyError: 'ALL_PROXY'`), confirming the test fails without the fix.
- `uv run --quiet --with pytest --with ruff sh -c 'ruff check . && ruff format --check . && pytest -q'` → ruff "All checks passed!", "39 files already formatted", all tests passed (exit 0).
## Deviations
- none
## Risks
- Passing `ALL_PROXY` does not reopen git's network: `GIT_ALLOW_PROTOCOL=file` still refuses every network transport whatever the proxy, and `test_git_in_a_session_cannot_reach_any_remote` still passes.
## Follow-ups
- none
