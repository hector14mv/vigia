## Handoff 3effd12f7a58b461b4749e6d94a06f82ac678ff8
Engine: claude · claude-opus-5-5 · effort high · conductor (merge of the base) · lab anthropic
Tier: T1
Tests run: `ruff check .`, `ruff format --check .`, `pytest -q` → all passed
Files changed: the merge of `main` only (`tests/test_mesa.py` resolved; `src/vigia/mesa.py` merged cleanly)

## Summary
The rules parked this PR because #62 (planner stall and the Mesa's proposed list) merged first and both added tests at the end of `tests/test_mesa.py`. This head merges `main` into the branch and keeps both sets of tests; `src/vigia/mesa.py` merged without a conflict (#62 changed how proposed tickets are read, this PR the state cache and its backoff). No code of this PR changed.
## Tests run
- `pytest -q` on the merged tree: all passed, including #62's and this PR's Mesa tests together.
## Deviations
- none
## Risks
- none beyond the PR itself
## Follow-ups
- none
