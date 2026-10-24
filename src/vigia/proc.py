"""Running other programs: `gh`, `git`, and the engines. One place, so tests can replace it."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


class CommandError(RuntimeError):
    def __init__(self, args: Sequence[str], code: int, stdout: str, stderr: str):
        self.args_list = list(args)
        self.code = code
        self.stdout = stdout
        self.stderr = stderr
        tail = (stderr or stdout).strip().splitlines()[-3:]
        super().__init__(f"`{' '.join(self.args_list[:4])}…` exited {code}: {' | '.join(tail)}")


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str
    timed_out: bool = False


def run(
    args: Sequence[str],
    *,
    cwd: Path | str | None = None,
    input: str | None = None,
    timeout: float | None = 300,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> Result:
    """Run a command to completion. stdin is always closed or fed: some CLIs wait on an open stdin forever."""
    try:
        proc = subprocess.run(
            list(args),
            cwd=cwd,
            input=input if input is not None else "",
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, **env} if env else None,
        )
    except FileNotFoundError as exc:
        raise CommandError(args, 127, "", f"{args[0]}: command not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise CommandError(args, 124, "", f"timed out after {timeout}s") from exc
    if check and proc.returncode != 0:
        raise CommandError(args, proc.returncode, proc.stdout, proc.stderr)
    return Result(proc.returncode, proc.stdout, proc.stderr)


def run_session(
    args: Sequence[str],
    *,
    cwd: Path,
    input: str,
    timeout: float,
    log: Path,
    keep_fds: tuple[int, ...] = (),
) -> Result:
    """Run an engine session: its own process group (so a timeout kills the whole tree), output to `log`.

    `keep_fds` are inherited by the session: vigia passes the target locks it holds (vigia.jobs.held), so a
    PR stays locked while its engine lives, even if the process that started the engine is killed outright.
    If this call is interrupted (Ctrl-C, or SIGTERM turned into SystemExit), the session is stopped first.
    """
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as err:
        try:
            proc = subprocess.Popen(
                list(args),
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=err,
                text=True,
                start_new_session=True,
                pass_fds=keep_fds,
            )
        except FileNotFoundError:
            return Result(127, "", f"{args[0]}: command not found")
        try:
            out, _ = proc.communicate(input=input, timeout=timeout)
            # The engine is done, but a child it started may not be: nothing of the session outlives this call,
            # or the target would be let go while something still works on it.
            _end_group(proc.pid, grace=5, proc=proc)
            return Result(proc.returncode, out or "", "")
        except subprocess.TimeoutExpired:
            out = _stop(proc, grace=15)
            return Result(
                proc.returncode if proc.returncode is not None else -9, out or "", "", timed_out=True
            )
        except BaseException:  # the runner is going away: the session must not outlive it
            _stop(proc, grace=5)
            raise


def _stop(proc: subprocess.Popen, grace: float) -> str:
    """SIGTERM the session's process group, SIGKILL what is left of it after `grace` seconds; return what it
    printed.

    The engine exiting is not the group exiting: a child that ignores SIGTERM and closed its inherited fds
    (so it holds neither the output pipe nor the target lock) would keep working on the target after vigia
    lets go of it. So the whole group gets the grace period, and is gone before this returns.
    """
    deadline = time.monotonic() + grace
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    out = None
    with contextlib.suppress(subprocess.TimeoutExpired):
        out, _ = proc.communicate(timeout=grace)
    _end_group(proc.pid, grace=max(0.0, deadline - time.monotonic()), proc=proc)
    if out is None:
        out, _ = proc.communicate()
    return out


def _end_group(pgid: int, grace: float, proc: subprocess.Popen | None = None) -> None:
    """Make sure no process is left in group `pgid`: SIGTERM, then SIGKILL after `grace` seconds, then wait for
    the group to be gone (at most five more seconds). `proc`, the group's leader, is reaped along the way."""

    def alive() -> bool:
        if proc is not None:
            proc.poll()  # reap the leader, or macOS keeps answering for its zombie
        return _group_alive(pgid)

    if not alive():
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if alive():
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while alive() and time.monotonic() < deadline:  # SIGKILLed orphans still need reaping
        time.sleep(0.05)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # macOS answers EPERM for a group whose only members are unreaped zombies
        return True
    return True
