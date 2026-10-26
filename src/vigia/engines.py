"""Engines: Claude Code (`claude -p`), Codex CLI (`codex exec`) and OpenCode (`opencode run`), run headless.

A builder session may edit files and run commands inside its worktree. A reviewer session only reads.
Both return their final message; reviewers must return the verdict JSON described by `VERDICT_SCHEMA`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import jobs, proc, safety
from .config import DEFAULT_ENGINE_ARGS, Config, Seat

SessionRunner = Callable[..., proc.Result]

# An engine session inherits only these variables (an allowlist: a denylist is the list that comes up one short).
# GitHub tokens, SSH agents and askpass programs, and every GIT_* setting but where git's own config files live,
# never reach it. Everything an engine needs to run and log in to its own lab is here.
SESSION_ENV = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "COLORTERM",
        "LANG",
        "TZ",
        "TMPDIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "all_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "VIRTUAL_ENV",
        "PYENV_ROOT",
        "NVM_DIR",
        "GOPATH",
        "GOROOT",
        "CARGO_HOME",
        "RUSTUP_HOME",
        "JAVA_HOME",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_NOSYSTEM",
    }
)
# No `OPENCODE_` here: a variable such as OPENCODE_CONFIG or OPENCODE_CONFIG_CONTENT from the user's shell would bring
# back the servers or permissions vigia turns off. vigia sets the ones a session needs (`opencode_env`), and
# OpenCode logs in with its own credential store (`opencode providers login`), not a provider-key variable.
SESSION_ENV_PREFIXES = ("LC_", "XDG_", "ANTHROPIC_", "CLAUDE_", "CODEX_", "OPENAI_", "UV_", "PIP_")
# What `gh` sees as its token inside a session: not a credential, and GitHub answers it with "Bad credentials".
SESSION_GH_TOKEN = "vigia-engine-session-has-no-github-access"
# Command-scope git config outranks every config file: an empty helper clears the helpers set before it
# (`gh auth git-credential`, the macOS keychain), so `git credential fill` hands out nothing.
_NO_CREDENTIAL_HELPER = (("credential.helper", ""), ("credential.https://github.com.helper", ""))

# OpenCode has no sandbox flag. A reviewer gets these deny rules in its inline config, at the top level and for every
# agent the resolved config defines, because an agent's own `permission` table outranks the top-level one (checked on
# 1.18.32: a user agent with `bash: allow` keeps its shell under a top-level deny).
OPENCODE_REVIEWER_DENY = {"edit": "deny", "bash": "deny", "webfetch": "deny"}
# A reviewer's tools are an allowlist: `*` denies everything else, including custom tools from
# ~/.config/opencode/tools, which 1.18.32 loads even under --pure and OPENCODE_DISABLE_PROJECT_CONFIG=1 (checked with
# `opencode debug agent build`: a denied tool leaves the agent's tool list). The last matching rule wins, so these
# come after `*`; reading a .env file stays off, as nobody is there to answer OpenCode's default `ask`.
OPENCODE_REVIEWER_READ = {
    "read": {"*": "allow", "*.env": "deny", "*.env.*": "deny", "*.env.example": "allow"},
    "grep": "allow",
    "glob": "allow",
    "list": "allow",
}
# Options vigia sets itself on `opencode run`: an override that names one is dropped, with its value. `--share`
# publishes the session, `--attach` runs it on a server whose config vigia never set, `--no-pure` loads plugins,
# `--command` runs a user's command template (its own model, and `!` shell lines no permission rule sees),
# and the model, effort, output format and directory must be the seat's and vigia's. True: takes a value.
_OPENCODE_FIXED = {
    "--share": False,
    "--no-share": False,
    "--pure": False,
    "--no-pure": False,
    "--attach": True,
    "--command": True,
    "--format": True,
    "-m": True,
    "--model": True,
    "--variant": True,
    "--dir": True,
}
# How long `opencode debug config` may take before the session is refused.
_OPENCODE_CONFIG_SECONDS = 120

VERDICT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "summary", "findings"],
    "properties": {
        "verdict": {"type": "string", "enum": ["APPROVE", "CHANGES"]},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "file", "line", "title", "scenario"],
                "properties": {
                    "severity": {"type": "string", "enum": ["P1", "P2"]},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "title": {"type": "string"},
                    "scenario": {"type": "string"},
                },
            },
        },
    },
}


@dataclass
class Outcome:
    ok: bool
    text: str  # the session's final message
    code: int
    timed_out: bool
    seconds: float
    log: Path


class Engines:
    def __init__(
        self,
        cfg: Config,
        logs: Path,
        runner: SessionRunner = proc.run_session,
        root: Path | None = None,
    ):
        self.cfg = cfg
        self.logs = logs
        self.root = root or logs  # vigia's work root: each session's empty `gh` config lives under it
        self._run = runner

    def command(
        self,
        seat: Seat,
        role: str,
        workdir: Path,
        out_file: Path,
        schema_file: Path | None,
        guards: Mapping[str, str] | None = None,
    ) -> list[str]:
        extra = list(
            self.cfg.engine_args.get(seat.engine, {}).get(role, DEFAULT_ENGINE_ARGS[seat.engine][role])
        )
        if role == "reviewer":  # whatever the overrides say, a reviewer never gets write access
            if seat.engine == "codex":
                extra = [
                    a
                    for i, a in enumerate(extra)
                    if a != "--sandbox" and (i == 0 or extra[i - 1] != "--sandbox")
                ]
                extra += ["--sandbox", "read-only"]
            elif seat.engine == "claude":  # OpenCode's deny rules travel in its environment (`opencode_env`)
                # a deny rule beats any allow rule, even under bypassPermissions: no shell, no writes
                extra += ["--disallowedTools", "Bash", "Edit", "Write", "NotebookEdit"]
        if seat.engine == "claude":
            # no session reaches the user's MCP servers (mail, calendar, browser...), whatever the overrides say:
            # strict mode loads only `--mcp-config` servers, and no `--mcp-config` survives
            extra = _without_option(extra, "--mcp-config") + ["--strict-mcp-config"]
            cmd = ["claude", "-p", "--model", seat.model]
            if seat.effort:
                cmd += ["--effort", seat.effort]
            cmd += extra + ["--output-format", "json"]
            if schema_file is not None:
                cmd += ["--json-schema", schema_file.read_text(encoding="utf-8")]
            return cmd
        if seat.engine == "codex":
            cmd = ["codex", "exec", "-m", seat.model]
            if seat.effort:
                cmd += ["-c", f"model_reasoning_effort={seat.effort}"]
            cmd += extra + ["-C", str(workdir), "-o", str(out_file)]
            if guards:
                # Codex builds the environment of the commands it runs from its own policy (a user's config may
                # keep only "core" variables), which would drop the session's guards: pass everything the session
                # has, and set the guards on top so they reach every command whatever the config says.
                table = ", ".join(f"{k} = {json.dumps(v)}" for k, v in guards.items())
                cmd += [
                    "-c",
                    'shell_environment_policy.inherit="all"',
                    "-c",
                    "shell_environment_policy.include_only=[]",  # a user filter would drop the guards again
                    "-c",
                    "shell_environment_policy.exclude=[]",
                    "-c",
                    f"shell_environment_policy.set={{{table}}}",
                ]
            if schema_file is not None:
                cmd += ["--output-schema", str(schema_file)]
            return cmd + ["-"]  # the prompt comes on stdin
        if seat.engine == "opencode":
            # The prompt comes on stdin (1.18.32 reads it, and refuses an empty one). Permissions, MCP servers and
            # sharing are set through the environment (`opencode_env`), so a reviewer's read-only rule does not
            # depend on these arguments; the reviewer prompt asks for the verdict JSON, as there is no schema flag.
            cmd = ["opencode", "run", "--pure", "-m", seat.model]
            if seat.effort:
                cmd += ["--variant", seat.effort]
            return cmd + _without_flags(extra, _OPENCODE_FIXED) + ["--format", "json", "--dir", str(workdir)]
        raise ValueError(f"unknown engine {seat.engine!r}")

    def run(
        self, seat: Seat, role: str, prompt: str, workdir: Path, name: str, schema: dict | None = None
    ) -> Outcome:
        """Run one session to completion (or to the session time cap) and return its final message."""
        self.logs.mkdir(parents=True, exist_ok=True)
        out_file = self.logs / f"{name}.last.txt"
        out_file.unlink(missing_ok=True)
        schema_file = None
        if role == "reviewer":
            schema_file = self.logs / f"{name}.schema.json"
            schema_file.write_text(json.dumps(schema or VERDICT_SCHEMA), encoding="utf-8")
        gh_config = self.root / f".gh-config-{name}"
        cmd = self.command(seat, role, workdir, out_file, schema_file, guards=session_guards(gh_config))
        safety.remove(gh_config, self.root)  # an interrupted run's: start logged out, never from its state
        gh_config.mkdir(parents=True)
        started = time.monotonic()
        held = jobs.held()
        log = self.logs / f"{name}.log"
        try:
            env = session_env(os.environ, gh_config)
            if seat.engine == "opencode":
                try:
                    env = opencode_env(env, role, workdir)
                except (
                    OpencodeConfigError
                ) as exc:  # the session does not start without its servers turned off
                    log.write_text(f"vigia: OpenCode session not started: {exc}\n", encoding="utf-8")
                    return Outcome(False, "", 1, False, round(time.monotonic() - started, 1), log)
            result = self._run(
                cmd,
                cwd=workdir,
                input=prompt,
                timeout=self.cfg.caps.session_minutes * 60,
                log=log,
                env=env,
                **({"keep_fds": held} if held else {}),
            )
        finally:
            safety.remove(gh_config, self.root)
        seconds = round(time.monotonic() - started, 1)
        text = _final_text(seat.engine, result.stdout, out_file)
        ok = result.code == 0 and not result.timed_out and bool(text.strip())
        if seat.engine == "claude":
            ok = ok and not _claude_error(result.stdout)
        if seat.engine == "opencode":
            ok = ok and not _opencode_error(result.stdout)
        return Outcome(ok, text, result.code, result.timed_out, seconds, log)


class OpencodeConfigError(RuntimeError):
    """`opencode debug config` failed or printed something vigia cannot read: the session must not start."""


def opencode_config(env: Mapping[str, str], workdir: Path) -> dict:
    """OpenCode's resolved config in `workdir`, read with `env` as the whole environment (the session's own, before
    vigia adds its inline config). Raises OpencodeConfigError when the read fails or does not parse."""
    try:
        out = subprocess.run(
            ["opencode", "--pure", "debug", "config"],
            cwd=workdir,
            input="",
            capture_output=True,
            text=True,
            timeout=_OPENCODE_CONFIG_SECONDS,
            env=dict(env),
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise OpencodeConfigError("opencode: command not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise OpencodeConfigError(
            f"`opencode debug config` timed out after {_OPENCODE_CONFIG_SECONDS}s"
        ) from exc
    if out.returncode != 0:
        # stderr only: stdout is the resolved config, which may hold provider keys or a server's environment
        tail = " | ".join(out.stderr.strip().splitlines()[-3:])
        raise OpencodeConfigError(f"`opencode debug config` exited {out.returncode}: {tail}")
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError as exc:
        raise OpencodeConfigError("`opencode debug config` did not print a JSON object") from exc
    if not isinstance(data, dict):
        raise OpencodeConfigError("`opencode debug config` did not print a JSON object")
    for key in ("mcp", "agent", "mode"):
        if not isinstance(data.get(key, {}), dict):
            raise OpencodeConfigError(f"`opencode debug config`: {key!r} is not a table")
    return data


def opencode_inline_config(resolved: Mapping, role: str) -> dict:
    """The OPENCODE_CONFIG_CONTENT of a session: OpenCode merges it over every other config, so it cannot empty the
    `mcp` table, but it can turn off each server by name. Sharing is off for everyone; a reviewer also gets
    `opencode_reviewer_permission` at the top level and for every agent and mode the resolved config names."""
    inline: dict = {
        "share": "disabled",
        "mcp": {name: {"enabled": False} for name in resolved.get("mcp", {})},
    }
    if role == "reviewer":
        agents = {*resolved.get("agent", {}), *resolved.get("mode", {})}
        permission = opencode_reviewer_permission(resolved)
        inline["permission"] = permission
        inline["agent"] = {name: {"permission": dict(permission)} for name in sorted(agents)}
        inline["mode"] = {name: {"permission": dict(permission)} for name in resolved.get("mode", {})}
    return inline


def opencode_reviewer_permission(resolved: Mapping) -> dict:
    """A reviewer's permission table: `*` denied, every tool the resolved config names in a `permission` or `tools`
    table (top level, agents, modes) denied by name, so a user's named allow keeps no place after the `*` whatever
    order the merge leaves, then `OPENCODE_REVIEWER_DENY` and the read-only allowlist."""
    named: set[str] = set()
    for table in (resolved, *_tables(resolved.get("agent")), *_tables(resolved.get("mode"))):
        for key in ("permission", "tools"):
            if isinstance(table.get(key), dict):
                named.update(table[key])
    named -= {"*", *OPENCODE_REVIEWER_DENY, *OPENCODE_REVIEWER_READ}
    return {
        "*": "deny",
        **{name: "deny" for name in sorted(named)},
        **OPENCODE_REVIEWER_DENY,
        **OPENCODE_REVIEWER_READ,
    }


def _tables(section: object) -> list[dict]:
    """The agent or mode tables of a resolved config section that are tables."""
    return [t for t in section.values() if isinstance(t, dict)] if isinstance(section, dict) else []


def opencode_env(env: Mapping[str, str], role: str, workdir: Path) -> dict[str, str]:
    """An OpenCode session's environment: `env` (the session allowlist and guards, which hold no OPENCODE_* from the
    user), no project config from the worktree, and the inline config built from what OpenCode would load."""
    out = {k: v for k, v in env.items() if not k.startswith("OPENCODE_")}
    out["OPENCODE_DISABLE_PROJECT_CONFIG"] = (
        "1"  # a worktree's opencode.json and .opencode/ (agents, servers)
    )
    out["OPENCODE_CONFIG_CONTENT"] = json.dumps(opencode_inline_config(opencode_config(out, workdir), role))
    return out


def session_guards(gh_config: Path) -> dict[str, str]:
    """The variables that keep a session off the user's GitHub login. They go into the session's environment and,
    for Codex, into the environment of every command it runs (`Engines.command`)."""
    guards = {
        "GH_CONFIG_DIR": str(gh_config),  # an empty config: no account, no settings
        # gh falls back to the OS keyring for a host's token even with an empty config (`gh auth token` would print
        # the user's). A token in the environment comes first, so a sentinel GitHub rejects closes that path, for
        # github.com (GH_TOKEN) and for GitHub Enterprise hosts, which read their own variables.
        "GH_TOKEN": SESSION_GH_TOKEN,
        "GH_ENTERPRISE_TOKEN": SESSION_GH_TOKEN,
        "GITHUB_ENTERPRISE_TOKEN": SESSION_GH_TOKEN,
        "GIT_ALLOW_PROTOCOL": "file",  # git may reach no remote: no https, ssh or git transport at all
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",  # set but empty: git runs no askpass program (it is asked before core.askPass)
        "GIT_CONFIG_COUNT": str(len(_NO_CREDENTIAL_HELPER)),
    }
    for i, (key, value) in enumerate(_NO_CREDENTIAL_HELPER):
        guards[f"GIT_CONFIG_KEY_{i}"] = key
        guards[f"GIT_CONFIG_VALUE_{i}"] = value
    return guards


def session_env(base: Mapping[str, str], gh_config: Path) -> dict[str, str]:
    """The environment of an engine session: what it needs to run, and no way to act as the user on GitHub.

    Only vigia's own code labels, comments, pushes and merges. The session gets an allowlist of `base`
    (`SESSION_ENV`), a logged-out `gh` (an empty config directory), no credential helper or askpass program for
    git, and `GIT_ALLOW_PROTOCOL=file`, so git refuses every network transport (https, ssh, git) whatever
    credentials or headers a config file holds.

    This stops a session from using your GitHub login by accident or by following an instruction it read. It is
    not a sandbox against a process determined to misbehave: the session runs as your user and could, for
    example, read credential stores itself. For that, run vigia under a separate OS user or in a container.
    """
    env = {k: v for k, v in base.items() if k in SESSION_ENV or k.startswith(SESSION_ENV_PREFIXES)}
    env.update(session_guards(gh_config))
    return env


def _without_option(args: list[str], option: str) -> list[str]:
    """`args` without `option`, in either spelling (`--opt=v` or `--opt v1 v2 ...`), and every value it takes:
    Claude's list options take values up to the next argument that starts with `-`."""
    out: list[str] = []
    dropping = False
    for a in args:
        if a == option or a.startswith(option + "="):
            dropping = a == option
            continue
        if dropping and not a.startswith("-"):
            continue
        dropping = False
        out.append(a)
    return out


