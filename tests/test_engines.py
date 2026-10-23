import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from vigia import proc
from vigia.config import Config, Seat, default_tiers
from vigia.engines import (
    SESSION_ENV,
    SESSION_GH_TOKEN,
    Engines,
    handoff_body,
    handoff_commit,
    parse_verdict,
    session_env,
)
from vigia.proc import Result

CFG = Config(repo="o/r", base="main", tiers=default_tiers())


def test_claude_builder_command(tmp_path):
    cmd = Engines(CFG, tmp_path).command(
        Seat("claude", "claude-sonnet-5-5", "medium"), "builder", tmp_path, tmp_path / "o", None
    )
    assert cmd[:6] == ["claude", "-p", "--model", "claude-sonnet-5-5", "--effort", "medium"]
    assert "acceptEdits" in cmd and cmd[-2:] == ["--output-format", "json"]


def test_codex_reviewer_command_is_read_only_with_schema(tmp_path):
    schema = tmp_path / "s.json"
    schema.write_text("{}")
    cmd = Engines(CFG, tmp_path).command(
        Seat("codex", "gpt-6.1-sol", "high"), "reviewer", tmp_path, tmp_path / "o", schema
    )
    assert cmd[:4] == ["codex", "exec", "-m", "gpt-6.1-sol"]
    assert "model_reasoning_effort=high" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert cmd[cmd.index("--output-schema") + 1] == str(schema)
    assert cmd[-1] == "-"  # prompt on stdin


def test_claude_reviewer_cannot_edit(tmp_path):
    schema = tmp_path / "s.json"
    schema.write_text('{"type": "object"}')
    cmd = Engines(CFG, tmp_path).command(
        Seat("claude", "claude-opus-5-5"), "reviewer", tmp_path, tmp_path / "o", schema
    )
    banned = cmd[cmd.index("--disallowedTools") + 1 :]
    assert "Edit" in banned and "Write" in banned
    assert "--json-schema" in cmd


def _last_denied(cmd: list[str]) -> list[str]:
    start = len(cmd) - cmd[::-1].index("--disallowedTools")
    end = next((i for i in range(start, len(cmd)) if cmd[i].startswith("--")), len(cmd))
    return cmd[start:end]


def test_claude_reviewer_default_has_no_shell(tmp_path):
    cmd = Engines(CFG, tmp_path).command(
        Seat("claude", "claude-opus-5-5"), "reviewer", tmp_path, tmp_path / "o", None
    )
    assert not [a for a in cmd if a.startswith("Bash(")]
    allowed = cmd[cmd.index("--allowedTools") + 1 :]
    assert allowed[: allowed.index("--disallowedTools")] == ["Read", "Glob", "Grep"]
    assert {"Bash", "Edit", "Write", "NotebookEdit"} <= set(_last_denied(cmd))


def test_claude_reviewer_override_cannot_reenable_shell_or_writes(tmp_path):
    override = ["--permission-mode", "acceptEdits", "--allowedTools", "Bash", "Read"]
    cfg = Config(
        repo="o/r",
        base="main",
        tiers=default_tiers(),
        engine_args={"claude": {"reviewer": tuple(override)}},
    )
    schema = tmp_path / "s.json"
    schema.write_text('{"type": "object"}')
    cmd = Engines(cfg, tmp_path).command(
        Seat("claude", "claude-opus-5-5"), "reviewer", tmp_path, tmp_path / "o", schema
    )
    assert cmd[4:9] == override  # the override is passed through ...
    # ... and the forced deny list comes after it, so no allow rule it adds survives
    assert cmd.index("--disallowedTools") > cmd.index("--allowedTools")
    assert {"Bash", "Edit", "Write", "NotebookEdit"} <= set(_last_denied(cmd))
    assert cmd[cmd.index("--json-schema") + 1] == '{"type": "object"}'


def test_claude_builder_keeps_its_shell(tmp_path):
    cmd = Engines(CFG, tmp_path).command(
        Seat("claude", "claude-sonnet-5-5"), "builder", tmp_path, tmp_path / "o", None
    )
    assert "--disallowedTools" not in cmd and "Bash" in cmd


def test_run_reads_codex_final_message_and_claude_json(tmp_path):
    seen = {}

    def runner(cmd, cwd, input, timeout, log, env=None):
        seen.update(cmd=cmd, input=input, timeout=timeout)
        if cmd[0] == "codex":
            out = Path(cmd[cmd.index("-o") + 1])
            out.write_text('{"verdict": "APPROVE", "summary": "ok", "findings": []}')
            return Result(0, "", "")
        return Result(0, json.dumps({"is_error": False, "result": "done"}), "")

    eng = Engines(CFG, tmp_path / "logs", runner=runner)
    out = eng.run(Seat("codex", "gpt-6.1-sol"), "reviewer", "PROMPT", tmp_path, "g1")
    assert out.ok and parse_verdict(out.text)["verdict"] == "APPROVE"
    assert seen["input"] == "PROMPT" and seen["timeout"] == CFG.caps.session_minutes * 60
    out = eng.run(Seat("claude", "claude-sonnet-5-5"), "builder", "P", tmp_path, "b1")
    assert out.ok and out.text == "done"


