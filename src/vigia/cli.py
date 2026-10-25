"""The `vigia` command."""

from __future__ import annotations

import argparse
import errno
import json
import signal
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from . import __version__, builder, doctor, gate, jobs, status, tickets, watch, workspace
from . import config as config_mod
from . import init as init_mod
from .config import ConfigError
from .context import Busy, Ctx
from .proc import CommandError
from .safety import UnsafePath


def _ask(question: str, default: str) -> str:
    shown = f" [{default}]" if default else ""
    try:
        answer = input(f"{question}{shown}: ").strip()
    except EOFError:
        answer = ""
    return answer or default


def _pr(ctx: Ctx, number: int) -> dict:
    pr = ctx.gh.pr(number)
    if ctx.cfg.labels.review not in pr["labels"]:
        raise SystemExit(f"PR #{number} is not managed by vigia (no `{ctx.cfg.labels.review}` label)")
    return pr


def cmd_init(args: argparse.Namespace) -> int:
    init_mod.init(
        Path.cwd(),
        _ask,
        print,
        repo=args.repo,
        builder=args.builder,
        assume_yes=args.yes,
        force=args.force,
        create_labels=not args.no_labels,
    )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    ctx = Ctx.load(Path.cwd())
    with ctx.lock():
        if args.issue:
            number = args.issue
        else:
            picked = builder.next_issue(ctx)
            if picked is None:
                print(f"No ready issues (label `{ctx.cfg.labels.ready}`).")
                return 0
            number = picked["number"]
        with _target(ctx, "build", number):
            # Everything is read again under the issue's lock: a detached job may have built it and let go of it
            # since it was picked (or since the person typed the number).
            if args.issue:
                issue = ctx.gh.issue(number)
                if issue["state"] != "OPEN":
                    raise SystemExit(f"#{number} is closed")
                if ctx.cfg.labels.building in issue["labels"]:
                    raise SystemExit(f"#{number} is being built; try again when that ends")
                if ctx.cfg.labels.ready not in issue["labels"]:
                    raise SystemExit(
                        f"#{number} is not ready: a maintainer adds `{ctx.cfg.labels.ready}` to build it"
                    )
                problems = builder.gaps(ctx, issue)
                if problems:
                    raise SystemExit(f"#{number} is not launchable: " + "; ".join(problems))
                # the same check as the watcher's: a maintainer's `ready` on the ticket as it is now
                issue = builder.launchable(ctx, issue)
                if issue is None:
                    raise SystemExit(f"#{number} was not built (the reason is above)")
            else:
                due, issue, _ = jobs.still_due(ctx, "build", number)
                if not due:
                    print(f"#{number} is no longer ready; nothing to do.")
                    return 0
            existing = _open_pr_for(ctx, number)
            if existing:
                raise SystemExit(f"#{number} already has an open pull request built by vigia: #{existing}")
            pr = builder.build(ctx, issue)
    return 0 if pr else 1


def _open_pr_for(ctx: Ctx, issue: int) -> int | None:
    """The open PR vigia built for `issue`, if any: a manual build must not open a second one.

    A PR whose state cannot be read stops the run: an unknown state is no permission to build again."""
    for row in reversed(ctx.ledger.events("pr_opened", issue=issue)):
        try:
            if ctx.gh.pr(row["pr"])["state"] == "OPEN":
                return row["pr"]
        except CommandError as e:
            raise SystemExit(
                f"#{issue}: cannot tell whether its pull request #{row['pr']} is still open ({e}); try again later"
            ) from e
    return None


@contextmanager
def _target(ctx: Ctx, action: str, number: int):
    """Manual commands take the same per-target lock as `vigia watch`'s jobs, so they never run alongside one."""
    with jobs.hold(ctx, jobs.target(action, number)) as got:
        if not got:
            raise SystemExit(
                f"a vigia job is working on {jobs.target(action, number)}; try again when it ends"
            )
        yield


