"""A workspace: one home folder that drives several repositories at once, the way one factory serves several
products. `vigia.workspace.toml` lists the clones (each with its own `vigia.toml`) and how many engine sessions
may run at once across all of them; `vigia watch` and `vigia status` run from that folder cover every repo.
"""

from __future__ import annotations

import fcntl
import os
import platform
import subprocess
import tomllib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import config as config_mod
from . import jobs
from .config import ConfigError

WORKSPACE_NAME = "vigia.workspace.toml"
MAX_RECOMMENDED = 6  # the private factory this comes from never ran more than six sessions on one machine


@dataclass(frozen=True)
class Workspace:
    path: Path
    repos: tuple[Path, ...]
    slots: int


def find(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        candidate = directory / WORKSPACE_NAME
        if candidate.is_file():
            return candidate
    return None


def load(path: Path) -> Workspace:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    ws = data.get("workspace") or {}
    slots = ws.get("slots", 1)
    if not isinstance(slots, int) or isinstance(slots, bool) or not 1 <= slots <= 32:
        raise ConfigError(f"{path}: workspace.slots: expected an integer from 1 to 32")
    raw = ws.get("repos")
    if not isinstance(raw, list) or not raw or not all(isinstance(r, str) and r for r in raw):
        raise ConfigError(f"{path}: workspace.repos: expected a list of clone folders")
    repos = []
    for entry in raw:
        folder = (path.parent / entry).resolve()
        if not (folder / config_mod.CONFIG_NAME).is_file():
            raise ConfigError(f"{path}: {entry} has no {config_mod.CONFIG_NAME} (run `vigia init` in it)")
        repos.append(folder)
    if len(set(repos)) != len(repos):
        raise ConfigError(f"{path}: workspace.repos lists a clone twice")
    return Workspace(path=path, repos=tuple(repos), slots=slots)


def discover(folder: Path) -> list[str]:
    """Sub-folders of `folder` (one level down) that hold a vigia.toml, as names relative to it."""
    return sorted(p.parent.name for p in folder.glob(f"*/{config_mod.CONFIG_NAME}"))


def render(slots: int, repos: list[str]) -> str:
    listed = ", ".join(config_mod._q(r) for r in repos)  # a folder name may hold quotes or backslashes
    return (
        "# vigia workspace: one home folder for every repository vigia works on.\n"
        "# Run `vigia watch` and `vigia status` from this folder.\n\n"
        "[workspace]\n"
        f"slots = {slots}  # engine sessions at once, across all repos\n"
        f"repos = [{listed}]\n"
    )


def allowance(ws_slots: int, live: dict[Path, int], repo: Path) -> int:
    """How many concurrent jobs `repo` may have this tick: the ones it runs now, plus whatever the whole
    workspace has free (the first repos in the list get the free slots first)."""
    free = ws_slots - sum(live.values())
    return live.get(repo, 0) + max(free, 0)


def memory_gb(run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> float | None:
    """Total RAM in GB, or None when it cannot be read."""
    try:
        if platform.system() == "Darwin":
            out = run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5).stdout
            return int(out.strip()) / 1024**3
        meminfo = Path("/proc/meminfo")
        if meminfo.exists():
            for line in meminfo.read_text().splitlines():
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / 1024**2
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return None


def recommend_slots(
    cpus: int | None = None, ram_gb: float | None = None, quota_used: float | None = None
) -> int:
    """Sessions this machine can run at once: about two CPU cores and two GB of RAM per engine session, at most
    six. When the other lab's weekly quota is more than 70 % used, half as many, because every PR also spends
    one of its gates."""
    cpus = os.cpu_count() if cpus is None else cpus
    ram_gb = memory_gb() if ram_gb is None else ram_gb
    slots = MAX_RECOMMENDED
    if cpus:
        slots = min(slots, max(cpus // 2, 1))
    if ram_gb:
        slots = min(slots, max(int(ram_gb // 2), 1))
    if quota_used is not None and quota_used > 70:
        slots = max(slots // 2, 1)
    return max(slots, 1)


@contextmanager
def lock(ws: Workspace) -> Iterator[None]:
    """One watcher per workspace at a time: two would each think the shared slots were free."""
    from .context import Busy

    path = ws.path.parent / ".vigia-workspace.lock"
    with path.open("a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Busy(f"another vigia watch is running the workspace in {ws.path.parent}") from exc
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def live_by_repo(contexts: list) -> dict[Path, int]:
    return {ctx.clone.resolve(): len(jobs.live(ctx)) for ctx in contexts}
