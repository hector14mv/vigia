"""The `vigia` command."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from . import __version__, builder, gate, jobs, status, watch, workspace
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
            issue = ctx.gh.issue(args.issue)
            if issue["state"] != "OPEN":
                raise SystemExit(f"#{args.issue} is closed")
            problems = builder.gaps(ctx, issue)
            if problems:
                raise SystemExit(f"#{args.issue} is not launchable: " + "; ".join(problems))
        else:
            issue = builder.next_issue(ctx)
            if issue is None:
                print(f"No ready issues (label `{ctx.cfg.labels.ready}`).")
                return 0
        pr = builder.build(ctx, issue)
    return 0 if pr else 1


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
        verdict = gate.review(ctx, pr, args.kind, round_no)
    return 0 if verdict in ("APPROVE", "CHANGES") else 1


def cmd_fix(args: argparse.Namespace) -> int:
    ctx = Ctx.load(Path.cwd())
    with ctx.lock():
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


def cmd_watch(args: argparse.Namespace) -> int:
    build = False if args.no_build else None
    found = _workspace()
    if found:
        ws, contexts = found
        if args.slots:
            ws = workspace.Workspace(ws.path, ws.repos, args.slots)
        if args.once:
            watch.workspace_tick(ws, contexts, build)
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
        return 0
    try:
        watch.loop(ctx, args.interval or ctx.cfg.interval_seconds, build)
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def cmd_job(args: argparse.Namespace) -> int:
    ctx = Ctx.load(Path.cwd())
    return 0 if jobs.run(ctx, args.action, args.number, args.stage or "") else 1


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


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vigia",
        description="AI agents write the code. A different lab reviews it. A script with no AI merges it.",
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
    s.add_argument("--pr", type=int, required=True)
    s.add_argument("--kind", choices=["gate", "review"], default="gate")
    s.set_defaults(func=cmd_review)

    s = sub.add_parser("fix", help="run one fix round on a PR now")
    s.add_argument("--pr", type=int, required=True)
    s.add_argument("--stage", choices=["gate", "review", "ci"], default="gate")
    s.set_defaults(func=cmd_fix)

    s = sub.add_parser("watch", help="the loop: decide with the rules, review, fix, merge, park, build")
    s.add_argument("--once", action="store_true", help="one tick, then exit")
    s.add_argument("--interval", type=int, help="seconds between ticks (default: watch.interval_seconds)")
    s.add_argument("--no-build", action="store_true", help="do not build new issues, only move open PRs")
    s.add_argument(
        "--slots", type=int, help="engine sessions at once (default: watch.slots, or the workspace's)"
    )
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser(
        "workspace", help="write vigia.workspace.toml here: one folder that drives every clone below it"
    )
    s.add_argument(
        "--slots", type=int, help="engine sessions at once across all repos (default: what this machine fits)"
    )
    s.add_argument("--force", action="store_true", help="overwrite an existing vigia.workspace.toml")
    s.set_defaults(func=cmd_workspace)

    s = sub.add_parser("job", help=argparse.SUPPRESS)  # what `vigia watch` starts for each parallel session
    s.add_argument("action", choices=jobs.ACTIONS)
    s.add_argument("number", type=int)
    s.add_argument("stage", nargs="?")
    s.set_defaults(func=cmd_job)

    s = sub.add_parser("status", help="queue, open PRs and their next step, parked reasons, recent events")
    out = s.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true")
    out.add_argument(
        "--html", metavar="PATH", help="write a self-contained HTML page here and print its path"
    )
    s.set_defaults(func=cmd_status)
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
