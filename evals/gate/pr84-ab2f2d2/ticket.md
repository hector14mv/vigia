## Outcome
The reasons vigia prints and writes on PRs read as plain English: `gate: round 1 on this head`, not `gate: gate round 1 on head`, and a cap that is reached says so, not "all 2 are used".

## Scope
The reason strings returned by `rules.decide()` in `src/vigia/rules.py`, and the tests that quote them.
Not touched: every decision itself (action, stage, head) and the order of the rules; the `waiting for a human to add …` text, which `mesa.py` matches; the ledger row shapes; reasons already stored in old ledger rows.
Must still hold: every row of `tests/test_rules.py` returns the same action and stage as before, with only the expected text updated; `vigia watch` prints `PR #N: <action>: <reason>` as before.

## Acceptance criteria
- [ ] The gate decision reads `round N on this head` (printed `gate: round 1 on this head`). The review decision reads `<tier> round N on this head` (`review: T1 round 1 on this head`). The merge decision reads `<tier> requirements hold on this head`.
- [ ] A park for used-up gate rounds reads `<why>; a fix needs another gate round, and the cap of N is reached`, and fix rounds `<why>; the cap of N fix rounds is reached`. The same goes for the other "used up" parks: they name the cap, and never say "all N are used".
- [ ] `on head` becomes `on this head` in every reason that says it.
- [ ] `tests/test_rules.py`, `tests/test_mesa.py` and any other test that quotes a changed reason is updated to the new text. No assertion about an action, stage or head changes.

## Done-test
`ruff check . && ruff format --check . && pytest -q`; `git diff -- tests/test_rules.py` shows only string changes.

## Risks
- `mesa.py` decides the Human column with `"waiting for a human" in d.reason`: that text must not change.
- The tier is T1, not T2: no decision changes, only the words, and the row tests prove it. A reviewer who sees any action, stage or head change should report it as a P1.
