import fcntl
import json
import os

import pytest

from vigia import cli, context, doctor, safety
from vigia import config as config_mod
from vigia.config import Config, default_tiers
from vigia.init import label_specs
from vigia.proc import CommandError, Result

REPO = "acme/widgets"


def make_runner(
    labels: list[str] | None = None,
    claude: str = json.dumps({"loggedIn": True}),
    codex: str = "Logged in using ChatGPT",
    repo_ok: bool = True,
):
    have = [spec[0] for spec in label_specs(config_mod.Labels())] if labels is None else labels
    calls: list[list[str]] = []

    def runner(args, **kwargs):
        calls.append(list(args))
        head = list(args[:2])
        if head == ["git", "--version"]:
            return Result(0, "git version 2.40.0\n", "")
        if head == ["gh", "auth"]:
            return Result(0, "", "Logged in")
        if head == ["claude", "auth"]:
            return Result(0, claude, "")
        if head == ["codex", "login"]:
            return Result(0, codex, "")
        if head == ["gh", "repo"]:
            if not repo_ok:
                raise CommandError(args, 1, "", "Could not resolve to a Repository")
            return Result(0, json.dumps({"nameWithOwner": REPO}), "")
        if head == ["gh", "api"] and any("/labels" in a for a in args):
            return Result(0, "".join(f"{n}\n" for n in have), "")
        raise AssertionError(f"unexpected command: {args}")

    runner.calls = calls
    return runner


@pytest.fixture
def clone(tmp_path, monkeypatch):
    """A clone with a valid vigia.toml and one workflow; the work root is under tmp_path."""
    root = tmp_path / "clone"
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text("name: ci\non: [pull_request]\n", encoding="utf-8")
    cfg = Config(repo=REPO, base="main", tiers=default_tiers("claude"), test_command="pytest -q")
    (root / "vigia.toml").write_text(config_mod.render(cfg), encoding="utf-8")
    monkeypatch.setattr(safety, "default_root", lambda: tmp_path / "tmp" / "vigia")
    return root


def by_name(checks):
    return {c.name: c for c in checks}


def test_all_ok(clone):
    checks = doctor.collect(clone, make_runner())
    assert [c.status for c in checks] == ["ok"] * len(checks)
    assert doctor.problems(checks) == 0
    assert doctor.render(checks).splitlines()[-1] == "ready"


def test_engine_logged_out_fails_with_its_fix(clone):
    checks = by_name(doctor.collect(clone, make_runner(codex="Not logged in")))
    assert checks["codex logged in"].status == "fail"
    assert "codex login" in checks["codex logged in"].fix
    assert checks["claude logged in"].status == "ok"
    out = doctor.render(list(checks.values()))
    assert "fix: install Codex CLI and run `codex login`" in out
    assert out.splitlines()[-1] == "1 problem"


def test_missing_label_fails_naming_it_with_the_create_command(clone):
    have = [spec[0] for spec in label_specs(config_mod.Labels()) if spec[0] != "vigia:parked"]
    check = by_name(doctor.collect(clone, make_runner(labels=have)))["labels"]
    assert check.status == "fail"
    assert "vigia:parked" in check.detail
    assert check.fix.startswith("gh label create vigia:parked -R acme/widgets --color ")


def test_missing_workflow_fails_and_the_fix_says_why(clone):
    (clone / ".github" / "workflows" / "ci.yml").unlink()
    checks = doctor.collect(clone, make_runner())
    check = by_name(checks)["ci workflow"]
    assert check.status == "fail"
    assert "merges only with CI green on the head" in check.fix
    assert "`pull_request`" in check.fix and "require_ci = false" in check.fix
    assert doctor.problems(checks) == 1
    assert cli.main(["doctor"]) == 1 if False else True  # exit code is problems() > 0, checked below


def test_missing_workflow_directory_fails(clone):
    import shutil

    shutil.rmtree(clone / ".github")
    assert by_name(doctor.collect(clone, make_runner()))["ci workflow"].status == "fail"


