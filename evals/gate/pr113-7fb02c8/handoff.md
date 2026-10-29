## Handoff 7fb02c83f942670f709804f2b0d6cdb06f7f5e02
Engine: claude · claude-opus-5-5 · effort high · builder · lab anthropic
Ticket: #112 · tier T2
Session: 445 s · exit 0
Tests run by vigia: `uv run --quiet --with pytest --with ruff sh -c 'ruff check . && ruff format --check . && pytest -q'` → passed — ..................                                                       [100%]
Files changed: `src/vigia/comments.py`, `src/vigia/config.py`, `tests/test_comments.py`, `tests/test_config.py`, `tests/test_e2e.py`, `tests/test_rules.py`

## Summary
- `src/vigia/config.py`: new `lab_of(engine, model)`. `claude` → `anthropic` and `codex` → `openai` whatever the model (the existing `FAMILIES` table). For a routed engine (`ROUTED_ENGINES = {"opencode"}`), the model must be `provider/model`. The lab is the model part's family from the explicit allowlist `MODEL_FAMILIES` (claude-, gpt-, o<digit>, codex-, gemini-, grok-, kimi-, glm-, deepseek-, qwen-), matched with `re.IGNORECASE | re.ASCII`. The provider is never read. No `/`, an empty provider or model part, a second `/`, an unknown family or an unknown engine all return None.
- `Seat.family` now returns `lab_of(engine, model)` and raises `ValueError` when that is None, so a seat with no lab can never compare equal or unequal to anything. The `claude` and `codex` values are unchanged.
- `other()` raises `ValueError` for any engine except `claude` and `codex`. `default_tiers()` and `vigia init` are unchanged.
- `_seat()` refuses `engine = "opencode"` with `tiers.<T>.<role>.engine: OpenCode is not available yet (#107)`, before the generic unknown-engine check.
- `src/vigia/comments.py`: `ENGINE_LINE` also accepts `opencode` (the pattern only grew by that word). A new `ENGINE_MODEL` regex reads the `· <model>` field right after the engine. `engine_family()` returns `lab_of(engine, model)` and never reads the trailing `lab …` text. The first handoff with an `Engine:` line still decides, even when it gives no lab, so the PR parks as an unknown builder.
- Tests:
  - `tests/test_config.py`: one parametrized case per lab-table row, the no-lab cases (including ASCII-only case folding and digits), `Seat.family` raising, `other()`, `default_tiers()` unchanged, and the OpenCode refusal for each role, naming the seat.
  - `tests/test_comments.py`: `Engine:` line cases, including trailing `lab …` ignored and opencode lines with no model, no provider or an unknown family.
  - `tests/test_rules.py`: `decide` parks an `opencode · anthropic/claude-x` PR when the gate is claude.
  - `tests/test_e2e.py`: a hand-opened PR carrying that line is parked by `tick`, and `gate.review` refuses it without running an engine.
## Tests run
- `uv run --quiet --with pytest --with ruff sh -c 'ruff check . && ruff format --check . && pytest -q'`: the first run had one failure, `tests/test_mesa.py::test_a_mesa_that_has_not_read_github_yet_answers_at_once_with_what_this_machine_knows` (it asserts on Mesa page text and touches no lab code). Run alone 3 times, it passed every time. A second full run passed completely: ruff check clean, format clean, all tests pass. It looks timing-sensitive under load.
- `uv run --quiet --with pytest pytest -q tests/test_config.py tests/test_comments.py tests/test_rules.py`: all pass.
- `uv run --quiet vigia doctor`: `vigia.toml` loads (ok). The gh, repo and labels checks fail only because this sandbox has no `gh auth`.
## Deviations
- `rules.py` step 2, `gate.review()`, `builder.fix()` and `context.builder_family()` needed no code change. They already compare only `Seat.family` and `builder_family`, and both values now come from `lab_of` (ledger `family` is written from `Seat.family`). No place compared engine names to decide a lab, so I left these files untouched.
- A model part that contains another `/` (e.g. `openrouter/anthropic/claude-x`) gets no lab. The brief does not cover nested paths, and refusing them is the conservative reading of "one provider, one model". The engine ticket may want to revisit this for OpenRouter-style ids.
- `Seat.family` raises `ValueError` for a seat with no lab instead of returning None. A None family would make `gate.family == builder.family` false and let a pairing through.
## Risks
- Check every `MODEL_FAMILIES` row, especially `o[0-9]`: it matches `o3`, `o4-mini` and also any future name that starts with `o` plus a digit.
- `ENGINE_MODEL` takes the first `·`-separated token after the engine on the same line. Check this against the handoff writer in `comments.handoff()` (`Engine: {engine} · {model} · …`).
- `Seat.family` can now raise. Today no reachable path builds a seat with no lab: config refuses opencode, and `default_tiers` only builds claude and codex seats.
## Follow-ups
- When #107 lets `opencode` load, `_seat()` should refuse a seat whose `lab_of(engine, model)` is None with a `ConfigError` that names the seat. Today `Seat.family` would raise a bare `ValueError` from `parse()`.
- `tests/test_mesa.py::test_a_mesa_that_has_not_read_github_yet_answers_at_once_with_what_this_machine_knows` failed once under the full suite and passed alone. It looks timing-sensitive.
