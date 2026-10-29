## Handoff 85057643a6581a40de0e1b3cb2c021a7310553fa
Engine: claude · claude-opus-5-5 · effort high · builder · lab anthropic
Ticket: #89 · tier T2
Session: 574 s · exit 0
Tests run by vigia: `uv run --quiet --with pytest --with ruff sh -c 'ruff check . && ruff format --check . && pytest -q'` → passed — .............                                                            [100%]
Files changed: `src/vigia/engines.py`, `tests/test_engines.py`

## Summary
- `src/vigia/engines.py`, `Engines.command()`, Claude: every role now ends its engine arguments with `--strict-mcp-config`, appended after any `vigia.toml` override and after the forced reviewer `--disallowedTools`, right before `--output-format`. Any `--mcp-config` in an override is dropped first, in both spellings (`--mcp-config a b ...` with all its values, and `--mcp-config=x`), by a new helper `_without_option()`. So strict mode loads zero servers.
- `src/vigia/engines.py`, `Engines.command()`, Codex: every role now gets `--ignore-user-config -c features.apps=false -c features.plugins=false`, after the role's or override's arguments and before `-C`. Any `-c`/`--config` override that sets `mcp_servers` or a key under it is dropped (`-c k=v`, `--config k=v`, `--config=k=v`, `-ck=v`) by a new helper `_without_codex_config()`. The feature switches go in as `-c` rather than `--disable` on purpose: probed on codex-cli 0.164.0, a `-c features.plugins=true` beats `--disable plugins` in either order, while the last `-c` wins between two `-c`s and beats `--enable`. Placed last, they also beat an override's `--enable apps` or `-c features.plugins=true`.
- `tests/test_engines.py`: 7 new test cases.
  - Claude builder and reviewer, with and without a schema: `--strict-mcp-config` is the last argument before `--output-format`, there is no `--mcp-config`, and model and effort are unchanged.
  - Claude builder and reviewer overrides that try `--mcp-config` (file, inline JSON, `=` form): the flag is stripped, the rest of the override passes through, strict comes last, and the reviewer deny list still holds.
  - Codex builder and reviewer: the forced flags are present exactly once, after the sandbox and before `-C`; model, effort and sandbox are unchanged.
  - Codex override with `-c features.plugins=true`, `--enable apps` and four `mcp_servers` spellings: all servers are stripped, the forced `-c` come after the override, and an unrelated `-c model_verbosity=low` survives.
  - Fixers run as `builder`; gates, reviewers and the planner run as `reviewer` (see `builder.py`, `gate.py`, `plan.py`), so the two roles cover all five.
- Done-test printout (abridged):
  - Claude reviewer: `[... '--disallowedTools', 'Bash', 'Edit', 'Write', 'NotebookEdit', '--disallowedTools', 'Bash', 'Edit', 'Write', 'NotebookEdit', '--strict-mcp-config', '--output-format', 'json']`
  - Claude builder: `[... '--allowedTools', 'Bash', 'Read', 'Edit', 'Write', 'Glob', 'Grep', '--strict-mcp-config', '--output-format', 'json']`
  - Codex gate: `['codex', 'exec', '-m', 'gpt-6.1-sol', '-c', 'model_reasoning_effort=high', '--sandbox', 'read-only', '--ignore-user-config', '-c', 'features.apps=false', '-c', 'features.plugins=false', '-C', '/w', '-o', '/w/o', '-']`

### Real probes on this machine (Claude Code 2.1.294, codex-cli 0.164.0)
Every probe built its command with `Engines.command()` and ran it under `session_env()` + `session_guards()`, the same way `Engines.run()` does. The only additions were `--skip-git-repo-check --ephemeral` for Codex (the scratch dir is not a repo) and `stream-json --verbose` for Claude (to read the init event). The scratch dir was outside this worktree.

**Claude** (model `claude-haiku-5-5`, effort `low`, to keep the cost down; the `init` event lists the loaded MCP servers and tools):
- Reviewer, before (no strict): `EXIT: 0`, 30 servers including `claude.ai Gmail`, `claude.ai Google Calendar`, `claude.ai Google Drive`, `plugin:design:gmail`, `playwright`, `posthog`; `mcp tools: 343 of 373`; `result: OK is_error: False`.
- Reviewer, after: `EXIT: 0`, `mcp_servers: []`, `mcp tools: 0 of 27`, `result: OK is_error: False`.
- Builder, before: same 30 servers, `mcp tools: 343 of 377`. Builder, after: `mcp_servers: []`, `mcp tools: 0 of 31`, `result: OK is_error: False`.
- So `--strict-mcp-config` also drops plugin servers and claude.ai connectors, not only `~/.claude.json` servers, and auth still works.

