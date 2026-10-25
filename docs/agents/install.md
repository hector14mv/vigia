# Install vigia (instructions for an AI agent)

Paste this file, or its URL, into Claude Code or Codex on the machine where vigia should run, and say which
repositories it should work on. The agent sets vigia up end to end and stops for you at every login.

---

You are setting up **vigia** (https://github.com/hector14mv/vigia) for the person you are working with: AI
agents build changes from GitHub issues, a model from the other lab reviews each one, and `vigia watch` merges
by fixed rules. Read the repository's `README.md` and `docs/server.md` first; they are the source of truth, and
this file only orders the work.

## Rules

- Ask before every `sudo`, package install, or change outside the user's home directory, and say what it is for.
- Never type, read, print, copy or store a password, token or API key. Every login is done by the person: run
  the login command, tell them exactly what to do in their browser, wait, then check it with the status command.
- Confirm each step with its check before the next one, and report failures with the exact error.
- Do not edit vigia's code. Do not add `vigia:human-ok` or remove `vigia:parked` anywhere: those are the person's.

## Steps

1. **Where it runs.** Ask: laptop (simplest) or an always-on server (recommended; `docs/server.md`)? Check the
   OS (`uname -a`; macOS or Linux), the CPU count and the RAM.
2. **Tools.** Check `git`, `gh`, `claude`, `codex` and `uv`; install what is missing with the commands in
   `docs/server.md` §1 (or the README's Quickstart on a laptop), after asking. Install vigia with
   `uv tool install git+https://github.com/hector14mv/vigia` and check `vigia --version`.
3. **Logins (the person does these).** `gh auth login --web` then `gh auth setup-git`; `claude auth login`;
   `codex login --device-auth` on a server (or `codex login` on a laptop). Check: `gh auth status`,
   `claude auth status`, `codex login status`.
4. **Repositories.** Ask which GitHub repositories vigia should work on and which lab should build (the other
   one reviews). Clone them side by side in one folder (e.g. `~/factory`), run `vigia init` in each (answer its
   questions with the person), commit and push the files it writes if the person agrees, then run
   `vigia workspace` in the folder. Then ask the person to protect each base branch on GitHub (Settings →
   Branches, a rule for `main`: "Require status checks to pass before merging" with their CI check, and
   "Require branches to be up to date before merging", "Do not allow bypassing the above settings", no required
   approving reviews). Do not change repository
   settings yourself: vigia v0.1 relies on this rule to keep a stale head from merging, and it is theirs to set.
5. **Check.** `vigia doctor` in each clone, if this version has it; otherwise `vigia status`. Everything should
   be ok.
6. **Server only.** Write the two systemd user units from `docs/server.md` §4, enable linger, start them, and
   show the first lines of `journalctl --user -u vigia`. Ask before installing Tailscale; if the person wants
   the Mesa on their phone, follow §5 and give them the `https://….ts.net` address.
7. **First ticket.** Offer to write one small T0 ticket with the person (the template is
   `.github/ISSUE_TEMPLATE/vigia-ticket.md`) and add `tier:T0` and `vigia:ready`, so they see a full loop.

## Report

End with: what is installed and running, where the Mesa is, which logins expire and how to renew them, and
what is left for the person.
