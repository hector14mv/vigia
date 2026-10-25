# Security

vigia runs AI coding agents on your machine. Read this before you point it at a repository.

## What the engines can do with your machine

Engine sessions run as you, with the `claude` and `codex` logins already on the machine, so they
spend your subscriptions or API credit, and a builder can read every file and run every command
your user can.

vigia itself stores no token. It shells out to `git`, `gh`, `claude` and `codex`, and only vigia's
own code acts on GitHub: it pushes, opens pull requests, comments, labels and merges with the `gh`
login you already have. Engine sessions do not get that login (`session_env()` in
`src/vigia/engines.py`):

- they inherit an allowlist of your environment (`SESSION_ENV`), so GitHub tokens, SSH agents,
  askpass programs and `GIT_*` settings never reach them;
- `gh` inside a session has no account: an empty `GH_CONFIG_DIR`, and `GH_TOKEN` set to a sentinel that
  GitHub rejects, because `gh` would otherwise fall back to the token in your OS keyring;
- git inside a session has no credential helper or askpass program, and `GIT_ALLOW_PROTOCOL=file`
  makes it refuse every network transport (https, ssh, git), whatever your git config holds.

That stops a session from using your GitHub login by accident, or because a file or an issue told
it to. It is not a sandbox against a process determined to misbehave: a session runs as your user
and could, for example, read your credential stores itself, or clear `GH_TOKEN` on purpose to let `gh`
reach your keyring again.

One more limit: vigia controls the environment it starts a session with, not your shell's startup files. If
`~/.zprofile`, `~/.zshenv`, `~/.bash_profile` or `~/.profile` exports a GitHub token, a login shell that a
session opens exports it again. `gh` does not need such a line (it keeps its own login); remove it, or keep it
only in `~/.zshrc`, which non-interactive shells do not read. To rule this out, run vigia under a separate OS
user or in a container (see [Run it safely](#run-it-safely)).

Claude Code sessions start without your MCP servers, so a Claude builder or reviewer cannot reach your mail,
calendar, browser or other connected tools: vigia runs every Claude role with `--strict-mcp-config` and no
`--mcp-config`, and drops any `--mcp-config` a `vigia.toml` override adds. v0.1 has no way to give a Claude
session MCP tools.

Codex sessions still start with the MCP servers, apps and plugins configured for Codex on the machine
([#96](https://github.com/hector14mv/vigia/issues/96)): every way to turn them all off from the command line
also loses some logins. Whether a session may call one depends on Codex's own approval settings. Turn off the
servers and plugins you do not want a builder or a gate to reach in `~/.codex/config.toml`, or run vigia under
its own OS user with a Codex setup of its own.

## Who can start a build

An issue's body and title are the builder's whole prompt, so the `vigia:ready` label decides what an engine is
told to do. vigia builds an issue only when that label was added by someone with write, maintain or admin
permission on the repository, and only when neither the body nor the title changed after it was added; anything
else is refused, with the reason written on the issue. The ticket template adds no label: a maintainer reads the
ticket and adds `vigia:ready`. Give write access only to people you would let run commands as your user.

The tier label sets how deep the review goes, and v0.1 does not yet notice a tier changed after the ready
label ([#79](https://github.com/hector14mv/vigia/issues/79)). The triage role can edit labels without write
access, so grant it only to people you would trust to set a ticket's tier.

## The sandboxing vigia applies

The flags live in `DEFAULT_ENGINE_ARGS` in `src/vigia/config.py` and are passed by
`src/vigia/engines.py`. They limit the engines. They are not a security boundary.

- **Claude builders** run with `--permission-mode acceptEdits` and
  `--allowedTools Bash Read Edit Write Glob Grep`. Unrestricted `Bash` is allowed, so a builder
  can run any command your user can run.
- **Codex builders** run with `--sandbox workspace-write`. Writes are meant to stay in the
  workspace, and the sandbox is Codex's own: vigia relies on the CLI and does not verify it.
- **Reviewers are read-only, whatever the config says.** Codex reviewers always get
  `--sandbox read-only`; any other `--sandbox` override is removed. Claude reviewers get only
  `Read`, `Glob` and `Grep` by default, and `--disallowedTools Bash Edit Write NotebookEdit` is
  appended after any override you configure. A deny rule wins over an allow rule in Claude Code,
  so a reviewer has no shell and cannot write, even if `[engines.claude]` tries to give it `Bash`.
- You can replace the builders' arguments under `[engines.<engine>]` in `vigia.toml`; that is your
  call to make.

Builders work in throwaway git worktrees under `vigia/` in the system temp directory. vigia only
deletes inside that directory: `remove()` in `src/vigia/safety.py` refuses the filesystem root,
your home directory, symlinks and any path outside the work root. This protects against vigia's
own bad paths. It does not stop a builder from running commands that touch files outside its worktree.

## Run it safely

- Use a machine or OS account that holds no production credentials, cloud keys or SSH keys you
  would mind losing. A throwaway VM or a dedicated user is best.
- Protect the base branch on GitHub (required status checks, require branches to be up to date, no
  bypassing, no force pushes), so a bug in any layer cannot push straight to it.
- Keep `require_ci = true` under `[checks]` in `vigia.toml`. It is the default.
- Run T2 for anything that touches money, auth or personal data, and set `checks.humans` to
  your own GitHub login so only you can add the `vigia:human-ok` label.
- Read the diff of every merged pull request at first, until you trust your tier labels.

## Reporting a vulnerability

Email **hector@hectormoyanovelez.com**. Please do not open a public issue or pull request for a
vulnerability. Include what you found, how to reproduce it and the version or commit. I will
acknowledge your report and keep you informed until it is fixed.
