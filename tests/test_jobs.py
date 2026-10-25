"""Parallel slots: job locks, the watcher filling free slots, jobs re-deciding, recovery leaving live jobs alone,
the ledger under concurrent writers, and the multi-repo workspace."""

import json
import multiprocessing
from dataclasses import replace

from fakes import APPROVE
from test_e2e import TICKET, decisions, world  # noqa: F401  (world is a fixture)
from vigia import fuel, jobs, workspace
from vigia.ledger import Ledger
from vigia.watch import tick


def test_a_held_lock_is_busy_and_released_on_exit(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    assert not jobs.busy(ctx, "pr-7")
    with jobs.hold(ctx, "pr-7") as got:
        assert got and jobs.busy(ctx, "pr-7") and jobs.live(ctx) == ["pr-7"]
        with jobs.hold(ctx, "pr-7") as second:
            assert not second  # nobody takes a target twice
    assert not jobs.busy(ctx, "pr-7") and jobs.live(ctx) == []


def test_the_watcher_fills_free_slots_and_returns(world, monkeypatch):  # noqa: F811
    gh, build = world
    ctx, run = build([APPROVE] * 4)
    for title in ("One", "Two", "Three"):
        gh.add_issue(title, TICKET, ["vigia:ready", "tier:T0"])
    spawned = []
    monkeypatch.setattr(
        jobs, "spawn", lambda c, action, number, stage="": spawned.append((action, number)) or True
    )
    ctx.cfg = replace(ctx.cfg, slots=2)
    tick(ctx)
    assert spawned == [("build", 1), ("build", 2)]  # two slots, two builds, nothing run inline
    assert run.calls == []


def test_a_target_with_a_live_job_gets_no_second_session(world, monkeypatch):  # noqa: F811
    gh, build = world
    ctx, run = build([APPROVE])
    gh.add_issue("One", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx)  # slots 1: builds inline
    pr = next(iter(gh.prs_))
    spawned = []
    monkeypatch.setattr(
        jobs, "spawn", lambda c, action, number, stage="": spawned.append((action, number)) or True
    )
    ctx.cfg = replace(ctx.cfg, slots=3)
    with jobs.hold(ctx, jobs.target("gate", pr)):
        seen = tick(ctx)
    assert spawned == [] and decisions(seen) == [(pr, "wait")]
    tick(ctx)
    assert spawned == [("gate", pr)]


def test_a_job_decides_again_before_spending_a_session(world):  # noqa: F811
    gh, build = world
    ctx, run = build([APPROVE])
    gh.add_issue("One", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx)
    pr = next(iter(gh.prs_))
    assert jobs.run(ctx, "gate", pr, "1") is True  # due: it runs
    reviews = [c for c in run.calls if c["role"] == "reviewer"]
    assert len(reviews) == 1
    assert jobs.run(ctx, "gate", pr, "1") is False  # the verdict is in: a stale job does nothing
    assert len([c for c in run.calls if c["role"] == "reviewer"]) == 1
    rows = [
        r["event"]
        for r in ctx.ledger.rows()
        if r["event"].startswith("job_") and r.get("target") == f"pr-{pr}"
    ]
    assert rows == ["job_started", "job_finished"]


def test_jobs_run_end_to_end_with_two_slots(world, monkeypatch):  # noqa: F811
    gh, build = world
    ctx, run = build([APPROVE, APPROVE])
    for title in ("One", "Two"):
        gh.add_issue(title, TICKET, ["vigia:ready", "tier:T0"])
    # run each job right away in this process, as `vigia job` would in its own
    monkeypatch.setattr(jobs, "spawn", lambda c, action, number, stage="": jobs.run(c, action, number, stage))
    ctx.cfg = replace(ctx.cfg, slots=2)
    for _ in range(4):
        tick(ctx)
    assert len(gh.merged) == 2


def test_recovery_leaves_a_live_build_alone(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    n = gh.add_issue("One", TICKET, ["tier:T0", "vigia:building"])
    ctx.ledger.append("build_started", issue=n, tier="T0", engine="claude", model="m", branch="b")
    with jobs.hold(ctx, jobs.target("build", n)):
        tick(ctx, build=False)
        assert "vigia:parked" not in gh.issues_[n]["labels"]  # in flight, not interrupted
    tick(ctx, build=False)
    assert "vigia:parked" in gh.issues_[n]["labels"]  # its job is gone: now it was interrupted


def test_recovery_does_not_park_a_build_that_finished_meanwhile(world, monkeypatch):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    n = gh.add_issue("One", TICKET, ["tier:T0", "vigia:building"])
    ctx.ledger.append("build_started", issue=n, tier="T0", engine="claude", model="m", branch="b")
    lock = jobs.target("build", n)
    real_busy, real_hold = jobs.busy, jobs.hold
    finished = []

    def job_finishes(name):
        # the build job opens its PR, enrolls it and exits right before recovery looks at its lock
        if name == lock and not finished:
            finished.append(name)
            ctx.ledger.append("pr_opened", pr=77, issue=n, sha="abc", branch="b", tier="T0")
            gh.issues_[n]["labels"].remove("vigia:building")
            ctx.ledger.append("enrolled", pr=77, issue=n)

    monkeypatch.setattr(jobs, "busy", lambda c, name: job_finishes(name) or real_busy(c, name))
    monkeypatch.setattr(jobs, "hold", lambda c, name: job_finishes(name) or real_hold(c, name))
    tick(ctx, build=False)
    assert finished  # the race happened
    assert "vigia:parked" not in gh.issues_[n]["labels"]
    assert ctx.ledger.events("build_failed", issue=n) == []
    assert gh.bodies(n) == []  # no false interruption comment


def test_recovery_keeps_the_worktree_of_a_live_review(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    live = ctx.root / "gate-5-abcdef12-1"
    stale = ctx.root / "gate-6-abcdef12-1"
    live.mkdir(parents=True)
    stale.mkdir(parents=True)
    with jobs.hold(ctx, jobs.target("gate", 5)):
        tick(ctx, build=False)
    assert live.exists() and not stale.exists()


def _append_many(path, n):
    lg = Ledger(path)
    for i in range(n):
        lg.append("ping", i=i, text="x" * 200)


def test_parallel_writers_never_tear_a_ledger_line(tmp_path):
    path = tmp_path / "ledger.jsonl"
    procs = [multiprocessing.Process(target=_append_many, args=(path, 150)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    lines = path.read_text().splitlines()
    assert len(lines) == 600 and all(json.loads(line)["event"] == "ping" for line in lines)


def test_workspace_load_discover_and_share_slots(tmp_path):
    from vigia.config import Config, default_tiers, render

    for name, repo in (("app", "acme/app"), ("api", "acme/api")):
        (tmp_path / name).mkdir()
        (tmp_path / name / "vigia.toml").write_text(
            render(Config(repo=repo, base="main", tiers=default_tiers()))
        )
    (tmp_path / "notes").mkdir()
    assert workspace.discover(tmp_path) == ["api", "app"]
    (tmp_path / workspace.WORKSPACE_NAME).write_text(workspace.render(3, ["app", "api"]))
    ws = workspace.load(workspace.find(tmp_path / "app"))
    assert ws.slots == 3 and [r.name for r in ws.repos] == ["app", "api"]
    app, api = ws.repos
    assert workspace.allowance(3, {app: 1, api: 0}, app) == 3  # its one job + the two free slots
    assert workspace.allowance(3, {app: 2, api: 1}, api) == 1  # full: only what it already runs


def test_recommended_slots_follow_the_machine_and_the_quota():
    assert workspace.recommend_slots(cpus=16, ram_gb=64) == 6  # never more than six
    assert workspace.recommend_slots(cpus=8, ram_gb=8) == 4
    assert workspace.recommend_slots(cpus=2, ram_gb=4) == 1
    assert workspace.recommend_slots(cpus=8, ram_gb=16, quota_used=80) == 2
    assert workspace.recommend_slots(cpus=1, ram_gb=1) == 1


def test_codex_weekly_quota_is_read_from_the_newest_rollout(tmp_path):
    day = tmp_path / "2026" / "10" / "24"
    day.mkdir(parents=True)
    row = {
        "payload": {
            "rate_limits": {
                "primary": {"used_percent": 12.0, "window_minutes": 10080, "resets_at": 1793386331}
            }
        }
    }
    (day / "rollout-a.jsonl").write_text('{"type": "session"}\n' + json.dumps(row) + "\n")
    tank = fuel.codex_weekly(tmp_path)
    assert tank["used_percent"] == 12.0 and tank["resets_at"] == "2026-10-30T18:52:11Z"
    assert fuel.codex_weekly(tmp_path / "missing") is None


def test_spawn_reserves_the_slot_before_the_child_runs(world, monkeypatch):  # noqa: F811
    import os
    import subprocess
    import sys

    gh, build = world
    ctx, _ = build([])
    seen, child_fds = {}, []

    def fake_popen(cmd, **kw):  # the "child" keeps its own copy of the locked file, as a real one does
        seen.update(cmd=cmd, **kw)
        child_fds.append(os.dup(kw["pass_fds"][0]))

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    assert jobs.spawn(ctx, "fix", 12, "gate") is True
    fd = seen["pass_fds"][0]
    assert seen["cmd"] == [
        sys.executable,
        "-I",
        "-m",
        "vigia",
        "job",
        "fix",
        "12",
        "gate",
        "--lock-fd",
        str(fd),
    ]
    assert (
        seen["cwd"] == ctx.clone and seen["start_new_session"] is True and seen["stdin"] == subprocess.DEVNULL
    )
    assert jobs.busy(ctx, "pr-12")  # counted as busy before the child even starts
    assert jobs.spawn(ctx, "fix", 12, "gate") is False and len(child_fds) == 1  # never twice
    os.close(child_fds[0])  # the child exits
    assert not jobs.busy(ctx, "pr-12")
    assert ctx.ledger.last("job_spawned")["target"] == "pr-12"


def test_spawn_releases_the_lock_when_the_log_folder_cannot_be_made(world, monkeypatch):  # noqa: F811
    import subprocess

    import pytest

    gh, build = world
    ctx, _ = build([])
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: started.append(a))
    ctx.state.mkdir(parents=True, exist_ok=True)
    (ctx.state / "logs").write_text("not a folder")  # `.vigia/logs` cannot be created
    with pytest.raises(OSError):
        jobs.spawn(ctx, "fix", 12, "gate")
    assert started == []
    assert not jobs.busy(ctx, "pr-12")  # the descriptor was closed: nothing keeps the target locked
    assert ctx.ledger.last("job_spawned") is None
    (ctx.state / "logs").unlink()
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: None)
    assert jobs.spawn(ctx, "fix", 12, "gate") is True  # and the target can be taken again


def test_no_gate_starts_while_the_build_that_opened_the_pr_holds_its_issue(world, monkeypatch):  # noqa: F811
    gh, build = world
    ctx, run = build([APPROVE])
    n = gh.add_issue("One", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx)  # slots 1: builds inline; the PR is open and labelled
    pr = next(iter(gh.prs_))
    spawned = []
    monkeypatch.setattr(
        jobs, "spawn", lambda c, action, number, stage="": spawned.append((action, number)) or True
    )
    ctx.cfg = replace(ctx.cfg, slots=3)
    with jobs.hold(ctx, jobs.target("build", n)):  # the build job has not posted its handoff yet
        seen = tick(ctx)
    assert spawned == [] and decisions(seen) == [(pr, "wait")]
    tick(ctx)  # the build job ended
    assert spawned == [("gate", pr)]


def test_a_job_skips_a_pr_taken_out_of_vigia_or_an_issue_parked_meanwhile(world):  # noqa: F811
    gh, build = world
    ctx, run = build([APPROVE])
    gh.add_issue("One", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx)
    pr = next(iter(gh.prs_))
    gh.remove_label(pr, "vigia:review")
    assert jobs.run(ctx, "gate", pr, "1") is False and not [c for c in run.calls if c["role"] == "reviewer"]
    n = gh.add_issue("Two", TICKET, ["vigia:ready", "tier:T0", "vigia:parked"])
    assert jobs.run(ctx, "build", n) is False


def test_a_one_slot_workspace_runs_inline_whatever_each_repo_says(world, monkeypatch):  # noqa: F811
    gh, build = world
    ctx, run = build([])
    ctx.cfg = replace(ctx.cfg, slots=3)
    gh.add_issue("One", TICKET, ["vigia:ready", "tier:T0"])
    spawned = []
    monkeypatch.setattr(jobs, "spawn", lambda *a, **k: spawned.append(a) or True)
    from vigia.watch import workspace_tick

    workspace_tick(workspace.Workspace(ctx.clone / "vigia.workspace.toml", (ctx.clone,), 1), [ctx])
    assert spawned == [] and len(gh.prs_) == 1  # built inline, no detached job


def test_manual_commands_wait_for_a_live_job(world):  # noqa: F811
    import pytest

    from vigia.cli import _target

    gh, build = world
    ctx, _ = build([])
    with (
        jobs.hold(ctx, "pr-5"),
        pytest.raises(SystemExit, match="a vigia job is working on pr-5"),
        _target(ctx, "gate", 5),
    ):
        pass


def test_vigia_run_rechecks_the_issue_it_picked_once_it_holds_it(world, monkeypatch, capsys):  # noqa: F811
    from vigia import builder, cli

    gh, build = world
    ctx, run = build([])
    n = gh.add_issue("One", TICKET, ["vigia:ready", "tier:T0"])
    monkeypatch.setattr(cli.Ctx, "load", classmethod(lambda cls, root: ctx))
    picked = builder.next_issue

    def picked_then_built_by_a_job(c, skip=frozenset()):
        issue = picked(c, skip)  # the snapshot still says ready...
        gh.remove_label(n, "vigia:ready")  # ...but a detached job builds it and lets go before we lock it
        return issue

    monkeypatch.setattr(builder, "next_issue", picked_then_built_by_a_job)
    assert cli.main(["run"]) == 0
    assert run.calls == [] and gh.prs_ == {}  # no second session, no duplicate PR
    assert "no longer ready" in capsys.readouterr().out


def test_vigia_run_on_a_number_never_builds_what_a_job_just_built(world, monkeypatch):  # noqa: F811
    import pytest

    from vigia import cli

    gh, build = world
    ctx, run = build([])
    n = gh.add_issue("One", TICKET, ["vigia:ready", "tier:T0"])
    monkeypatch.setattr(cli.Ctx, "load", classmethod(lambda cls, root: ctx))
    assert jobs.run(ctx, "build", n)  # a detached job built it, opened its PR and let go
    gh.add_labels(n, "vigia:ready")  # even with the label back, the issue already has its PR
    calls = len(run.calls)
    with pytest.raises(SystemExit, match="already has an open pull request"):
        cli.main(["run", "--issue", str(n)])
    assert len(run.calls) == calls and len(gh.prs_) == 1


def test_vigia_run_stops_when_the_existing_pr_cannot_be_read(world, monkeypatch):  # noqa: F811
    import pytest

    from vigia import cli
    from vigia.proc import CommandError

    gh, build = world
    ctx, run = build([])
    n = gh.add_issue("One", TICKET, ["vigia:ready", "tier:T0"])
    monkeypatch.setattr(cli.Ctx, "load", classmethod(lambda cls, root: ctx))
    assert jobs.run(ctx, "build", n)  # a detached job built it and opened its PR
    gh.add_labels(n, "vigia:ready")
    (pr,) = gh.prs_

    def unreachable(number):
        raise CommandError(["gh", "pr", "view", str(number)], 1, "", "HTTP 502: Bad Gateway")

    monkeypatch.setattr(gh, "pr", unreachable)  # the issue reads fine, the PR lookup does not
    calls = len(run.calls)
    with pytest.raises(SystemExit, match=f"cannot tell whether its pull request #{pr} is still open"):
        cli.main(["run", "--issue", str(n)])
    assert len(run.calls) == calls and len(gh.prs_) == 1  # no second session, no duplicate PR


def test_one_slot_runs_nothing_inline_while_an_earlier_job_holds_it(world):  # noqa: F811
    gh, build = world
    ctx, run = build([])
    gh.add_issue("One", TICKET, ["vigia:ready", "tier:T0"])
    with jobs.hold(ctx, "issue-99"):  # a job left by an earlier run with more slots
        tick(ctx)
    assert run.calls == [] and gh.prs_ == {}
    tick(ctx)
    assert len(gh.prs_) == 1


def test_two_watchers_cannot_spend_one_workspace(world):  # noqa: F811
    import pytest

    from vigia.context import Busy

    gh, build = world
    ctx, _ = build([])
    ws = workspace.Workspace(ctx.clone / "vigia.workspace.toml", (ctx.clone,), 2)
    with workspace.lock(ws), pytest.raises(Busy, match="another vigia watch"), workspace.lock(ws):
        pass


def test_a_stale_park_is_not_applied_to_a_pr_that_moved(world):  # noqa: F811
    from fakes import git
    from vigia.rules import Decision
    from vigia.watch import Seen, park, sense

    gh, build = world
    ctx, _ = build([])
    gh.add_issue("One", TICKET, ["vigia:ready", "tier:T0"])
    tick(ctx)
    pr = next(iter(gh.prs_))
    old = sense(ctx, track=False)[0]
    stale = Seen(
        old.pr,
        old.state,
        Decision("park", "CI is red on this head; the cap of 2 fix rounds is reached", old.pr["head"]),
    )
    wt = ctx.root / "moved"  # a job pushed a new head after the snapshot
    git("worktree", "add", "--quiet", "--detach", str(wt), old.pr["head"], cwd=ctx.clone)
    (wt / "fix.txt").write_text("fixed")
    git("add", "-A", cwd=wt)
    git("commit", "--quiet", "-m", "fix", cwd=wt)
    git("push", "--quiet", "origin", f"HEAD:refs/heads/{gh.prs_[pr]['branch']}", cwd=wt)
    park(ctx, stale)
    assert "vigia:parked" not in gh.prs_[pr]["labels"]
    with jobs.hold(ctx, f"pr-{pr}"):  # and never while a job works on it
        park(ctx, stale)
    assert "vigia:parked" not in gh.prs_[pr]["labels"]


def test_slot_counts_outside_1_to_32_are_refused(capsys):
    import pytest

    from vigia import cli

    for bad in ("-1", "0", "33", "two"):
        with pytest.raises(SystemExit):
            cli.parser().parse_args(["workspace", "--slots", bad])
        with pytest.raises(SystemExit):
            cli.parser().parse_args(["watch", "--slots", bad])
    assert cli.parser().parse_args(["workspace", "--slots", "4"]).slots == 4


def test_the_workspace_file_survives_folder_names_with_quotes_and_backslashes(tmp_path):
    import tomllib

    names = ['my"repo', "back\\slash", "plain"]
    data = tomllib.loads(workspace.render(2, names))
    assert data["workspace"]["repos"] == names
