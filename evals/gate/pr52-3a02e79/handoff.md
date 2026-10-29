## Handoff 3a02e79cc4e7bd39551e4904be2a902d1ceea064
Engine: claude · claude-sonnet-5-5 · effort high · builder · lab anthropic
Ticket: #51 · tier T1
Session: 170 s · exit 0
Tests run by vigia: `uv run --quiet --with pytest --with ruff sh -c 'ruff check . && ruff format --check . && pytest -q'` → passed — ...................................                                      [100%]
Files changed: `src/vigia/jobs.py`, `src/vigia/watch.py`, `tests/test_jobs.py`

## Summary
- `src/vigia/jobs.py` (`spawn`): the log-folder `mkdir` and the log-file open now sit inside the existing `try/finally` that closes the locked descriptor, so a failure there closes the fd (releasing the flock) and the error propagates. Nothing is written to the ledger in that case.
- `src/vigia/watch.py` (`tick`, new `_still_building`): a PR whose opening build job (found through the ledger's `pr_opened` row, `issue` field) still holds its `issue-N` lock gets `Decision("wait", ...)`, the same way a PR with a live `pr-N` job already does. That also holds back merge and park for that PR, which is intended: with no handoff yet the rules would park it as "unknown builder".
- `tests/test_jobs.py`: two new tests. `test_spawn_releases_the_lock_when_the_log_folder_cannot_be_made` (a file sits where `.vigia/logs` should be; asserts the error, no child started, the target not busy, and that it can be spawned again after the fault is cleared). `test_no_gate_starts_while_the_build_that_opened_the_pr_holds_its_issue` (tick 1 with `issue-N` held: decision `wait`, nothing spawned; tick 2 after release: `gate` spawned).
## Tests run
- `uv run --quiet --with pytest --with ruff sh -c 'ruff check . && ruff format --check . && pytest -q'` — ruff clean, 47 files formatted, all tests pass.
- Mutation check: restored `jobs.py` and `watch.py` from HEAD and ran `pytest -q tests/test_jobs.py` — both new tests failed; with the fixes back, the full suite passes.
## Deviations
- none
## Risks
- `_still_building` relies on the ledger's `pr_opened` row to map PR to issue. A PR opened by hand or by another agent has no such row, so it is never held back (it also has no build job to wait for).
- The wait also applies to merge/park decisions, not only gates, while the issue lock is held. The build job holds that lock until it returns, so the hold is short.
- `spawn` now raises on a log-folder failure instead of leaking; `_spawn_jobs` and `tick` do not catch `OSError`, so the exception reaches `watch.loop`, which records it as a tick error and retries next tick. Check that this matches how you want it reported.
## Follow-ups
- none
