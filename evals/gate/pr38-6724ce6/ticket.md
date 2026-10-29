## Outcome
An engine session (builder, fixer, gate or reviewer) cannot act on GitHub: it cannot label, comment, approve, push or merge, because it runs without the user's GitHub credentials. Only vigia's own deterministic code talks to GitHub.

## Scope
`src/vigia/proc.py` (`run_session`: accept an environment), `src/vigia/engines.py` (`Engines.run`: build that environment), tests in `tests/test_engines.py` (plus `tests/test_e2e.py` if useful), and the sections of `SECURITY.md` that describe what engine sessions can do.
Not touched: how vigia itself calls `gh` and `git` (`gh.py`, `git.py`, `proc.run`), the engines' own logins (`claude` and `codex` keep working), `rules.py`.
Out of scope, documented as limits in SECURITY.md (the planner fixed this boundary after three gate rounds kept finding vectors outside what an environment can control): a process that deliberately clears or edits its own environment, reads OS credential stores itself, or runs a login shell whose startup files (`~/.zprofile`, `~/.profile`, …) export a token. Findings in these classes are not P1s for this ticket.
Must still hold: builders can still read and edit files and run the project's tests in their worktree (variables outside the allowlist, such as a test database URL, are not passed; vigia's own run of `checks.test_command` still gets the full environment); `claude -p` and `codex exec` still authenticate with their own logins; vigia still pushes, opens PRs and comments with the user's `gh` login; every existing test passes.

## Acceptance criteria
- [ ] every engine session runs with `GH_TOKEN`, `GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN` and `GITHUB_ENTERPRISE_TOKEN` removed from its environment, and `GH_CONFIG_DIR` pointing at an empty directory vigia creates under its work root, so `gh` inside the session is logged out
- [ ] git inside the session cannot borrow the user's GitHub login or reach a remote: the session environment is an allowlist of the caller's (no tokens, SSH agent, askpass programs or `GIT_*` settings), credential helpers are emptied through command-scope config, `GIT_ASKPASS` is empty, `GIT_TERMINAL_PROMPT=0`, and `GIT_ALLOW_PROTOCOL=file` makes git refuse every network transport (amended by the planner after two gate rounds: a denylist kept missing vectors — askpass, `GIT_CONFIG_PARAMETERS`, SSH agents, `http.extraheader`)
- [ ] what the engines need passes through (PATH, HOME, locale, proxies and CA bundles, `ANTHROPIC_*`, `CLAUDE_*`, `OPENAI_*`, `CODEX_*`)
- [ ] a test with a fake session runner asserts the environment it receives has none of the token variables, a `GH_CONFIG_DIR` inside the work root, and `GIT_TERMINAL_PROMPT=0`
- [ ] SECURITY.md says what a session can and cannot do, and the limit: this stops accidental or instructed use of the user's login, not a process determined to read credential stores itself (that needs a separate OS user or a container)

## Done-test
Run a builder session on a scratch issue whose ticket asks it to run `gh auth status` and record the output in its handoff: it reports not logged in, and the PR is still opened and commented by vigia.

## Risks
T2: credentials and the launcher. Facts: verified (`grep -n "subprocess.Popen" src/vigia/proc.py` and `grep -n "self._run(" src/vigia/engines.py`): engine sessions inherit the caller's whole environment; verified (`env | grep -c '^GITHUB_TOKEN='` → 1 on the maintainer's machine, and git's credential helper for github.com is `gh auth git-credential`): a session can therefore use the user's GitHub login. Consequence: a builder working on one PR could add `vigia:human-ok` to another PR as an allowed human, and `watch.human_approved` could not tell. hypothesis — disprove if wrong: Claude Code and Codex CLI do not need any GitHub variable to authenticate themselves.
