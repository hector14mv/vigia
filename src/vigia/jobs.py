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

if TYPE_CHECKING:
    from .context import Ctx

ACTIONS = ("gate", "review", "fix", "build")


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
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"{os.getpid()}\n")
            fh.flush()
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


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


def spawn(ctx: Ctx, action: str, number: int, stage: str = "") -> None:
    """Start `vigia job <action> <number> [stage]` in the background, detached from the watcher."""
    logs = ctx.state / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = (logs / f"job-{action}-{number}.log").open("a", encoding="utf-8")
    cmd = [sys.executable, "-m", "vigia", "job", action, str(number)] + ([stage] if stage else [])
    subprocess.Popen(  # noqa: S603 - a fixed argv, no shell
        cmd,
        cwd=ctx.clone,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    ctx.ledger.append("job_spawned", action=action, target=target(action, number), stage=stage)
    ctx.say(f"{target(action, number)}: started a {action} job")


def run(ctx: Ctx, action: str, number: int, stage: str = "") -> bool:
    """The body of `vigia job`: take the target's lock, check the step is still due, do it. True if it ran."""
    from . import (
        builder,
        gate,
        watch,
    )  # the job imports the rest of vigia lazily: this module is imported by them

    if action not in ACTIONS:
        raise ValueError(f"unknown job action {action!r}")
    name = target(action, number)
    with hold(ctx, name) as got:
        if not got:
            ctx.say(f"{name}: another job holds it; nothing to do")
            return False
        if action == "build":
            issue = ctx.gh.issue(number)
            labels = ctx.cfg.labels
            if issue["state"] != "OPEN" or labels.ready not in issue["labels"] or builder.gaps(ctx, issue):
                ctx.say(f"#{number}: no longer ready to build; nothing to do")
                return False
            ctx.ledger.append("job_started", action=action, target=name)
            builder.build(ctx, issue)
        else:
            pr = ctx.gh.pr(number)
            decision = watch.decide(watch.state(ctx, pr), ctx.cfg)  # GitHub may have moved since the spawn
            if decision.action != action:
                ctx.say(f"{name}: the step is now `{decision}`, not {action}; nothing to do")
                return False
            ctx.ledger.append("job_started", action=action, target=name, sha=pr["head"])
            if action == "fix":
                builder.fix(ctx, pr, decision.stage)
            else:
                gate.review(ctx, pr, action, int(decision.stage or 1))
        ctx.ledger.append("job_finished", action=action, target=name)
        return True
