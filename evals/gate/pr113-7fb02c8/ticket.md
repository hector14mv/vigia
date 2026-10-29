## Outcome
A seat's lab is computed by one function from its engine and exact model, through one explicit family table, and every place that pairs or compares labs uses that function. Today the lab comes from the engine name. This is the first of the two pieces of #107 (OpenCode as a third engine): it prepares the lab rule so an OpenCode seat can be added next without touching the cross-lab checks again. It adds no new engine.

## Scope
Touches:
- `src/vigia/config.py`: `FAMILIES` (today engine → lab) and `Seat.family` (today `FAMILIES[self.engine]`) become one function, e.g. `lab_of(engine, model)`; the same-lab refusal in `parse()`; `other()`.
- `src/vigia/comments.py`: `ENGINE_LINE` and `engine_family()`, which read a handoff's `Engine:` line.
- `src/vigia/context.py`: `builder_family()`.
- `src/vigia/rules.py` step 2, `src/vigia/gate.py` `review()` and `src/vigia/builder.py` `fix()`, only where they compare labs.
- Tests: `tests/test_config.py`, `tests/test_comments.py`, `tests/test_rules.py` and the gate/builder tests that cover lab checks.

What is required:
- **One lab function.** `claude` → `anthropic` and `codex` → `openai`, as today. For an engine that names `provider/model` (the form OpenCode will use, engine name `opencode`), the lab comes from the model part's family through one explicit table in `config.py`, never from the provider or the engine:
  - `claude-…` → `anthropic`
  - `gpt-…`, `o<digit>…`, `codex-…` → `openai`
  - `gemini-…` → `google`
  - `grok-…` → `xai`
  - `kimi-…` → `moonshotai`
  - `glm-…` → `zhipuai`
  - `deepseek-…` → `deepseek`
  - `qwen-…` → `alibaba`

  Matching is case-insensitive on the model part. The provider prefix only says where the request goes, so a reseller works: `github-copilot/claude-opus-5.5` is `anthropic` and `opencode-go/kimi-k3` is `moonshotai`. A model with no `/`, an empty part, or no matching family has no lab, and the function says so without guessing.
- **Every lab comparison goes through that function or a value it produced** (`Seat.family`, ledger `family`, the lab read from an `Engine:` line). No place compares engine names to decide a lab.
- **The `Engine:` line.** `ENGINE_LINE` also accepts `opencode`. `engine_family()` takes the lab from the model on that line with the same function, and never trusts trailing `lab …` text. An `opencode` line with no model, no provider or an unknown family yields no lab, so the PR parks as an unknown builder.
- **`other()`** raises on any engine but `claude` and `codex`. Today it answers `claude` for everything that is not `claude`. `default_tiers()` and `vigia init` are unchanged.
- **Config does not accept `engine = "opencode"` yet.** A seat that names it is refused with «OpenCode is not available yet (#107)». Running it is the next ticket.

Not touched: `src/vigia/engines.py` and how sessions run; the decision order and the merge step in `rules.py`; the caps, labels and ledger format; `vigia init`; the Mesa; README and SECURITY.md (they change with the engine ticket); `pyproject.toml` and every dependency; this repository's `vigia.toml`.

Must still hold: the cross-lab guarantee, exactly as today:
- Every existing test passes unchanged, except tests that assert the exact `ENGINE_LINE` pattern, which may grow by `opencode` only.
- `claude` vs `claude`, `codex` vs `codex`, and a PR with no `Engine:` line are still refused or parked exactly as today, by config loading, `rules.decide`, `gate.review` and `builder.fix`.
- A `vigia.toml` with `claude` and `codex` seats parses and renders (`render()` → `parse()`) to the same `Config` as before, with the same `Seat.family` values.

## Acceptance criteria
- [ ] A lab table test with one case per row, through the lab function:
  - `claude` with any model → `anthropic`; `codex` with any model → `openai`
  - `opencode` with `anthropic/claude-opus-5-5`, `github-copilot/claude-opus-5.5` or `Anthropic/Claude-X` → `anthropic`
  - `opencode` with `openai/gpt-6.1-sol` or `github-copilot/gpt-6-sol` → `openai`
  - `opencode` with `opencode-go/kimi-k3` → `moonshotai`; `github-copilot/gemini-x` → `google`; `opencode-go/glm-x` → `zhipuai`
- [ ] The lab function gives no lab for `opencode` with `kimi-k3` (no provider), `/kimi-k3`, `opencode-go/` and `somelab/mystery-1` (test).
- [ ] `engine_family()` reads `Engine: opencode · github-copilot/kimi-k3 · builder · lab anthropic` as `moonshotai`, so trailing text is ignored. It returns None for an `opencode` line with no model, no provider or an unknown family. The existing `claude` and `codex` lines read as before (test).
- [ ] `rules.decide` parks a PR whose `Engine:` line is `opencode · anthropic/claude-x` when the tier's gate is `claude`, and `gate.review` refuses to gate it (test).
- [ ] `other("opencode")` raises; `default_tiers("claude")` and `default_tiers("codex")` are unchanged (test).
- [ ] A `vigia.toml` seat with `engine = "opencode"` is refused at load with an error that names the seat (test).
- [ ] `ruff check . && ruff format --check . && pytest` all pass.

## Done-test
Run `pytest tests/test_config.py tests/test_comments.py tests/test_rules.py`: the lab table, the `Engine:` line cases and the same-lab parking through an `opencode` line pass. Run `vigia doctor` in this repository with its current `vigia.toml`: it loads as before.

## Risks
- This changes how the merge-safety invariant computes a lab. A wrong lab lets a lab review its own work. The family table is an allowlist, so an unknown name has no lab and parks or is refused. The remaining risk is a wrong row, so each row gets its own test.
- `Seat.family` is read in many places (ledger rows, handoffs, the Mesa's «Who does what»). Keep its name and values for `claude` and `codex` so nothing downstream changes.

Tier: T2 (changes how the cross-lab guarantee computes a lab). Difficulty: acotado (bounded: one function and its call sites, no new engine).
