"""Engine sessions and the processes around them: a session never outlives its runner unnoticed, and the
job entry point cannot be replaced by a file in the repository it works on.

These tests start real (tiny Python) processes, all under tmp_path, and only ever signal processes they
started themselves.
"""

import fcntl
import os
import signal
import subprocess
import sys
import time

from fakes import APPROVE
from test_e2e import TICKET, world  # noqa: F401  (world is a fixture)
from vigia import jobs

SESSION = """\
import fcntl, os, signal, sys
from pathlib import Path
from vigia import proc

def stop(signum, frame):
    raise SystemExit(143)

signal.signal(signal.SIGTERM, stop)
fd = os.open({lock!r}, os.O_RDWR | os.O_CREAT)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
proc.run_session([sys.executable, "-c", {engine!r}], cwd=Path({cwd!r}), input="", timeout=120,
                 log=Path({log!r}), keep_fds=(fd,))
"""


ENGINE = "import os, time; open({pid!r}, 'w').write(str(os.getpid())); time.sleep(60)"

# An engine that leaves behind a child the SIGTERM does not reach: it ignores the signal, and it closed every
# inherited fd and redirected its output, so neither the output pipe nor the target lock tracks it.
ENGINE_WITH_STUBBORN_CHILD = """\
import os, subprocess, sys, time
child = "import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); open({child!r}, 'w').write(str(os.getpid())); time.sleep(60)"
subprocess.Popen([sys.executable, "-c", child], close_fds=True,
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
while not (os.path.exists({child!r}) and open({child!r}).read()):
    time.sleep(0.05)
open({pid!r}, 'w').write(str(os.getpid()))
time.sleep(60)
"""


def _start(tmp_path, engine=ENGINE):
    lock, pid = tmp_path / "pr-1.lock", tmp_path / "engine.pid"
    script = tmp_path / "job.py"
    engine = engine.format(pid=str(pid), child=str(tmp_path / "child.pid"))
    script.write_text(
        SESSION.format(lock=str(lock), engine=engine, cwd=str(tmp_path), log=str(tmp_path / "s.log"))
    )
    job = subprocess.Popen([sys.executable, str(script)], cwd=tmp_path)
    for _ in range(200):
        if pid.exists() and pid.read_text():
            return job, lock, int(pid.read_text())
        time.sleep(0.05)
    job.kill()
    raise AssertionError("the engine never started")


def _locked(lock) -> bool:
    with lock.open("r") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
        return False


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_gone(pid: int) -> bool:
    for _ in range(200):
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def test_a_killed_job_leaves_its_target_locked_while_its_engine_lives(tmp_path):
    job, lock, engine = _start(tmp_path)
    try:
        job.kill()  # SIGKILL: no handler runs, the engine is orphaned
        job.wait(timeout=10)
        assert _alive(engine) and _locked(lock)  # the engine's inherited copy keeps the PR locked
    finally:
        os.kill(engine, signal.SIGKILL)
    assert _wait_gone(engine)
    for _ in range(100):
        if not _locked(lock):
            break
        time.sleep(0.05)
    assert not _locked(lock)  # and the lock goes with it


def test_a_terminated_job_stops_its_engine_first(tmp_path):
    job, lock, engine = _start(tmp_path)
    try:
        job.send_signal(signal.SIGTERM)
        assert job.wait(timeout=20) == 143
        assert _wait_gone(engine)
        assert not _locked(lock)
    finally:
        if _alive(engine):
            os.kill(engine, signal.SIGKILL)


def test_a_terminated_job_stops_what_its_engine_left_behind_before_letting_go(tmp_path):
    job, lock, engine = _start(tmp_path, ENGINE_WITH_STUBBORN_CHILD)
    child = int((tmp_path / "child.pid").read_text())
    try:
        job.send_signal(signal.SIGTERM)
        assert job.wait(timeout=30) == 143
        assert not _alive(child)  # gone by the time the job exits, so the freed target is really free
        assert not _locked(lock)
    finally:
        for pid in (engine, child):
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)


def test_engine_sessions_inherit_the_locks_their_runner_holds(world):  # noqa: F811
    gh, build = world
    ctx, run = build([APPROVE])
    gh.add_issue("Add a helper", TICKET, ["vigia:ready", "tier:T0"])
    assert jobs.run(ctx, "build", 1)
    assert run.calls[-1]["keep_fds"]  # the build held issue-1, and its session got the lock
    assert jobs.held() == ()  # and nothing stays registered once the job is over


def test_the_job_entry_point_ignores_a_vigia_py_in_the_clone(tmp_path):
    (tmp_path / "vigia.py").write_text("open('shadowed', 'w').write('x')\n")
    out = subprocess.run(
        [sys.executable, "-I", "-m", "vigia", "--version"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0 and out.stdout.startswith("vigia ")
    assert not (tmp_path / "shadowed").exists()


def test_a_session_that_ends_well_leaves_nothing_behind(tmp_path):
    from pathlib import Path

    from vigia import proc

    pid, child_pid = tmp_path / "engine.pid", tmp_path / "child.pid"
    engine = ENGINE_WITH_STUBBORN_CHILD.format(pid=str(pid), child=str(child_pid)).replace(
        "time.sleep(60)\n", "", 1
    )  # the engine itself exits at once, cleanly, leaving its child behind
    started = time.monotonic()
    result = proc.run_session(
        [sys.executable, "-c", engine], cwd=Path(tmp_path), input="", timeout=60, log=tmp_path / "s.log"
    )
    child = int(child_pid.read_text())
    try:
        assert result.code == 0 and not result.timed_out
        assert not _alive(child)  # SIGTERM ignored, so it took the SIGKILL after the grace period
        assert time.monotonic() - started < 30
    finally:
        if _alive(child):
            os.kill(child, signal.SIGKILL)
