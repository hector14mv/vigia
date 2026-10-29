## Outcome
Two small robustness gaps left by the parallel-slots work are closed: a job's lock file is never left open when its log folder cannot be created, and the watcher never starts a gate on a PR whose build has not posted its handoff yet.

## Scope
`src/vigia/jobs.py` (`spawn`), `src/vigia/watch.py` (what counts as gate-ready while a build job still holds its issue), and their tests.
Not touched: how target locks are taken or inherited by engine sessions; the rules table; the merge path.
Must still hold: `jobs.spawn` returns False without starting anything when the target is already locked; a spawned job still receives the locked file and holds it for its whole life; a PR whose handoff is posted gets its gate as today.

## Acceptance criteria
- [ ] If creating `.vigia/logs` (or opening the job log) raises inside `jobs.spawn`, the target lock's file descriptor is closed and the lock released, and the error propagates or is reported; a test forces the failure.
- [ ] While the build job for issue N is still running (its `issue-N` lock is held), the watcher does not spawn a gate for the PR that build just opened; once the build job ends, the gate is spawned on the next tick; a test covers both ticks.
- [ ] `pytest -q` passes.

## Done-test
`uv run --with pytest --with ruff pytest -q`

## Risks
Low: two edge cases in the job runner, found as P2 by the gate (codex gpt-6.1-sol) and the T2 reviewer (claude-fable-5-1) on #40's head 8eb98d64.

## Facts
- Verified (gate and Fable, #40 at 8eb98d64): `src/vigia/jobs.py:109` opens and locks the target file, then creates the log folder; a failure there leaks the descriptor and keeps the target locked until the watcher exits.
- Verified (Fable, #40 at 8eb98d64): `src/vigia/watch.py:201` can spawn a gate for a PR opened moments earlier by a build job that has not yet posted its handoff comment.