def cmd_review(args: argparse.Namespace) -> int:
    ctx = Ctx.load(Path.cwd())
    with ctx.lock():
        pr = _pr(ctx, args.pr)
        rounds = {
            e["sha"]
            for e in ctx.ledger.events("verdict", pr=args.pr, kind=args.kind)
            if e["verdict"] != "UNAVAILABLE"
        }
        round_no = len(rounds | {pr["head"]})
        with _target(ctx, args.kind, args.pr):
            verdict = gate.review(ctx, pr, args.kind, round_no)
    return 0 if verdict in ("APPROVE", "CHANGES") else 1


def cmd_fix(args: argparse.Namespace) -> int:
    ctx = Ctx.load(Path.cwd())
    with ctx.lock(), _target(ctx, "fix", args.pr):
        ok = builder.fix(ctx, _pr(ctx, args.pr), args.stage)
    return 0 if ok else 1


def _workspace() -> tuple[workspace.Workspace, list[Ctx]] | None:
    """The workspace to work on when the command runs outside any repo but inside a workspace folder."""
    try:
        config_mod.find(Path.cwd())
        return None  # inside a repo: that repo only
    except ConfigError:
        pass
    path = workspace.find(Path.cwd())
    if path is None:
        return None
    ws = workspace.load(path)
    return ws, [Ctx.load(repo) for repo in ws.repos]


def _stop_on_sigterm() -> None:
    """Turn SIGTERM (systemctl stop, kill) into SystemExit, so a running engine session is stopped first."""

    def stop(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, stop)


def _slots(value: str) -> int:
    try:
        slots = int(value)
    except ValueError:
        slots = 0
    if not 1 <= slots <= 32:
        raise argparse.ArgumentTypeError(f"expected an integer from 1 to 32, got {value!r}")
    return slots


def _summarize(ctx: Ctx) -> None:
    try:
        print(watch.summary(ctx))
    except CommandError as exc:  # the tick already did its work: a failed read must not turn it into an error
        print(f"{ctx.cfg.repo}: summary unavailable — {exc}")


def cmd_watch(args: argparse.Namespace) -> int:
    _stop_on_sigterm()
    build = False if args.no_build else None
    found = _workspace()
    if found:
        ws, contexts = found
        if args.slots:
            ws = workspace.Workspace(ws.path, ws.repos, args.slots)
        if args.once:
            watch.workspace_tick(ws, contexts, build)
            for c in contexts:
                _summarize(c)
            return 0
        try:
            watch.workspace_loop(
                ws, contexts, args.interval or min(c.cfg.interval_seconds for c in contexts), build
            )
        except KeyboardInterrupt:
            print("\nstopped")
        return 0
    ctx = Ctx.load(Path.cwd())
    if args.slots:
        ctx.cfg = replace(ctx.cfg, slots=args.slots)
    if args.once:
        watch.tick(ctx, build)  # tick reports each decision itself, before acting on it
        _summarize(ctx)
        return 0
    try:
        watch.loop(ctx, args.interval or ctx.cfg.interval_seconds, build)
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def cmd_job(args: argparse.Namespace) -> int:
    _stop_on_sigterm()
    ctx = Ctx.load(Path.cwd())
    return 0 if jobs.run(ctx, args.action, args.number, args.stage or "", lock_fd=args.lock_fd) else 1


