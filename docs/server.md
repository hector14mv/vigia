# Running vigia on a server

vigia works on a laptop, but a factory wants to be always on. On a small Linux server:

- the watcher keeps ticking at night and at weekends, when most of the merges in the factory this comes from
  happened;
- your laptop stays free, cool and closed;
- you approve T2 changes and read the Mesa from your phone.

Any always-on Linux machine works: a small cloud VM, a home server, a spare mini PC. Engine sessions are the
heavy part: plan on about two CPU cores and two GB of RAM per session at once (`vigia init` proposes a slot
count from the machine). Claude Code supports Ubuntu 20.04+ and Debian 10+ with 4 GB of RAM; so does Codex CLI.

This guide uses a normal user account, a systemd **user** unit and Tailscale. Every command below comes from
the documentation of the tool it belongs to, linked next to it. Where the documentation does not settle
something, the guide says **test it** instead of guessing.

> Prefer letting an agent do this? Paste [docs/agents/install.md](agents/install.md) into Claude Code or Codex
> on the server. It follows this guide and stops at every login for you.

## 1. Install the tools

```bash
# Claude Code (installs to ~/.local/bin/claude)            https://code.claude.com/docs/en/setup
curl -fsSL https://claude.ai/install.sh | bash
# Codex CLI                                                 https://github.com/openai/codex
curl -fsSL https://chatgpt.com/codex/install.sh | sh      # or: npm install -g @openai/codex
# uv, to install vigia                                       https://docs.astral.sh/uv/
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Install the GitHub CLI from its apt repository, exactly as
[cli.github.com/…/install_linux.md](https://github.com/cli/cli/blob/trunk/docs/install_linux.md) shows (not
from snap). Then install vigia:

```bash
uv tool install git+https://github.com/hector14mv/vigia
```

## 2. Log in, once, over SSH

Each CLI logs in with your own account. vigia never sees a token.

**GitHub** — `gh auth login --web` prints a one-time code and a URL to open on any device; then
`gh auth setup-git` so `git push` uses that login. ([gh auth login](https://cli.github.com/manual/gh_auth_login))

**Claude Code** — run `claude auth login` (or `claude`, then `/login`). On a server the browser cannot reach the
CLI's local callback, so the sign-in page shows a code: paste it at the `Paste code here if prompted` prompt.
Press `c` to copy the URL if it does not open. Check with `claude auth status` (exit code 0 when logged in).
([authentication](https://code.claude.com/docs/en/authentication),
[troubleshooting SSH logins](https://code.claude.com/docs/en/troubleshoot-install))

- The interactive login refreshes itself and warns three days before it expires. If it lapses, headless
  sessions fail with `OAuth session expired` and vigia parks the work after a few failed attempts.
- For a login that lasts, `claude setup-token` prints a one-year token for subscription plans, which you put in
  `CLAUDE_CODE_OAUTH_TOKEN` (step 3). It prints the token once and saves it nowhere. **Test it over SSH**: the
  docs do not say whether it completes there; if not, run it on your laptop and copy the value.

**Codex CLI** — `codex login --device-auth` prints a code to enter in a browser (enable device code login in
your ChatGPT security settings first). Check with `codex login status`.
([Codex authentication](https://learn.chatgpt.com/docs/auth))

- Copying `~/.codex/auth.json` from your laptop also works, but Codex rotates that token, and its CI guide
  warns against sharing one file between machines. Prefer the device login on the server.

## 3. The repositories and the workspace

```bash
mkdir -p ~/factory && cd ~/factory
git clone https://github.com/you/app && (cd app && vigia init)
git clone https://github.com/you/api && (cd api && vigia init)
vigia workspace          # writes vigia.workspace.toml: these clones and the slots they share
vigia doctor             # in each clone: every check should say ok
```

Secrets for the service go in a file only you can read, which the unit loads:

```bash
mkdir -p ~/.config/vigia && touch ~/.config/vigia/env && chmod 600 ~/.config/vigia/env
# e.g. CLAUDE_CODE_OAUTH_TOKEN=... if you used claude setup-token
```

## 4. Keep `vigia watch` running with systemd

A user unit runs as you, without root, and `loginctl enable-linger` keeps your user's services running after
you log out and starts them at boot. ([loginctl](https://www.freedesktop.org/software/systemd/man/latest/loginctl.html),
[systemd.service](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html))

`~/.config/systemd/user/vigia.service`:

```ini
[Unit]
Description=vigia watch: build, review and merge from GitHub issues

[Service]
Type=simple
WorkingDirectory=%h/factory
EnvironmentFile=-%h/.config/vigia/env
# The user manager's PATH depends on the distribution; claude lives in ~/.local/bin.
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=%h/.local/bin/vigia watch
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
```

`~/.config/systemd/user/vigia-mesa.service` is the same with
`Description=vigia mesa` and `ExecStart=%h/.local/bin/vigia mesa`.

```bash
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now vigia.service vigia-mesa.service
journalctl --user -u vigia -f        # the watcher's output
```

- Stopping the service stops the engine sessions with it (systemd's default `KillMode=control-group`).
- `journalctl --user` keeps logs only if journald is set to store them persistently on your distribution.
- **Test it**: whether `vigia` is at `~/.local/bin/vigia` on your machine (`which vigia` after the uv install)
  and whether the PATH line above is enough for `claude` and `codex` (`systemctl --user status vigia` shows
  the first failures).

## 5. Open the Mesa from your phone with Tailscale

`vigia mesa` listens on 127.0.0.1 only and has no login of its own. Tailscale Serve exposes it to the devices
of your tailnet, and to nobody else. ([Tailscale Serve](https://tailscale.com/kb/1242/tailscale-serve))

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up                     # prints a login URL
sudo tailscale serve --bg 8790        # https://<machine>.<tailnet>.ts.net → 127.0.0.1:8790, survives reboots
tailscale serve status
```

- Serve needs HTTPS certificates enabled for your tailnet (admin console, DNS page); machine names then appear
  in the public certificate-transparency logs. ([HTTPS certificates](https://tailscale.com/kb/1153/enabling-https))
- Never use `tailscale funnel` for the Mesa: Funnel publishes to the whole internet.
- The Mesa answers requests addressed to `*.ts.net`, `localhost` and `127.0.0.1`. Behind another proxy name,
  start it with `vigia mesa --allow-host <name>`.
- **Test it**: whether `serve` needs `sudo` on your machine; `tailscale set --operator=$USER` lets your user run
  it without.

## 6. What the server still needs you for

- T2 approvals: add `vigia:human-ok` on GitHub, from the phone; the Mesa's **Your turn** links to it.
- Parked PRs and refused tickets: the Mesa shows the reason; you decide, then remove `vigia:parked`.
- Logins that expire: `claude auth status` and `codex login status` over SSH, or `vigia doctor`.

The engine sessions run with this user's logins and files. Read [SECURITY.md](../SECURITY.md); for real
isolation give vigia its own OS user (or a container) that holds nothing else.