def _without_flags(args: list[str], flags: Mapping[str, bool]) -> list[str]:
    """`args` without any option in `flags`, in either spelling (`--opt=v` or `--opt v`), with the value it takes
    (`flags[opt]` is True) or an explicit `true`/`false` after a boolean one."""
    out: list[str] = []
    skip: str | None = None  # "value": drop the next argument; "bool": drop it only if it is true/false
    for a in args:
        if skip == "value" or (skip == "bool" and a in ("true", "false")):
            skip = None
            continue
        skip = None
        name, eq, _ = a.partition("=")
        if name in flags:
            if not eq:
                skip = "value" if flags[name] else "bool"
            continue
        out.append(a)
    return out


def _opencode_events(stdout: str) -> list[dict]:
    """The JSON events `opencode run --format json` prints, one per line; a line that is not one is skipped."""
    events = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _opencode_error(stdout: str) -> bool:
    """An `error` event (1.18.32 prints one for a provider or auth error, and exits 1) fails the session."""
    return any(e.get("type") == "error" for e in _opencode_events(stdout))


def _opencode_text(stdout: str) -> str:
    """The final message: the text parts of the message the last event belongs to, once that message has finished
    a step that did not stop for tool calls. Text from earlier steps is not the answer, so a final message with no
    text, or one that never finished, gives no text at all."""
    parts = [
        e["part"]
        for e in _opencode_events(stdout)
        if isinstance(e.get("part"), dict) and e["part"].get("messageID")
    ]
    if not parts:
        return ""
    last = parts[-1]["messageID"]
    final = [p for p in parts if p["messageID"] == last]
    finishes = [p for p in final if p.get("type") == "step-finish"]
    if not finishes or finishes[-1].get("reason") == "tool-calls":
        return ""
    return "\n".join(p["text"] for p in final if p.get("type") == "text" and isinstance(p.get("text"), str))