def cmd_workspace(args: argparse.Namespace) -> int:
    folder = Path.cwd()
    target = folder / workspace.WORKSPACE_NAME
    if target.exists() and not args.force:
        raise SystemExit(f"{target} already exists (use --force to overwrite)")
    repos = workspace.discover(folder)
    if not repos:
        raise SystemExit(
            f"no sub-folder of {folder} has a vigia.toml: clone your repos here and run `vigia init` in each"
        )
    slots = args.slots or workspace.recommend_slots()
    target.write_text(workspace.render(slots, repos), encoding="utf-8")
    print(f"Wrote {target}: {len(repos)} repo(s) ({', '.join(repos)}), {slots} slot(s).")
    print("Run `vigia watch` and `vigia status` from this folder.")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from . import report

    found = _workspace()
    contexts = found[1] if found else [Ctx.load(Path.cwd())]
    print(report.show({c.cfg.repo: c.ledger for c in contexts}, days=args.days, as_json=args.json))
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    from . import plan

    ctx = Ctx.load(Path.cwd())
    # plan.plan takes the clone's lock itself, only while it reads and while it files
    result, filed = plan.plan(ctx, args.goal, limit=args.max, dry_run=args.dry_run)
    if args.dry_run:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    for item in filed:
        print(f"#{item['number']} [{', '.join(item['labels'])}] {item['title']}")
    if not filed:
        print("The planner proposed nothing.")
    return 0