def test_run_is_not_ok_on_error_or_timeout(tmp_path):
    def err(cmd, cwd, input, timeout, log, env=None):
        return Result(0, json.dumps({"is_error": True, "result": "rate limited"}), "")

    def slow(cmd, cwd, input, timeout, log, env=None):
        return Result(-9, "", "", timed_out=True)

    seat = Seat("claude", "claude-sonnet-5-5")
    assert not Engines(CFG, tmp_path, runner=err).run(seat, "builder", "p", tmp_path, "x").ok
    assert not Engines(CFG, tmp_path, runner=slow).run(seat, "builder", "p", tmp_path, "y").ok


TOKENS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")


@pytest.mark.parametrize("engine", ["claude", "codex"])
@pytest.mark.parametrize("role", ["builder", "reviewer"])
def test_sessions_run_without_the_users_github_login(tmp_path, monkeypatch, engine, role):
    for var in TOKENS:
        monkeypatch.setenv(var, "ghp_secret")
    monkeypatch.setenv("GIT_ASKPASS", "/editor/askpass.sh")
    monkeypatch.setenv("SSH_ASKPASS", "/usr/libexec/ssh-askpass")
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "users-gh"))  # where the user's own login lives
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")  # the engines' own logins pass through
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai")
    root = tmp_path / "work"
    stale = root / ".gh-config-s1"  # an interrupted run's config, logged in: must not survive
    stale.mkdir(parents=True)
    (stale / "hosts.yml").write_text("github.com:\n    oauth_token: ghp_secret\n")
    seen = {}

    def runner(cmd, cwd, input, timeout, log, env=None):
        gh_dir = Path(env["GH_CONFIG_DIR"])
        seen.update(env=env, exists=gh_dir.is_dir(), contents=list(gh_dir.iterdir()))
        return Result(0, json.dumps({"is_error": False, "result": "done"}), "")

    Engines(CFG, tmp_path / "logs", runner=runner, root=root).run(
        Seat(engine, "m"), role, "p", tmp_path, "s1"
    )
    env = seen["env"]
    assert env["GH_TOKEN"] == SESSION_GH_TOKEN  # a sentinel GitHub rejects, never the user's token
    assert not ({*TOKENS, "SSH_ASKPASS"} - {"GH_TOKEN"}) & env.keys()
    assert env["GIT_ASKPASS"] == ""
    assert Path(env["GH_CONFIG_DIR"]).is_relative_to(root)
    assert seen["exists"] and seen["contents"] == []
    assert not list(root.iterdir())  # removed again once the session is over
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    pairs = {
        (env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"])
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert ("credential.helper", "") in pairs
    for var in ("PATH", "HOME", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        assert env[var] == os.environ[var]


def test_session_env_is_an_allowlist(tmp_path):
    base = {
        "PATH": "/bin",
        "LC_ALL": "C",
        "OPENAI_API_KEY": "sk-oai",
        "HTTPS_PROXY": "http://proxy.example:3128",
        "ALL_PROXY": "http://proxy.example:3128",
        "all_proxy": "socks5://proxy.example:1080",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": "Authorization: Basic secret",
        "GIT_SSH_COMMAND": "ssh -i ~/.ssh/github",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "DATABASE_URL": "postgres://prod",
    }
    env = session_env(base, tmp_path)
    assert {"PATH", "LC_ALL", "OPENAI_API_KEY"} <= env.keys()
    for proxy in ("HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        assert env[proxy] == base[proxy]
    assert not {"GIT_SSH_COMMAND", "SSH_AUTH_SOCK", "DATABASE_URL"} & env.keys()
    pairs = {
        (env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"])
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert pairs == {("credential.helper", ""), ("credential.https://github.com.helper", "")}
    assert not {"GH_TOKEN", "GITHUB_TOKEN"} & SESSION_ENV


@pytest.mark.parametrize("url", ["https://example.invalid/o/r.git", "ssh://git@example.invalid/o/r.git"])
def test_git_in_a_session_cannot_reach_any_remote(tmp_path, url):
    env = session_env(_isolated_git(tmp_path, helper=False), tmp_path / "gh")
    out = subprocess.run(["git", "ls-remote", url], capture_output=True, text=True, env=env, timeout=30)
    assert out.returncode != 0 and "not allowed" in out.stderr  # refused before any connection is tried


def _isolated_git(tmp_path: Path, helper: bool = True) -> dict[str, str]:
    """An environment whose global git config holds a credential helper that hands out a password (or nothing)."""
    gitconfig = tmp_path / "gitconfig"
    gitconfig.touch()
    if helper:
        value = "!f() { echo username=me; echo password=ghp_from_helper; }; f"
        subprocess.run(
            ["git", "config", "--file", str(gitconfig), "credential.https://github.com.helper", value],
            check=True,
        )
    keep = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return {**keep, "GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1"}


def _fill(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_git_in_a_session_cannot_borrow_the_users_credential_helper(tmp_path):
    base = _isolated_git(tmp_path)
    assert "password=ghp_from_helper" in _fill(base).stdout  # the helper does work outside a session
    out = _fill(session_env(base, tmp_path / "gh"))
    assert out.returncode != 0 and "ghp_from_helper" not in out.stdout


def _askpass(tmp_path: Path) -> str:
    """A password program that answers every prompt, as an editor's or the keychain's askpass would."""
    script = tmp_path / "askpass.sh"
    script.write_text("#!/bin/sh\necho ghp_from_askpass\n")
    script.chmod(0o755)
    return str(script)


@pytest.mark.parametrize("source", ["core.askPass", "SSH_ASKPASS", "GIT_ASKPASS"])
def test_git_in_a_session_cannot_borrow_an_askpass_program(tmp_path, source):
    base = _isolated_git(tmp_path, helper=False)
    if source == "core.askPass":
        subprocess.run(
            ["git", "config", "--file", base["GIT_CONFIG_GLOBAL"], "core.askPass", _askpass(tmp_path)],
            check=True,
        )
    else:
        base[source] = _askpass(tmp_path)
    assert "password=ghp_from_askpass" in _fill(base).stdout  # the askpass does answer outside a session
    out = _fill(session_env(base, tmp_path / "gh"))
    assert out.returncode != 0 and "ghp_from_askpass" not in out.stdout


def test_git_in_a_session_cannot_borrow_a_helper_from_git_config_parameters(tmp_path):
    base = _isolated_git(tmp_path, helper=False)
    helper = "!f() { echo username=me; echo password=ghp_from_params; }; f"
    base["GIT_CONFIG_PARAMETERS"] = f"'credential.helper={helper}' 'user.name=Me'"
    assert "password=ghp_from_params" in _fill(base).stdout  # git -c's helper works outside a session
    out = _fill(session_env(base, tmp_path / "gh"))
    assert out.returncode != 0 and "ghp_from" not in out.stdout


@pytest.mark.skipif(shutil.which("gh") is None, reason="gh is not installed")
def test_gh_in_a_session_is_logged_out(tmp_path):
    (tmp_path / "gh").mkdir()
    env = session_env({**os.environ, "GH_TOKEN": "ghp_secret"}, tmp_path / "gh")
    out = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, env=env, timeout=30)
    assert out.returncode != 0
    # gh would fall back to the OS keyring for the token; the sentinel in GH_TOKEN comes first
    token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, env=env, timeout=30)
    assert token.stdout.strip() in ("", SESSION_GH_TOKEN)


def test_the_session_token_is_the_sentinel_never_the_users():
    env = session_env({"PATH": "/bin", "GH_TOKEN": "ghp_secret", "GITHUB_TOKEN": "ghp_other"}, Path("/tmp/x"))
    assert env["GH_TOKEN"] == SESSION_GH_TOKEN and "GITHUB_TOKEN" not in env


def test_run_session_uses_the_given_environment_whole(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_secret")
    env = {"PATH": os.environ["PATH"], "ONLY": "this"}
    result = proc.run_session(
        ["sh", "-c", 'printf "%s|%s" "$ONLY" "${GH_TOKEN-unset}"'],
        cwd=tmp_path,
        input="",
        timeout=30,
        log=tmp_path / "s.log",
        env=env,
    )
    assert result.stdout == "this|unset"


def test_parse_verdict_accepts_fenced_json_and_counts():
    text = (
        '```json\n{"verdict": "CHANGES", "summary": "s", "findings": ['
        '{"severity": "P1", "file": "a.py", "line": "3", "title": "t", "scenario": "x"},'
        '{"severity": "P2", "file": "b.py", "line": 1, "title": "u", "scenario": ""}]}\n```'
    )
    v = parse_verdict(text)
    assert (v["verdict"], v["p1"], v["p2"]) == ("CHANGES", 1, 1)
    assert v["findings"][0]["line"] == 3


def test_parse_verdict_approve_with_p1_becomes_changes():
    text = json.dumps(
        {
            "verdict": "APPROVE",
            "summary": "",
            "findings": [{"severity": "P1", "file": "a", "line": 1, "title": "t", "scenario": "s"}],
        }
    )
    assert parse_verdict(text)["verdict"] == "CHANGES"


def test_no_verdict_is_none():
    for text in ["", "Looks good to me!", '{"verdict": "LGTM"}', "{not json}"]:
        assert parse_verdict(text) is None


def test_handoff_commit_line():
    notes = 'Commit: "feat: add truncate()"\n## Summary\n- x'
    assert handoff_commit(notes, "fallback") == "feat: add truncate()"
    assert handoff_commit("## Summary", "fallback") == "fallback"
    assert handoff_body(notes).startswith("## Summary")
    long = "Commit: fix: keep ledger events after a torn tail, escape TOML control characters, keep fenced content"
    subject = handoff_commit(long, "fallback")
    assert subject == "fix: keep ledger events after a torn tail, escape TOML control"
    dangling = "Commit: fix: keep approvals and reviews tied to the right head, base and enrollment state"
    assert handoff_commit(dangling, "x") == "fix: keep approvals and reviews tied to the right head, base"
