"""The command line on first contact: help, a busy port, the watch summary, `vigia ticket`."""

import argparse
import contextlib
import io
import socket

import pytest

from test_e2e import TICKET, world  # noqa: F401  (world is a fixture)
from vigia import cli, report, watch


def _help(capsys) -> str:
    with contextlib.suppress(SystemExit):
        cli.main(["--help"])
    return capsys.readouterr().out


def test_help_lists_only_real_commands_under_the_readme_tagline(capsys):
    text = " ".join(_help(capsys).split())
    assert (
        "Agents build. The other lab reviews. A deterministic decision table merges only the reviewed commit."
        in text
    )
    assert "SUPPRESS" not in text and "job" not in text
    assert "ticket" in text and "mesa" in text


def test_job_is_hidden_from_help_but_still_parses():
    args = cli.parser().parse_args(["job", "gate", "7", "ci", "--lock-fd", "9"])
    assert (args.func, args.action, args.number, args.stage, args.lock_fd) == (
        cli.cmd_job,
        "gate",
        7,
        "ci",
        9,
    )


def test_a_busy_mesa_port_gets_one_line_and_exit_2(world, monkeypatch, capsys):  # noqa: F811
    _, build = world
    ctx, _ = build([])
    monkeypatch.setattr(cli.Ctx, "load", classmethod(lambda cls, root: ctx))
    monkeypatch.setattr(cli, "_workspace", lambda: None)
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        assert cli.main(["mesa", "--port", str(port)]) == 2
    captured = capsys.readouterr()
    assert captured.err == f"vigia: port {port} is in use: pass --port with a free one\n"
    assert "Traceback" not in captured.err and captured.out == ""


def test_watch_once_ends_with_one_summary_line(world, monkeypatch, capsys):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    gh.add_issue("One", TICKET, ["vigia:ready", "tier:T0"])
    gh.add_issue("Parked", TICKET, ["vigia:ready", "vigia:parked"])
    monkeypatch.setattr(cli.Ctx, "load", classmethod(lambda cls, root: ctx))
    monkeypatch.setattr(cli, "_workspace", lambda: None)
    assert cli.main(["watch", "--once", "--no-build"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [f"{gh.repo}: 0 PRs · 1 ready · 0 sessions running"]


def test_watch_once_in_a_workspace_prints_one_line_per_repo(world, monkeypatch, capsys):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    monkeypatch.setattr(cli, "_workspace", lambda: (object(), [ctx, ctx]))
    monkeypatch.setattr(watch, "workspace_tick", lambda ws, contexts, build: None)
    assert cli.main(["watch", "--once"]) == 0
    assert capsys.readouterr().out.splitlines() == [f"{gh.repo}: 0 PRs · 0 ready · 0 sessions running"] * 2


def test_the_summary_counts_the_prs_vigia_manages(world):  # noqa: F811
    gh, build = world
    ctx, _ = build([])
    gh.add_issue("One", TICKET, ["vigia:ready", "tier:T0"])
    watch.tick(ctx)  # builds the issue into a PR
    assert watch.summary(ctx) == f"{gh.repo}: 1 PR · 0 ready · 0 sessions running"


def _data(updates: int) -> dict:
    totals = {"merged": 0, "p1_gate": 0, "p1_review": 0, "fix_rounds": 0, "parks": 0, "updates": updates}
    return {"days": 7, "repos": ["a/b"], "totals": totals, "builders": [], "reviewers": []}


def test_the_report_counts_base_updates_only_when_there_are_some():
    assert "base updates" not in report.render(_data(0))
    assert "3 base updates" in report.render(_data(3))


def test_ticket_prints_ok_for_a_complete_body(tmp_path, capsys):
    body = tmp_path / "body.md"
    body.write_text(TICKET, encoding="utf-8")
    assert cli.main(["ticket", str(body)]) == 0
    assert capsys.readouterr().out == "ok\n"


def test_ticket_lists_each_missing_item_and_exits_1(tmp_path, capsys):
    body = tmp_path / "body.md"
    body.write_text("## Outcome\nx\n", encoding="utf-8")
    assert cli.main(["ticket", str(body)]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "missing:"
    assert "  - section `## Scope`" in lines and "  - section `## Risks`" in lines and "ok" not in lines


def test_ticket_unreadable_file_prints_the_os_reason_and_exits_2(tmp_path, capsys):
    assert cli.main(["ticket", str(tmp_path / "nope.md")]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"vigia: cannot read {tmp_path / 'nope.md'}: No such file or directory\n"


def test_every_argument_has_help(capsys):
    def walk(p):
        for action in p._actions:
            if action.dest == "help":
                continue
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    if name != "job":  # the internal command is hidden on purpose
                        walk(sub)
            else:
                assert action.help, f"{p.prog}: {action.dest} has no help"

    walk(cli.parser())
    with pytest.raises(SystemExit):
        cli.main(["doctor", "--help"])
    assert "--json" in capsys.readouterr().out


def test_mesa_help_names_the_status_page(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "the read-only status page: what needs you, every PR's next step, recent events, quota" in out


def test_ticket_reads_stdin_with_a_dash(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("## Outcome\nx\n"))
    assert cli.main(["ticket", "-"]) == 1
    assert "  - section `## Scope`" in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", io.StringIO(TICKET))
    assert cli.main(["ticket", "-"]) == 0
    assert capsys.readouterr().out == "ok\n"