def cmd_mesa(args: argparse.Namespace) -> int:
    from . import mesa

    found = _workspace()
    contexts = found[1] if found else [Ctx.load(Path.cwd())]
    slots = found[0].slots if found else None  # a workspace shares one limit between its repos
    try:
        srv = mesa.server(contexts, args.host, args.port, tuple(args.allow_host or ()), slots=slots)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        print(f"vigia: port {args.port} is in use: pass --port with a free one", file=sys.stderr)
        return 2
    names = ", ".join(c.cfg.repo for c in contexts)
    print(f"vigia Mesa for {names}: http://{args.host}:{srv.server_address[1]}/  (read-only; Ctrl-C to stop)")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            "Warning: the Mesa has no login of its own. Prefer 127.0.0.1 behind `tailscale serve` (https://github.com/hector14mv/vigia/blob/main/docs/server.md)."
        )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def cmd_ticket(args: argparse.Namespace) -> int:
    try:
        body = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        reason = (exc.strerror or str(exc)) if isinstance(exc, OSError) else str(exc)
        print(f"vigia: cannot read {args.file}: {reason}", file=sys.stderr)
        return 2
    gaps = tickets.missing(body)
    if gaps:
        print("\n".join(["missing:", *(f"  - {gap}" for gap in gaps)]))
        return 1
    print("ok")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    found = _workspace()
    if found and not args.html:
        _, contexts = found
        if args.json:
            print("[" + ",\n".join(status.show(c, as_json=True) for c in contexts) + "]")
        else:
            print("\n\n".join(status.show(c) for c in contexts))
        return 0
    ctx = Ctx.load(Path.cwd())
    if args.html:
        try:
            print(status.write_html(ctx, Path(args.html)))
        except OSError as exc:
            raise SystemExit(f"vigia: cannot write {args.html}: {exc}") from exc
        return 0
    print(status.show(ctx, as_json=args.json))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    checks = doctor.collect(Path.cwd())
    print(doctor.to_json(checks) if args.json else doctor.render(checks))
    return 1 if doctor.problems(checks) else 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vigia",
        description="Agents build. The other lab reviews. A deterministic decision table merges only the reviewed commit.",
    )
    p.add_argument("--version", action="version", version=f"vigia {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="check tools, choose engines per tier, write vigia.toml, create labels")
    s.add_argument("--repo", help="owner/name (default: the clone's GitHub repo)")
    s.add_argument("--builder", choices=["claude", "codex"], help="which lab builds; the other one reviews")
    s.add_argument("--yes", "-y", action="store_true", help="accept the defaults without asking")
    s.add_argument("--force", action="store_true", help="overwrite an existing vigia.toml")
    s.add_argument("--no-labels", action="store_true", help="do not create labels on GitHub")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("run", help="build one ready issue into a pull request")
    s.add_argument("--issue", type=int, help="build this issue (default: the oldest ready one)")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser(
        "review", help="run the cross-lab gate (or the tier reviewer) on a PR's exact head now"
    )
    s.add_argument("--pr", type=int, required=True, help="the pull request number")
    s.add_argument(
        "--kind",
        choices=["gate", "review"],
        default="gate",
        help="gate: the other lab; review: the tier's reviewer",
    )
    s.set_defaults(func=cmd_review)

    s = sub.add_parser("fix", help="run one fix round on a PR now")
    s.add_argument("--pr", type=int, required=True, help="the pull request number")
    s.add_argument(
        "--stage", choices=["gate", "review", "ci"], default="gate", help="what failed: gate, review or ci"
    )
    s.set_defaults(func=cmd_fix)

    s = sub.add_parser("watch", help="the loop: decide with the rules, review, fix, merge, park, build")
    s.add_argument("--once", action="store_true", help="one tick, then exit")
    s.add_argument("--interval", type=int, help="seconds between ticks (default: watch.interval_seconds)")
    s.add_argument("--no-build", action="store_true", help="do not build new issues, only move open PRs")
    s.add_argument(
        "--slots", type=_slots, help="engine sessions at once (default: watch.slots, or the workspace's)"
    )
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser(
        "workspace", help="write vigia.workspace.toml here: one folder that drives every clone below it"
    )
    s.add_argument(
        "--slots",
        type=_slots,
        help="engine sessions at once across all repos (default: what this machine fits)",
    )
    s.add_argument("--force", action="store_true", help="overwrite an existing vigia.workspace.toml")
    s.set_defaults(func=cmd_workspace)

    s = sub.add_parser(
        "report", help="the engine table: per model, PRs, first-round P1s, rounds, time to merge"
    )
    s.add_argument("--days", type=int, default=7, help="how far back (default 7)")
    s.add_argument("--json", action="store_true", help="print JSON instead of a table")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser(
        "plan", help="turn a goal into proposed tickets in the template, for a person to ratify"
    )
    s.add_argument("goal", help="what you want, in a sentence or a paragraph")
    s.add_argument("--max", type=int, default=5, help="at most this many tickets (default 5)")
    s.add_argument("--dry-run", action="store_true", help="print the plan, file nothing")
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser(
        "mesa", help="the read-only status page: what needs you, every PR's next step, recent events, quota"
    )
    s.add_argument("--host", default="127.0.0.1", help="interface to listen on (default 127.0.0.1)")
    s.add_argument("--port", type=int, default=8790, help="port (default 8790)")
    s.add_argument(
        "--allow-host", action="append", help="extra host name the Mesa answers to (e.g. a reverse proxy's)"
    )
    s.set_defaults(func=cmd_mesa)

    s = sub.add_parser(
        "job"
    )  # what `vigia watch` starts for each parallel session; no help= keeps it out of --help
    s.add_argument("action", choices=jobs.ACTIONS)
    s.add_argument("number", type=int)
    s.add_argument("stage", nargs="?")
    s.add_argument(
        "--lock-fd", type=int, help=argparse.SUPPRESS
    )  # the target lock the watcher took for this job
    s.set_defaults(func=cmd_job)

    s = sub.add_parser("status", help="queue, open PRs and their next step, parked reasons, recent events")
    out = s.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true", help="print JSON instead of text")
    out.add_argument(
        "--html", metavar="PATH", help="write a self-contained HTML page here and print its path"
    )
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("ticket", help="check a ticket body (a file, or - for stdin) against the template")
    s.add_argument("file", help="the ticket body, or - to read it from stdin")
    s.set_defaults(func=cmd_ticket)

    s = sub.add_parser(
        "doctor", help="check that vigia can work in this clone, with the fix for each problem"
    )
    s.add_argument("--json", action="store_true", help="print JSON instead of text")
    s.set_defaults(func=cmd_doctor)
    # the usage line lists the commands a person types; `job` still parses, it is just not offered
    sub.metavar = "{" + ",".join(name for name in sub.choices if name != "job") + "}"
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, Busy, UnsafePath) as exc:
        print(f"vigia: {exc}", file=sys.stderr)
        return 2
    except CommandError as exc:
        print(f"vigia: {exc}", file=sys.stderr)
        return 3
