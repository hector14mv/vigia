import json
from pathlib import Path

import pytest

from vigia import config as config_mod
from vigia.init import init, preflight
from vigia.proc import Result

OK_CLAUDE = json.dumps({"loggedIn": True})


def make_runner(root: Path, claude: str = OK_CLAUDE, codex: str = "Logged in using ChatGPT"):
    calls: list[list[str]] = []

    def runner(args, **kwargs):
        calls.append(list(args))
        head = list(args[:2])
        if head == ["git", "--version"]:
            return Result(0, "git version 2.40.0\n", "")
        if head == ["git", "rev-parse"]:
            return Result(0, f"{root}\n", "")
        if head == ["gh", "auth"]:
            return Result(0, "", "Logged in")
        if head == ["gh", "repo"]:
            return Result(
                0, json.dumps({"nameWithOwner": "acme/widgets", "defaultBranchRef": {"name": "main"}}), ""
            )
        if head == ["claude", "auth"]:
            return Result(0, claude, "")
        if head == ["codex", "login"]:
            return Result(0, codex, "")
        raise AssertionError(f"unexpected command: {args}")

    runner.calls = calls
    return runner


def no_ask(prompt: str, default: str) -> str:
    raise AssertionError(f"unexpected prompt: {prompt}")


def run_init(tmp_path: Path, runner=None, **kwargs):
    runner = runner or make_runner(tmp_path)
    messages: list[str] = []
    kwargs.setdefault("assume_yes", True)
    kwargs.setdefault("create_labels", False)
    path = init(tmp_path, no_ask, messages.append, runner=runner, **kwargs)
    return path, messages


def test_preflight_all_ok(tmp_path):
    results = preflight(make_runner(tmp_path))
    assert [name for name, _, _ in results] == ["git", "gh logged in", "claude logged in", "codex logged in"]
    assert all(ok for _, ok, _ in results)


def test_preflight_claude_not_logged_in(tmp_path):
    results = dict(
        (name, ok) for name, ok, _ in preflight(make_runner(tmp_path, claude='{"loggedIn": false}'))
    )
    assert results["claude logged in"] is False
    assert results["codex logged in"] is True


def test_preflight_codex_not_logged_in(tmp_path):
    results = dict((name, ok) for name, ok, _ in preflight(make_runner(tmp_path, codex="Not logged in")))
    assert results["codex logged in"] is False
    assert results["claude logged in"] is True


def test_init_writes_loadable_config_with_cross_lab_gates(tmp_path):
    path, _ = run_init(tmp_path)
    assert path == tmp_path / "vigia.toml"
    cfg = config_mod.load(path)
    assert cfg.repo == "acme/widgets"
    for tier in config_mod.TIERS:
        policy = cfg.policy(tier)
        assert policy.gate.family != policy.builder.family
        assert policy.builder.engine == "claude"
        assert policy.gate.engine == "codex"


def test_init_gitignore_once_and_template(tmp_path):
    run_init(tmp_path)
    run_init(tmp_path, force=True)
    lines = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines.count(".vigia/") == 1
    assert (tmp_path / ".github" / "ISSUE_TEMPLATE" / "vigia-ticket.md").read_text(encoding="utf-8")


def test_init_keeps_existing_gitignore_content(tmp_path):
    (tmp_path / ".gitignore").write_text("node_modules", encoding="utf-8")
    run_init(tmp_path)
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines() == ["node_modules", ".vigia/"]


def test_init_refuses_to_overwrite_without_force(tmp_path):
    (tmp_path / "vigia.toml").write_text("keep me", encoding="utf-8")
    with pytest.raises(SystemExit, match="already exists"):
        run_init(tmp_path)
    assert (tmp_path / "vigia.toml").read_text(encoding="utf-8") == "keep me"
    run_init(tmp_path, force=True)
    assert (tmp_path / "vigia.toml").read_text(encoding="utf-8") != "keep me"


@pytest.mark.parametrize(
    "kwargs",
    [{"claude": '{"loggedIn": false}'}, {"codex": "Not logged in"}],
)
def test_init_stops_when_an_engine_is_not_logged_in(tmp_path, kwargs):
    messages: list[str] = []
    with pytest.raises(SystemExit, match="both engines logged in"):
        init(
            tmp_path,
            no_ask,
            messages.append,
            runner=make_runner(tmp_path, **kwargs),
            assume_yes=True,
            create_labels=False,
        )
    assert not (tmp_path / "vigia.toml").exists()
    assert any(m.strip().startswith("NO ") for m in messages)


def test_init_builder_codex_gates_with_claude(tmp_path):
    path, _ = run_init(tmp_path, builder="codex")
    cfg = config_mod.load(path)
    for tier in config_mod.TIERS:
        policy = cfg.policy(tier)
        assert policy.builder.engine == "codex"
        assert policy.gate.engine == "claude"


def test_init_uses_only_the_fake_runner_for_the_clone(tmp_path):
    runner = make_runner(tmp_path)
    run_init(tmp_path, runner=runner)
    assert ["git", "rev-parse", "--show-toplevel"] in runner.calls
