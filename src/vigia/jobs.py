"""Engine work in parallel: every gate, review, fix or build can run as its own `vigia job` process.

With `watch.slots = 1` (the default) the watcher runs one engine session per tick, inline, exactly as
before. With more slots it spawns jobs instead and moves on: each job holds a lock on its target (one PR
or one issue) for its whole life, so two sessions never work on the same PR, and the watcher counts the
locks that are held to know how many slots are busy. A job re-reads GitHub and decides again before it
spends a session, so a job spawned from an old snapshot does nothing.

The locks are `flock` locks on files under `.vigia/locks/`: the kernel releases them when a job dies, so
a crashed job never leaves a slot taken.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from .proc import CommandError

if TYPE_CHECKING:
    from .context import Ctx

ACTIONS = ("gate", "review", "fix", "build")
_HELD: set[int] = set()  # file descriptors of the target locks this process holds


def held() -> tuple[int, ...]:
    """The target locks this process holds. Engine sessions inherit them (proc.run_session's `keep_fds`), so a
    target stays locked for as long as its engine lives, even when the job that started it is killed."""
    return tuple(sorted(_HELD))


def target(action: str, number: int) -> str:
    """The lock name of an action: builds lock their issue, everything else its PR."""
    return f"issue-{number}" if action == "build" else f"pr-{number}"


def _path(ctx: Ctx, name: str) -> Path:
    return ctx.state / "locks" / f"{name}.lock"


@contextmanager
def hold(ctx: Ctx, name: str) -> Iterator[bool]:
    """Try to take the lock `name` without waiting. Yields True if this process now holds it."""
    path = _path(ctx, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        _HELD.add(fh.fileno())
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"{os.getpid()}\n")
            fh.flush()
            yield True
        finally:
            # No explicit unlock: closing the file releases the lock, unless an engine started under it still
            # holds an inherited copy, which is the point (see held()).
            _HELD.discard(fh.fileno())


def busy(ctx: Ctx, name: str) -> bool:
    """True while some process holds the lock `name`. Only probes it: never writes to the file."""
    path = _path(ctx, name)
    if not path.exists():
        return False
    with path.open("r") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
        return False


def live(ctx: Ctx) -> list[str]:
    """The targets a job is working on right now, in this clone."""
    folder = ctx.state / "locks"
    if not folder.is_dir():
        return []
    return sorted(p.stem for p in folder.glob("*.lock") if busy(ctx, p.stem))


def spawn(ctx: Ctx, action: str, number: int, stage: str = "") -> bool:
    """Start `vigia job <action> <number> [stage]` in the background, detached from the watcher.

    The watcher takes the target's lock itself and hands the locked file to the child (an inherited file
    descriptor), so the slot is counted as busy from this moment, not from whenever the child gets scheduled.
    Returns False, starting nothing, when the target is already locked.
    """
    name = target(action, number)
    path = _path(ctx, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return False
    # -I: the job runs in the clone, and a `vigia.py` there must not be what `-m vigia` imports (nor PYTHONPATH)
    cmd = [sys.executable, "-I", "-m", "vigia", "job", action, str(number)] + ([stage] if stage else [])
    cmd += ["--lock-fd", str(fd)]
    try:  # everything that can fail from here on must still close the locked file
        logs = ctx.state / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / f"job-{action}-{number}.log").open("a", encoding="utf-8") as log:
            subprocess.Popen(  # noqa: S603 - a fixed argv, no shell
                cmd,
                cwd=ctx.clone,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                pass_fds=(fd,),
            )
    finally:
        os.close(fd)  # the child holds its own copy of the locked file: the lock lives as long as the child
    ctx.ledger.append("job_spawned", action=action, target=name, stage=stage)
    ctx.say(f"{name}: started a {action} job")
    return True


@contextmanager
def owned(ctx: Ctx, name: str, lock_fd: int | None) -> Iterator[bool]:
    """The lock of a job: the one the watcher handed over (`lock_fd`), or one taken here (manual and inline runs)."""
    if lock_fd is None:
        with hold(ctx, name) as got:
            yield got
        return
    _HELD.add(lock_fd)
    try:
        yield True
    finally:
        _HELD.discard(lock_fd)
        os.close(lock_fd)


def still_due(ctx: Ctx, action: str, number: int) -> tuple[bool, object, object]:
    """Re-read GitHub just before spending a session: (due, item, decision). A PR must still be open, managed by
    vigia and decided to need `action`; an issue must still be ready and not building or parked."""
    from . import builder, watch  # lazily: those modules import this one

    labels = ctx.cfg.labels
    if action == "build":
        issue = ctx.gh.issue(number)
        stopped = {labels.building, labels.parked} & set(issue["labels"])
        due = (
            issue["state"] == "OPEN"
            and labels.ready in issue["labels"]
            and not stopped
            and not builder.gaps(ctx, issue)
        )
        try:
            due = due and not builder.blockers(ctx, issue)
        except CommandError:  # cannot tell whether something still blocks it: do not spend a session
            due = False
        return due, issue, None
    pr = ctx.gh.pr(number)
    if pr["state"] != "OPEN" or labels.review not in pr["labels"]:
        return False, pr, None
    decision = watch.decide(watch.state(ctx, pr), ctx.cfg)
    return decision.action == action, pr, decision


def run(ctx: Ctx, action: str, number: int, stage: str = "", lock_fd: int | None = None) -> bool:
    """The body of `vigia job` (and of the inline and manual runs): hold the target, check the step is still due,
    do it. True if it ran."""
    from . import builder, gate  # lazily: those modules import this one

    if action not in ACTIONS:
        raise ValueError(f"unknown job action {action!r}")
    name = target(action, number)
    with owned(ctx, name, lock_fd) as got:
        if not got:
            ctx.say(f"{name}: a job is already working on it; nothing to do")
            return False
        due, item, decision = still_due(ctx, action, number)
        if not due:
            ctx.say(
                f"{name}: no longer due for {action}"
                + (f" (now `{decision}`)" if decision else "")
                + "; nothing to do"
            )
            return False
        ctx.ledger.append(
            "job_started", action=action, target=name, **({"sha": item["head"]} if decision else {})
        )
        if action == "build":
            builder.build(ctx, item)
        elif action == "fix":
            builder.fix(ctx, item, decision.stage)
        else:
            gate.review(ctx, item, action, int(decision.stage or 1))
        ctx.ledger.append("job_finished", action=action, target=name)
        return True
