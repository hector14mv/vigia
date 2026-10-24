"""Git operations on the local clone and the worktrees vigia makes under its work root."""

from __future__ import annotations

import fcntl
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from . import proc, safety

Runner = Callable[..., proc.Result]


class Git:
    def __init__(self, clone: Path, root: Path, runner: Runner = proc.run):
        self.clone = clone  # the user's clone of the target repo (where vigia.toml lives)
        self.root = root  # vigia's work root; every worktree is created strictly inside it
        self._run = runner

    def _git(self, *args: str, cwd: Path | None = None, check: bool = True) -> proc.Result:
        return self._run(["git", *args], cwd=cwd or self.clone, check=check)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """One process at a time changes the clone's shared state (refs, the worktree list, info/exclude):
        parallel jobs would otherwise race on git's own lock files and fail."""
        path = self.clone / ".vigia" / "git.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def fetch(self, *refs: str) -> None:
        with self._locked():
            self._git("fetch", "--quiet", "origin", *refs)

    def rev(self, ref: str, cwd: Path | None = None) -> str:
        return self._git("rev-parse", ref, cwd=cwd).stdout.strip()

    def remote_branch_sha(self, branch: str) -> str:
        """Fetch `branch` from origin and return the commit it points to."""
        self.fetch(f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
        return self.rev(f"refs/remotes/origin/{branch}")

    def _check_new(self, path: Path) -> Path:
        target = safety.inside(path, self.root)
        if target.exists():
            raise safety.UnsafePath(f"{target} already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def worktree_detached(self, path: Path, commit: str) -> Path:
        """A worktree at exactly `commit`, on no branch. Builders and fixers push with HEAD:<branch>."""
        target = self._check_new(path)
        with self._locked():
            self._git("worktree", "add", "--quiet", "--detach", str(target), commit)
            self._exclude(".vigia-handoff.md")
        return target

    def remove_worktree(self, path: Path) -> None:
        target = safety.inside(path, self.root)
        with self._locked():
            self._git("worktree", "remove", "--force", str(target), check=False)
            safety.remove(target, self.root)
            self._git("worktree", "prune", check=False)

    def exclude(self, pattern: str) -> None:
        """Add `pattern` to the clone's info/exclude (shared by all its worktrees), once."""
        with self._locked():
            self._exclude(pattern)

    def _exclude(self, pattern: str) -> None:
        common = Path(self._git("rev-parse", "--git-common-dir").stdout.strip())
        if not common.is_absolute():
            common = self.clone / common
        exclude = common / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if pattern not in current.splitlines():
            exclude.write_text(
                current + ("" if current.endswith("\n") or not current else "\n") + pattern + "\n",
                encoding="utf-8",
            )

    def files(self, commit: str) -> list[str]:
        """Every tracked file at `commit`."""
        return [f for f in self._git("ls-tree", "-r", "--name-only", commit).stdout.splitlines() if f]

    def dirty(self, wt: Path) -> bool:
        return bool(self._git("status", "--porcelain", cwd=wt).stdout.strip())

    def commit_all(self, wt: Path, message: str) -> None:
        self._git("add", "-A", cwd=wt)
        self._git("commit", "--quiet", "-m", message, cwd=wt)

    def count(self, wt: Path, since: str) -> int:
        return int(self._git("rev-list", "--count", f"{since}..HEAD", cwd=wt).stdout.strip() or 0)

    def changed_files(self, wt: Path, since: str) -> list[str]:
        out = self._git("diff", "--name-only", f"{since}..HEAD", cwd=wt).stdout
        return [line for line in out.splitlines() if line.strip()]

    def push(self, wt: Path, branch: str, new: bool = False) -> None:
        """Push HEAD to `branch`. Never forced: if someone else moved the branch, the push fails."""
        self._git("push", "--quiet", "origin", f"HEAD:refs/heads/{branch}", cwd=wt)

    def diff(self, base: str, head: str, cwd: Path | None = None) -> str:
        return self._git("diff", f"{base}...{head}", cwd=cwd).stdout

    def merge_in(self, wt: Path, ref: str, message: str) -> bool:
        """Merge `ref` into the worktree's HEAD with a merge commit. False, leaving HEAD as it was, on a conflict."""
        result = self._git("merge", "--no-ff", "--no-edit", "-m", message, ref, cwd=wt, check=False)
        if result.code != 0:
            self._git("merge", "--abort", cwd=wt, check=False)
            return False
        return True

    def merge_base(self, a: str, b: str) -> str:
        return self._git("merge-base", a, b).stdout.strip()