def _claude_error(stdout: str) -> bool:
    try:
        return bool(json.loads(stdout).get("is_error"))
    except (json.JSONDecodeError, AttributeError):
        return False


def _final_text(engine: str, stdout: str, out_file: Path) -> str:
    if engine == "opencode":
        return _opencode_text(stdout)
    if engine == "claude":
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return stdout
        if isinstance(data, dict):
            if isinstance(data.get("structured_output"), dict):
                return json.dumps(data["structured_output"])
            return str(data.get("result") or "")
        return stdout
    if out_file.exists():
        return out_file.read_text(encoding="utf-8")
    return stdout


def parse_verdict(text: str) -> dict | None:
    """The reviewer's verdict as {'verdict', 'summary', 'findings', 'p1', 'p2'}, or None if there is none.

    No parsable verdict means GATE_UNAVAILABLE: content, never presence, counts as a review.
    """
    if not text:
        return None
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(body[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("verdict") not in ("APPROVE", "CHANGES"):
        return None
    findings = []
    for f in data.get("findings") or []:
        if not isinstance(f, dict) or f.get("severity") not in ("P1", "P2"):
            continue
        try:
            line = int(f.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        findings.append(
            {
                "severity": f["severity"],
                "file": str(f.get("file") or "?"),
                "line": line,
                "title": str(f.get("title") or "").strip(),
                "scenario": str(f.get("scenario") or "").strip(),
            }
        )
    p1 = sum(1 for f in findings if f["severity"] == "P1")
    verdict = data["verdict"] if p1 == 0 else "CHANGES"
    return {
        "verdict": verdict,
        "summary": str(data.get("summary") or "").strip(),
        "findings": findings,
        "p1": p1,
        "p2": len(findings) - p1,
    }


_HANDOFF_COMMIT = re.compile(r"^\s*Commit:\s*(.+?)\s*$", re.M)


def handoff_commit(text: str, fallback: str) -> str:
    """The commit subject the builder proposed in its handoff file, or `fallback`."""
    m = _HANDOFF_COMMIT.search(text or "")
    subject = m.group(1).strip().strip("`\"'") if m else ""
    if len(subject) > 72:  # keep git's one-line convention without cutting a word in half
        words = subject[:72].rsplit(" ", 1)[0].rstrip(",;:").split(" ")
        while len(words) > 3 and words[-1].lower() in {
            "and",
            "or",
            "the",
            "a",
            "an",
            "of",
            "to",
            "with",
            "for",
        }:
            words.pop()
        subject = " ".join(words).rstrip(",;:")
    return subject or fallback


def handoff_body(text: str) -> str:
    """The handoff file without its Commit: line."""
    return _HANDOFF_COMMIT.sub("", text or "", count=1).strip()