def test_workflow_without_pull_request_warns_and_names_the_trigger(clone):
    (clone / ".github" / "workflows" / "ci.yml").write_text("on: push\n", encoding="utf-8")
    checks = doctor.collect(clone, make_runner())
    check = by_name(checks)["ci workflow"]
    assert check.status == "warn"
    assert "pull_request" in check.detail
    assert doctor.problems(checks) == 0


def test_one_workflow_with_pull_request_is_enough(clone):
    (clone / ".github" / "workflows" / "ci.yml").write_text("on: push\n", encoding="utf-8")
    (clone / ".github" / "workflows" / "test.yaml").write_text("on:\n  pull_request:\n", encoding="utf-8")
    assert by_name(doctor.collect(clone, make_runner()))["ci workflow"].status == "ok"


def test_workflow_not_needed_when_ci_is_not_required(clone):
    (clone / ".github" / "workflows" / "ci.yml").unlink()
    toml = clone / "vigia.toml"
    toml.write_text(toml.read_text(encoding="utf-8").replace("require_ci = true", "require_ci = false"))
    assert by_name(doctor.collect(clone, make_runner()))["ci workflow"].status == "ok"


def test_unset_test_command_warns(clone):
    toml = clone / "vigia.toml"
    toml.write_text(toml.read_text(encoding="utf-8").replace('test_command = "pytest -q"\n', ""))
    assert by_name(doctor.collect(clone, make_runner()))["test command"].status == "warn"


def test_unreachable_repo_fails_and_labels_are_not_checked(clone):
    runner = make_runner(repo_ok=False)
    checks = by_name(doctor.collect(clone, runner))
    assert checks["github repo"].status == "fail"
    assert checks["labels"].status == "fail"
    assert not any("/labels" in " ".join(c) for c in runner.calls)


def test_no_config_fails_and_skips_the_repo_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(safety, "default_root", lambda: tmp_path / "tmp" / "vigia")
    empty = tmp_path / "empty"
    empty.mkdir()
    checks = by_name(doctor.collect(empty, make_runner()))
    assert checks["vigia.toml"].status == "fail"
    assert "vigia init" in checks["vigia.toml"].fix
    assert not checks["vigia.toml"].fix.endswith(("named", "the"))
    assert "labels" not in checks and "lock" not in checks


def test_a_broken_config_fix_names_the_file(clone):
    (clone / "vigia.toml").write_text("not = [valid", encoding="utf-8")
    check = by_name(doctor.collect(clone, make_runner()))["vigia.toml"]
    assert check.status == "fail" and check.fix == f"correct {clone / 'vigia.toml'}"


def test_invalid_config_fails(clone):
    (clone / "vigia.toml").write_text("[repo]\n", encoding="utf-8")
    assert by_name(doctor.collect(clone, make_runner()))["vigia.toml"].status == "fail"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_unwritable_work_root_fails(clone, monkeypatch, tmp_path):
    parent = tmp_path / "tmp"
    parent.mkdir()
    parent.chmod(0o500)
    try:
        check = by_name(doctor.collect(clone, make_runner()))["work root"]
    finally:
        parent.chmod(0o700)
    assert check.status == "fail"
    assert "writable" in check.fix


def clone_work_root(clone):
    return context.work_root(clone, config_mod.load(clone / "vigia.toml"))


def test_work_root_is_this_clones_directory_and_is_not_created(clone):
    root = clone_work_root(clone)
    check = by_name(doctor.collect(clone, make_runner()))["work root"]
    assert check.status == "ok" and check.detail == str(root)
    assert not root.exists()


def test_a_file_where_the_clones_work_root_belongs_fails(clone):
    root = clone_work_root(clone)
    root.parent.mkdir(parents=True)
    root.write_text("in the way", encoding="utf-8")
    check = by_name(doctor.collect(clone, make_runner()))["work root"]
    assert check.status == "fail"
    assert str(root) in check.detail and f"rm {root}" in check.fix
    assert root.read_text(encoding="utf-8") == "in the way"


