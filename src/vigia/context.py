"""Everything one vigia command needs, built once from vigia.toml."""

from __future__ import annotations

import fcntl
import hashlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import comments, safety
from . import config as config_mod
from .config import Config
from .engines import Engines
from .gh import GitHub
from .git import Git
from .ledger import Ledger


class Busy(RuntimeError):
    """Another vigia process holds the lock for this clone."""


def work_root(clone: Path, cfg: Config) -> Path:
    """This clone's work root (not created). One per clone: the clone's lock is what makes recovery safe, so
    two clones of the same repository must never share the directory one of them cleans."""
    tag = hashlib.sha256(str(clone.resolve()).encode()).hexdigest()[:8]
    return safety.check_root(safety.default_root()) / f"{cfg.repo.replace('/', '-')}-{tag}"


@dataclass
class Ctx:
    cfg: Config
    clone: Path
    gh: GitHub
    git: Git
    ledger: Ledger
    engines: Engines
    root: Path
    say: Callable[[str], None] = field(default=print)

    @property
    def state(self) -> Path:
        return self.clone / ".vigia"

    @classmethod
    def load(cls, start: Path, say: Callable[[str], None] = print) -> Ctx:
        path = config_mod.find(start)
        cfg = config_mod.load(path)
        clone = path.parent
        root = work_root(clone, cfg)
        root.mkdir(parents=True, exist_ok=True)
        state = clone / ".vigia"
        return cls(
            cfg=cfg,
            clone=clone,
            gh=GitHub(cfg.repo, cwd=root),
            git=Git(clone, root),
            ledger=Ledger(state / "ledger.jsonl"),
            engines=Engines(cfg, state / "logs"),
            root=root,
            say=say,
        )

    def builder_family(self, pr: int) -> str | None:
        """The lab that built `pr`: from the ledger when vigia opened it, else from the `Engine:` line of a
        handoff that vigia's own GitHub login posted (how a PR opened by hand or by another agent joins)."""
        opened = self.ledger.last("pr_opened", pr=pr)
        if opened and opened.get("family"):
            return opened["family"]
        return comments.engine_family(self.gh.comments(pr), self.gh.login())

    @contextmanager
    def lock(self) -> Iterator[None]:
        self.state.mkdir(parents=True, exist_ok=True)
        with (self.state / "lock").open("w") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Busy(f"another vigia process is working in {self.clone}") from exc
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