**Codex** (`gpt-6.1-sol`; the session was asked to list every MCP server/tool or answer `NO_MCP_TOOLS`):
- Before, reviewer (today's command): `EXIT: 0`, header `model: gpt-6.1-sol / sandbox: read-only / reasoning effort: high`. It listed 608 tools: `apple_mcp` (7: calendar, contacts, mail, maps, messages, notes, reminders), `codex_apps` (568 ChatGPT connector tools, e.g. `codex_apps/vercel_update_project`, `codex_apps/chatgpt_space_edit_page`), `playwright` (25, e.g. `playwright/browser_navigate`), `context7` (2), `node_repl` (3), `cua_repl` (2) and `openai_api_key_local_confirmation` (1). The SessionStart hooks of the `codex@openai-codex` plugin also ran.
- `--ignore-user-config` alone: `apple_mcp`, `playwright`, `context7`, `node_repl` and `cua_repl` are gone, but `codex_apps` (568) and `openai_api_key_local_confirmation` remain. The latter is the MCP server of the remote curated plugin `openai-developers` (`~/.codex/plugins/cache/openai-curated-remote/...`).
- `+ --disable apps`: only `openai_api_key_local_confirmation` remains.
- `--disable apps --disable plugins` without `--ignore-user-config`: `apple_mcp`, `playwright`, `context7` and `node_repl` remain. So no single flag is enough.
- **After**, with the final shipped command (effort set to `medium` on purpose, to differ from the user's config default `high`):
  - Reviewer: `EXIT: 0`, `model: gpt-6.1-sol`, `provider: openai`, `sandbox: read-only`, `reasoning effort: medium`, `LAST: NO_MCP_TOOLS`.
  - Builder: `EXIT: 0`, `sandbox: workspace-write [workdir, /tmp, $TMPDIR]`, `reasoning effort: medium`, `LAST: NO_MCP_TOOLS`.
  - Builder shell check: asked to run `sh -c 'echo "$GH_TOKEN" > built.txt'`, it ran (`EXIT: 0`). The file holds `vigia-engine-session-has-no-github-access`, so commands, writes and the `shell_environment_policy` guards still work under `--ignore-user-config`.
- Auth: the ChatGPT login under `CODEX_HOME` was used in every run.

## Tests run
- `uv run --quiet --with pytest --with ruff sh -c 'ruff check . && ruff format --check . && pytest -q'`: all passed (ruff clean, formatting unchanged, full suite green).
- `pytest -q tests/test_engines.py`: passed.
- Mutation check, restoring the file after each run. Removing `--strict-mcp-config`, the Claude `--mcp-config` strip, the Codex `mcp_servers` strip, `--ignore-user-config` or `features.plugins=false` each makes at least one new test fail.
- The real Claude and Codex probes quoted above.

## Deviations
- Codex is changed, not left alone: the ticket's condition held, with three flags (`--ignore-user-config` + two `-c features.*=false`) rather than one. No single flag removes every server.
- Codex overrides that set `mcp_servers` are stripped, the analogue of the Claude `--mcp-config` strip. The ticket asked for that only on the Claude side. Without it, a `vigia.toml` override could add servers back, because `--ignore-user-config` still honours `-c`.
- The Claude probe used `claude-haiku-5-5` to keep it cheap. MCP loading does not depend on the model.

## Risks
- **`--ignore-user-config` drops all of `~/.codex/config.toml`, not only MCP.** Model, effort, sandbox and the env policy are passed explicitly and verified. A user whose Codex login depends on config.toml would break: a custom `model_provider` / `[model_providers.*]` (Azure, a proxy, `--oss`), `preferred_auth_method`, a custom `base_url`, `sandbox_workspace_write.network_access`, `service_tier`. On this machine (ChatGPT login) every role authenticates. Docs should say so. Overrides in `vigia.toml` `[engines.codex]` (`-c model_provider=...`) still apply, so that is the workaround.
- Also dropped for Codex sessions: the user's `notify` hook, `[features] memories = true`, `personality`, `[agents]` limits, project trust entries and all plugin hooks. I believe each is harmless or desirable for a factory session. `~/.codex/AGENTS.md` still loads: the probe sessions answered in Spanish, so the user's global instructions still reach Codex sessions (not MCP, but worth knowing).
- **No opt-out in v0.1**: a user who wants MCP in builders (e.g. context7 docs, playwright for UI checks) loses it on both engines, and overrides cannot bring it back. This is by design for this ticket, and the docs PR should say it.
- Codex `-p/--profile` in an override is not stripped. I did not probe whether a profile still layers on top of `--ignore-user-config`. If it does, a profile with `mcp_servers` would add servers. It only comes from the repo's own `vigia.toml`.
- The Codex strip matches only keys spelled `mcp_servers...`. A quoted TOML key like `-c '"mcp_servers".x=...'` would get through. Same trust boundary as above.
- These Codex flags are tied to codex-cli 0.164.0 behaviour (`--ignore-user-config`, feature names `apps`/`plugins`, `-c` precedence). An older CLI without `--ignore-user-config` would fail at startup, loudly rather than silently. A future CLI that adds a new MCP source would bring servers back silently. A periodic `NO_MCP_TOOLS` probe would catch that.

## Follow-ups
- Docs PR (README/SECURITY.md/docs): engine sessions run with no MCP servers. Codex sessions ignore `~/.codex/config.toml`, so a custom provider must be passed through `[engines.codex]` overrides, and there is no MCP opt-in in v0.1.
- Possibly a `vigia doctor`-style check that runs the `NO_MCP_TOOLS` probe against the installed CLIs after an upgrade.
- Decide whether Codex `-p/--profile` overrides should be rejected the way `mcp_servers` overrides are.
