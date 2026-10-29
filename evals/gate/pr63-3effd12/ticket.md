## Outcome
The Mesa is gentle with GitHub and honest about its own failures: a refresh never hammers the API when GitHub is slow or rate-limiting, and an unexpected error in the server shows as a server error, not as "the Mesa cannot reach vigia".

## Scope
`src/vigia/mesa.py` (the state cache and the request handler) and `src/vigia/mesa.html` (how a failed `/api/state` is shown), with tests.
Not touched: what the state contains; the read-only rules (no write verbs, Host check, CSP).
Must still hold: the page refreshes every 15 s while GitHub answers; a GitHub failure still makes the affected lists unknown, never empty.

## Acceptance criteria
- [ ] After a refresh fails because GitHub could not be read, the next attempt waits longer (backoff up to a few minutes), and the page shows when it last succeeded.
- [ ] A non-GitHub exception while building the state answers 500 with the error, and the page says the server failed (with the message), distinct from "cannot reach".
- [ ] Tests cover the backoff timing (with an injected clock) and the 500 path.

## Done-test
`uv run --with pytest --with ruff pytest -q`

## Risks
Low: the Mesa is read-only. Found as P2 by the T2 reviewer (claude-fable-5-1) on #50 at ebe6cc26.

## Facts
- Verified (Fable on #50): each refresh spends about 5 + P GitHub calls (P = open PRs) on the same token the watcher uses, with no backoff (`src/vigia/mesa.py:147`).
- Verified (Fable on #50): an exception other than `CommandError` in `state()` reaches the page as "the Mesa cannot reach vigia" (`src/vigia/mesa.py:408`).