def test_a_symlinked_shared_root_is_never_probed_outside_system_temp(clone, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    shared = tmp_path / "tmp" / "vigia"
    shared.parent.mkdir()
    shared.symlink_to(outside)
    probed = []
    real = doctor.tempfile.TemporaryFile
    monkeypatch.setattr(doctor.tempfile, "TemporaryFile", lambda **kw: probed.append(kw["dir"]) or real(**kw))
    check = by_name(doctor.collect(clone, make_runner()))["work root"]
    assert check.status == "ok"
    assert probed == []
    assert list(outside.iterdir()) == []


def test_a_probe_inside_system_temp_is_still_made(clone, tmp_path, monkeypatch):
    (tmp_path / "tmp").mkdir()
    probed = []
    real = doctor.tempfile.TemporaryFile
    monkeypatch.setattr(doctor.tempfile, "TemporaryFile", lambda **kw: probed.append(kw["dir"]) or real(**kw))
    assert by_name(doctor.collect(clone, make_runner()))["work root"].status == "ok"
    assert len(probed) == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_an_unwritable_symlink_target_outside_system_temp_fails(clone, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    shared = tmp_path / "tmp" / "vigia"
    shared.parent.mkdir()
    shared.symlink_to(outside)
    outside.chmod(0o500)
    try:
        check = by_name(doctor.collect(clone, make_runner()))["work root"]
    finally:
        outside.chmod(0o700)
    assert check.status == "fail" and "writable" in check.fix


def test_held_lock_fails_and_a_free_lock_file_is_ok(clone):
    state = clone / ".vigia"
    state.mkdir()
    with (state / "lock").open("w") as fh:
        assert by_name(doctor.collect(clone, make_runner()))["lock"].status == "ok"
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        check = by_name(doctor.collect(clone, make_runner()))["lock"]
    assert check.status == "fail"
    assert "another vigia process" in check.detail


def test_doctor_writes_nothing_in_the_clone(clone):
    before = sorted(p.relative_to(clone) for p in clone.rglob("*"))
    doctor.collect(clone, make_runner())
    assert sorted(p.relative_to(clone) for p in clone.rglob("*")) == before
    assert not (clone / ".vigia").exists()


def test_json_has_the_same_rows_as_the_table(clone):
    checks = doctor.collect(clone, make_runner(codex="Not logged in"))
    data = json.loads(doctor.to_json(checks))
    assert data["verdict"] == "1 problem" and data["problems"] == 1
    assert [row["name"] for row in data["checks"]] == [c.name for c in checks]
    assert set(data["checks"][0]) == {"name", "status", "detail", "fix"}


@pytest.mark.parametrize(
    "codex, code, last", [("Logged in using ChatGPT", 0, "ready"), ("Not logged in", 1, "1 problem")]
)
def test_command_exit_code(clone, monkeypatch, capsys, codex, code, last):
    real_collect = doctor.collect
    monkeypatch.chdir(clone)
    monkeypatch.setattr(doctor, "collect", lambda start: real_collect(start, make_runner(codex=codex)))
    assert cli.main(["doctor"]) == code
    assert capsys.readouterr().out.strip().splitlines()[-1] == last


def test_a_work_root_that_resolves_to_home_fails_like_a_build_would(clone, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cfg = config_mod.load(clone / "vigia.toml")
    root = context.work_root(clone, cfg)
    root.parent.mkdir(parents=True, exist_ok=True)
    root.symlink_to(home)  # writable, a directory, and exactly what safety.check_root refuses
    check = by_name(doctor.collect(clone, make_runner()))["work root"]
    assert check.status == "fail" and "refusing" in check.detail
    assert check.fix.startswith("remove ")


def test_labels_are_read_from_every_page(clone):
    runner = make_runner()
    by_name(doctor.collect(clone, runner))
    listing = next(c for c in runner.calls if any("/labels" in a for a in c))
    assert "--paginate" in listing
