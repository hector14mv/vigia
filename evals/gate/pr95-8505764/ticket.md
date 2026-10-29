## Outcome
Engine sessions start without the user's MCP servers, so a builder, a fixer or a reviewer cannot reach the person's mail, calendar, browser or other tools through MCP. Today every Claude and Codex session vigia starts loads them.

## Scope
`Engines.command()` in `src/vigia/engines.py`, and its tests in `tests/test_engines.py`.
Not touched: `README.md`, `SECURITY.md` and `docs/` (a docs PR describes the result); the existing forced reviewer flags (`--disallowedTools …` for Claude, `--sandbox read-only` for Codex); `session_env()`; `vigia.toml` keys.
Must still hold: every role still starts and authenticates. That means Claude builder, fixer, gate, reviewer and planner, and Codex builder, fixer and gate, using the logins already on the machine. The model, effort, sandbox and schema flags reach the CLI as today.

## Acceptance criteria
- [ ] Every Claude command ends its engine arguments with `--strict-mcp-config`, with no `--mcp-config`, after any `vigia.toml` override, exactly as the forced `--disallowedTools` is appended today. A test covers each role, and an override that tries `--mcp-config` for a reviewer.
- [ ] Codex sessions start with no MCP servers, **if a flag of the installed Codex CLI does it while keeping auth, the model, effort and sandbox**. Show it with one real `codex exec` probe that asks the session to list its MCP tools, before and after, and quote both in the handoff. If no flag does it, leave Codex unchanged and say so in the handoff's Risks, with what was tried.
- [ ] `ruff check .`, `ruff format --check .` and `pytest -q` pass.

## Done-test
`pytest -q tests/test_engines.py`. A `Engines(...).command(...)` printout for a Claude reviewer and a Claude builder shows `--strict-mcp-config` as the last engine argument before `--output-format`.

## Risks
- Security and the process itself, so T2.
- Verified on this machine:
  - Claude Code 2.1.294's help says `--strict-mcp-config` will "Only use MCP servers from --mcp-config, ignoring all other MCP configurations".
  - `codex -c 'mcp_servers={}' mcp list` (codex-cli 0.164.0) still lists the bundled plugin servers (`apple-mcp`, `cua_repl`, `node_repl`, `playwright`) as enabled, so that override does not remove them.
- Hypothesis, unverified: `codex exec --ignore-user-config` exists ("Do not load `$CODEX_HOME/config.toml`; auth still uses `CODEX_HOME`"). It may remove the user's servers, but plugins may come from elsewhere, and it may drop settings a session needs. Probe it before relying on it.
- A user who wants MCP in builders loses it, with no override in v0.1. Say so in the handoff for the docs.
