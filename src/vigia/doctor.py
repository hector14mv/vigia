"""`vigia doctor`: can vigia work in this clone? Read-only: it creates, labels and writes nothing."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import shlex
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from . import config as config_mod
from . import proc, safety
from .config import Config, ConfigError
from .context import work_root
from .init import label_specs, preflight, workflow_files
from .proc import CommandError

OK, WARN, FAIL = "ok", "warn", "fail"
Runner = Callable[..., proc.Result]


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""


def _run(runner: Runner, args: list[str]) -> proc.Result | None:
    try:
        return runner(args, check=False, timeout=60)
    except CommandError:
        return None


def _tools(runner: Runner) -> list[Check]:
    checks = []
    for name, ok, detail in preflight(runner):
        if ok:
            checks.append(Check(name, OK, detail if name == "git" else ""))
        else:
            fix = "install git" if name == "git" else detail
            checks.append(Check(name, FAIL, "not ready", fix))
    return checks


def _repo(runner: Runner, repo: str) -> Check:
    r = _run(runner, ["gh", "repo", "view", repo, "--json", "nameWithOwner"])
    if r and r.code == 0:
        return Check("github repo", OK, repo)
    fix = f"check that `gh repo view {repo}` works: the name in vigia.toml, and `gh auth login` access"
    return Check("github repo", FAIL, f"{repo} is not reachable", fix)


def _labels(runner: Runner, cfg: Config, reachable: bool) -> Check:
    if not reachable:
        return Check("labels", FAIL, "not checked: repo unreachable", "resolve the github repo check first")
    # Every page: a label past any fixed limit must not be reported missing (its create command would fail).
    r = _run(runner, ["gh", "api", "--paginate", f"repos/{cfg.repo}/labels?per_page=100", "--jq", ".[].name"])
    if not (r and r.code == 0):
        return Check("labels", FAIL, "could not list the labels", f"run `gh label list -R {cfg.repo}`")
    have = {line.strip() for line in (r.stdout or "").splitlines() if line.strip()}
    specs = label_specs(cfg.labels)
    missing = [spec for spec in specs if spec[0] not in have]
    if not missing:
        return Check("labels", OK, f"{len(specs)} of {len(specs)} present")
    fix = " && ".join(
        f"gh label create {shlex.quote(name)} -R {cfg.repo} --color {color} "
        f"--description {shlex.quote(description)}"
        for name, color, description in missing
    )
    return Check("labels", FAIL, "missing " + ", ".join(spec[0] for spec in missing), fix)


def _workflows(clone: Path, cfg: Config) -> Check:
    if not cfg.require_ci:
        return Check("ci workflow", OK, "checks.require_ci is false")
    found = workflow_files(clone)
    if not found:
        return Check(
            "ci workflow",
            FAIL,
            "no workflow file in .github/workflows",
            "vigia merges only with CI green on the head: add a GitHub Actions workflow that runs on "
            "`pull_request`, or set `require_ci = false` under [checks]",
        )
    if not any("pull_request" in _read(p) for p in found):
        return Check(
            "ci workflow",
            WARN,
            f"{len(found)} in .github/workflows, none runs on pull_request",
            "add `pull_request` to the `on:` trigger of the workflow that runs your tests",
        )
    return Check("ci workflow", OK, f"{len(found)} in .github/workflows")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _test_command(cfg: Config) -> Check:
    if cfg.test_command:
        return Check("test command", OK, cfg.test_command)
    return Check(
        "test command",
        WARN,
        "checks.test_command is not set",
        'add `test_command = "<your test command>"` under [checks] in vigia.toml',
    )


def _work_root(clone: Path | None, cfg: Config | None) -> Check:
    """The directory `Ctx.load` will create for this clone (the shared one when there is no valid config)."""
    try:
        root = work_root(clone, cfg) if clone and cfg else safety.check_root(safety.default_root())
    except safety.UnsafePath as exc:
        return Check("work root", FAIL, str(exc), "point TMPDIR at a directory outside your home")
    try:
        safety.check_root(root)  # what every build checks: the per-clone folder may be a symlink to home
    except safety.UnsafePath as exc:
        return Check("work root", FAIL, str(exc), f"remove {shlex.quote(str(root))}; vigia creates it again")
    if os.path.lexists(root) and not root.is_dir():
        return Check(
            "work root",
            FAIL,
            f"{root} exists and is not a directory",
            f"remove it: rm {shlex.quote(str(root))}",
        )
    probe = root
    while not probe.exists():
        probe = probe.parent
    # Doctor writes nothing outside the system temp directory: a work root that resolves out of it (a
    # symlinked `vigia`) is only checked with access(), never probed with a file.
    system_temp = Path(os.path.realpath(safety.default_root().parent))
    try:
        if Path(os.path.realpath(probe)).is_relative_to(system_temp):
            with tempfile.TemporaryFile(dir=probe):
                pass
        elif not os.access(probe, os.W_OK | os.X_OK):
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
    except OSError as exc:
        fix = f"make {shlex.quote(str(probe))} writable, or point TMPDIR at a directory you can write to"
        return Check("work root", FAIL, f"{probe} is not writable: {exc.strerror or exc}", fix)
    return Check("work root", OK, str(root))


def _lock(clone: Path) -> Check:
    """Held means a live process: `flock` is released when its holder exits, so no lock is ever stale."""
    path = clone / ".vigia" / "lock"
    if not path.exists():
        return Check("lock", OK, "free")
    try:
        with path.open("r") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return Check(
                    "lock",
                    FAIL,
                    f"another vigia process is working in {clone}",
                    "wait for it to finish, or stop it (Ctrl-C in the terminal running `vigia watch`)",
                )
            fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError as exc:
        return Check(
            "lock", FAIL, f"cannot open {path}: {exc.strerror or exc}", f"check the permissions of {path}"
        )
    return Check("lock", OK, "free")


def collect(start: Path, runner: Runner = proc.run) -> list[Check]:
    checks = _tools(runner)
    clone: Path | None = None
    cfg: Config | None = None
    path: Path | None = None
    try:
        path = config_mod.find(start)
        clone = path.parent
        cfg = config_mod.load(path)
        checks.append(Check("vigia.toml", OK, str(path)))
    except ConfigError as exc:
        detail = str(exc) if path else str(exc).removesuffix(" — run `vigia init` first")
        checks.append(
            Check("vigia.toml", FAIL, detail, f"correct {path}" if path else "run `vigia init` in a clone")
        )
    if cfg and clone:
        repo = _repo(runner, cfg.repo)
        checks += [repo, _labels(runner, cfg, repo.status == OK), _workflows(clone, cfg), _test_command(cfg)]
    checks.append(_work_root(clone, cfg))
    if clone:
        checks.append(_lock(clone))
    return checks


def problems(checks: list[Check]) -> int:
    return sum(c.status == FAIL for c in checks)


def verdict(checks: list[Check]) -> str:
    n = problems(checks)
    return "ready" if n == 0 else f"{n} problem" + ("" if n == 1 else "s")


def render(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    lines = []
    for c in checks:
        line = f"{c.status:<4}  {c.name:<{width}}  {c.detail}".rstrip()
        lines.append(line)
        if c.fix:
            lines.append(f"      {'':<{width}}  fix: {c.fix}")
    return "\n".join([*lines, "", verdict(checks)])


def to_json(checks: list[Check]) -> str:
    return json.dumps(
        {"checks": [asdict(c) for c in checks], "problems": problems(checks), "verdict": verdict(checks)},
        indent=2,
    )
