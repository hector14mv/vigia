"""Where vigia may create and delete things, and the one function allowed to delete.

Every worktree vigia makes lives under one work root in the system temp directory. `remove()`
refuses anything that is not strictly inside that root, so a bad path (an empty string, `/`, the
home directory, a symlink pointing out) can never become a recursive delete.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


class UnsafePath(RuntimeError):
    pass


def default_root() -> Path:
    return Path(tempfile.gettempdir()) / "vigia"


def _forbidden(path: Path) -> bool:
    home = Path.home().resolve()
    return path == Path(path.anchor) or path == home or home.is_relative_to(path) or len(path.parts) < 3


def check_root(root: Path) -> Path:
    """The resolved work root, or UnsafePath if it is the filesystem root, home, or an ancestor of home."""
    resolved = Path(os.path.realpath(root))
    if _forbidden(resolved):
        raise UnsafePath(f"refusing to use {resolved} as vigia's work root")
    return resolved


def inside(path: Path, root: Path) -> Path:
    """`path` resolved, if it is strictly inside `root`; UnsafePath otherwise."""
    root_r = check_root(root)
    path_r = Path(os.path.realpath(path))
    if path_r == root_r or not path_r.is_relative_to(root_r) or _forbidden(path_r):
        raise UnsafePath(f"refusing to touch {path_r}: not inside {root_r}")
    return path_r


def remove(path: Path, root: Path) -> bool:
    """Delete a directory vigia created under `root`. Returns False if it was already gone."""
    if Path(path).is_symlink():
        raise UnsafePath(f"refusing to delete {path}: it is a symlink")
    target = inside(path, root)
    if not target.exists():
        return False
    shutil.rmtree(target)
    return True
