# Security

vigia runs AI coding agents on your machine. Read this before you point it at a repository.

## What the engines can do with your machine

Builder sessions run as you. They use the accounts that are already logged in on the machine:

- the `claude` and `codex` CLI logins, so they spend your subscriptions or API credit;
- your git identity (`user.name`, `user.email`) and whatever git credentials your shell can use;
- every file, environment variable, network route and command the shell can reach.

vigia itself stores no token. It shells out to `git`, `gh`, `claude` and `codex`, and it uses
the `gh` login you already have. Engine sessions inherit vigia's environment as it is.

## The sandboxing vigia applies

The flags live in `DEFAULT_ENGINE_ARGS` in `src/vigia/config.py` and are passed by
`src/vigia/engines.py`. They limit the engines. They are not a security boundary.

- **Claude builders** run with `--permission-mode acceptEdits` and
  `--allowedTools Bash Read Edit Write Glob Grep`. Unrestricted `Bash` is allowed, so a builder
  can run any command your user can run.
- **Codex builders** run with `--sandbox workspace-write`. Writes are meant to stay in the
  workspace, and the sandbox is Codex's own: vigia relies on the CLI and does not verify it.
- **Reviewers are read-only by default.** Codex reviewers always get `--sandbox read-only`; any
  other `--sandbox` override is removed. Claude reviewers get `--disallowedTools Edit Write
  NotebookEdit` appended and, by default, only `Read`, `Glob`, `Grep` and `git diff`, `git log`
  and `git show`. That exclusion blocks those three tools only. It does not make a Claude reviewer
  read-only if you override its tools: if `[engines.claude]` gives a reviewer unrestricted `Bash`
  (or any permissive `--permission-mode` plus `Bash`), it can write or delete files through the shell.
- You can replace these arguments under `[engines.<engine>]` in `vigia.toml`. For builders, that
  is your call to make. For a Claude reviewer, never add `Bash` beyond the read-only `git`
  commands above, or you give it the same reach as a builder.

Builders work in throwaway git worktrees under `vigia/` in the system temp directory. vigia only
deletes inside that directory: `remove()` in `src/vigia/safety.py` refuses the filesystem root,
your home directory, symlinks and any path outside the work root. This protects against vigia's
own bad paths. It does not stop a builder from running commands that touch files outside its worktree.

## Run it safely

- Use a machine or OS account that holds no production credentials, cloud keys or SSH keys you
  would mind losing. A throwaway VM or a dedicated user is best.
- Protect the base branch on GitHub (required pull request, required status checks, no force
  pushes), so a bug in any layer cannot push straight to it.
- Keep `require_ci = true` under `[checks]` in `vigia.toml`. It is the default.
- Run T2 for anything that touches money, auth or personal data, and set `checks.humans` to
  your own GitHub login so only you can add the `vigia:human-ok` label.
- Read the diff of every merged pull request at first, until you trust your tier labels.

## Reporting a vulnerability

Email **hector@hectormoyanovelez.com**. Please do not open a public issue or pull request for a
vulnerability. Include what you found, how to reproduce it and the version or commit. I will
acknowledge your report and keep you informed until it is fixed.
