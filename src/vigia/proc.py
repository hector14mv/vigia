"""Running other programs: `gh`, `git`, and the engines. One place, so tests can replace it."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
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
    env: dict[str, str] | None = None,
) -> Result:
    """Run an engine session: its own process group (so a timeout kills the whole tree), output to `log`.

    `env`, when given, is the session's whole environment (not merged into ours), so it can leave variables out.
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
                env=env,
            )
        except FileNotFoundError:
            return Result(127, "", f"{args[0]}: command not found")
        try:
            out, _ = proc.communicate(input=input, timeout=timeout)
            return Result(proc.returncode, out or "", "")
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                out, _ = proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                out, _ = proc.communicate()
            return Result(
                proc.returncode if proc.returncode is not None else -9, out or "", "", timed_out=True
            )
