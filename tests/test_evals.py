"""`vigia eval gate`: past PRs replayed through the gate and scored, with no GitHub and no ledger."""

import json
import subprocess
from dataclasses import replace

import pytest

from fakes import APPROVE, git
from test_e2e import world  # noqa: F401  (world is a fixture)
from vigia import cli, evals
from vigia.config import Seat
from vigia.git import Git
from vigia.proc import run as proc_run

DEFECT = "textkit.py"


def p1(file=DEFECT, line=2):
    return {
        "verdict": "CHANGES",
        "summary": "A defect.",
        "findings": [
            {"severity": "P1", "file": file, "line": line, "title": "wrong", "scenario": "f() returns 1"},
            {"severity": "P2", "file": "other.py", "line": 1, "title": "meh", "scenario": ""},
        ],
    }


def commit_on_branch(clone, name="feature"):
    """(base, head): main's tip and one commit on top of it, in the clone only."""
    base = git("rev-parse", "main", cwd=clone)
    git("checkout", "--quiet", "-b", name, cwd=clone)
    (clone / "textkit.py").write_text("def shout(text):\n    return text.lower()\n")
    git("commit", "--quiet", "-am", "break shout", cwd=clone)
    head = git("rev-parse", "HEAD", cwd=clone)
    git("checkout", "--quiet", "main", cwd=clone)
    return base, head


def toml_value(value):
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {toml_value(v)}" for k, v in value.items()) + " }"
    return json.dumps(value)  # a JSON string, number or list of strings is also valid TOML


def write_case(parent, name, base=None, head=None, **over):
    fields = {
        "pr": 17,
        "head": head,
        "base": base,
        "tier": "T1",
        "builder": {"engine": "claude", "model": "claude-sonnet-5-5"},
        "expect": "p1",
        "files": [DEFECT],
        "why": "shout() lower-cases",
    }
    fields.update(over)
    d = parent / name
    d.mkdir(parents=True)
    lines = [f"{k} = {toml_value(v)}" for k, v in fields.items() if v is not None]
    (d / "case.toml").write_text("\n".join(lines) + "\n")
    (d / "ticket.md").write_text("## Outcome\nTHE-TICKET-BODY\n")
    (d / "handoff.md").write_text("THE-HANDOFF-BODY\n")
    return d


@pytest.fixture
def lab(world, tmp_path, monkeypatch):  # noqa: F811
    gh, build = world
    clone = tmp_path / "clone"
    base, head = commit_on_branch(clone)
    cases = tmp_path / "cases"
    cases.mkdir()

    def start(verdicts):
        ctx, run = build(verdicts)
        monkeypatch.setattr(cli.Ctx, "load", classmethod(lambda cls, root: ctx))
        return ctx, run

    return gh, start, cases, base, head, clone


def eval_cli(cases, *args):
    return cli.main(["eval", "gate", str(cases), *args])


def result_file(clone):
    (path,) = (clone / ".vigia" / "evals").glob("gate-*.json")
    return json.loads(path.read_text())


@pytest.mark.parametrize(
    "case_over, verdict, outcome, elsewhere",
    [
        ({}, p1(), "caught", False),
        ({}, p1(file="./textkit.py"), "caught", False),
        ({}, p1(file="neighbour.py"), "missed", True),
        ({}, APPROVE, "missed", False),
        ({"expect": "clean", "files": None}, p1(), "false alarm", False),
        ({"expect": "clean", "files": None}, APPROVE, "clean", False),
        ({}, {"verdict": "MAYBE"}, "unavailable", False),
    ],
)
def test_scoring(lab, case_over, verdict, outcome, elsewhere, capsys):
    gh, start, cases, base, head, clone = lab
    write_case(cases, "c", base, head, **case_over)
    ctx, run = start([verdict])
    code = eval_cli(cases, "--runs", "1")
    result = result_file(clone)["cases"][0]["runs"][0]
    assert (result["outcome"], result["elsewhere"]) == (outcome, elsewhere)
    assert code == (1 if outcome == "unavailable" else 0)
    shown = outcome + (" (P1 elsewhere)" if elsewhere else "")
    assert shown in capsys.readouterr().out


def test_a_run_is_one_gate_session_with_the_real_prompt(lab):
    gh, start, cases, base, head, clone = lab
    write_case(cases, "c", base, head)
    ctx, run = start([APPROVE])
    eval_cli(cases, "--runs", "1")
    (call,) = run.calls
    assert call["role"] == "reviewer" and call["engine"] == "codex"  # T1's gate seat
    prompt = call["prompt"]
    assert "You are the gate in vigia" in prompt and "THE-TICKET-BODY" in prompt
    assert "THE-HANDOFF-BODY" in prompt and f"Reviewed commit: {head}" in prompt
    assert "return text.lower()" in prompt and "Previous round" not in prompt


def test_runs_repeat_each_case_and_differing_cases_are_listed(lab, capsys):
    gh, start, cases, base, head, clone = lab
    write_case(cases, "flaky", base, head)
    write_case(cases, "steady", base, head, expect="clean", files=None)
    ctx, run = start([p1(), APPROVE, p1(), APPROVE, APPROVE, APPROVE])
    assert eval_cli(cases, "--runs", "3") == 0
    out = capsys.readouterr().out
    assert "2 case(s) x 3 run(s) = 6 engine session(s)" in out
    assert len(run.calls) == 6
    assert "caught 2/3 p1 runs" in out and "false alarms 0/3 clean runs" in out
    assert "differed between runs: flaky" in out and "steady" not in out.split("differed between runs:")[1]
    assert "seat: codex · gpt-6.1-sol · effort medium" in out
    assert [r["outcome"] for r in result_file(clone)["cases"][0]["runs"]] == ["caught", "missed", "caught"]


def test_case_filter_picks_by_directory_name(lab, capsys):
    gh, start, cases, base, head, clone = lab
    write_case(cases, "a", base, head)
    write_case(cases, "b", base, head)
    ctx, run = start([APPROVE])
    assert eval_cli(cases, "--runs", "1", "--case", "b") == 0
    assert "1 case(s) x 1 run(s) = 1 engine session(s)" in capsys.readouterr().out
    assert [c["case"] for c in result_file(clone)["cases"]] == ["b"]


def test_a_seat_from_the_builders_lab_is_refused_and_never_starts(lab, capsys):
    gh, start, cases, base, head, clone = lab
    write_case(cases, "c", base, head)
    ctx, run = start([APPROVE])
    code = eval_cli(cases, "--engine", "claude", "--model", "claude-opus-5-5")
    assert code == 1 and run.calls == []
    out = capsys.readouterr().out
    assert "refused c: refusing the gate: claude (lab anthropic) reviewing work of lab anthropic" in out
    assert "0 case(s) x 2 run(s) = 0 engine session(s)" in out


def test_a_builder_of_unknown_lab_is_refused(lab, capsys):
    gh, start, cases, base, head, clone = lab
    write_case(cases, "c", base, head, builder={"engine": "mystery", "model": "x"})
    ctx, run = start([APPROVE])
    assert eval_cli(cases) == 1 and run.calls == []
    assert "refused c: refusing the gate" in capsys.readouterr().out


def test_an_override_seat_replaces_every_cases_gate(lab):
    gh, start, cases, base, head, clone = lab
    write_case(cases, "c", base, head, builder={"engine": "codex", "model": "gpt-6.1-sol"})
    ctx, run = start([APPROVE])
    assert (
        eval_cli(cases, "--runs", "1", "--engine", "claude", "--model", "claude-opus-5-5", "--effort", "low")
        == 0
    )
    assert run.calls[0]["engine"] == "claude"
    assert result_file(clone)["seat_override"] == {
        "engine": "claude",
        "model": "claude-opus-5-5",
        "effort": "low",
    }


def test_engine_without_model_is_an_error(lab):
    gh, start, cases, base, head, clone = lab
    start([])
    with pytest.raises(SystemExit, match="--engine and --model go together"):
        eval_cli(cases, "--engine", "claude")


def test_a_missing_head_is_fetched_once_and_a_still_missing_one_is_refused(lab, tmp_path, capsys):
    gh, start, cases, base, head, clone = lab
    other = tmp_path / "other"
    git("clone", "--quiet", str(gh.origin), str(other), cwd=tmp_path)
    git("config", "user.name", "T", cwd=other)
    git("config", "user.email", "t@example.com", cwd=other)
    (other / "textkit.py").write_text("def shout(text):\n    return None\n")
    git("commit", "--quiet", "-am", "pr head", cwd=other)
    fetched_head = git("rev-parse", "HEAD", cwd=other)
    git("push", "--quiet", "origin", "HEAD:refs/pull/5/head", cwd=other)
    assert subprocess.run(["git", "cat-file", "-e", fetched_head], cwd=clone).returncode != 0

    write_case(cases, "fetched", base, fetched_head, pr=5)
    write_case(cases, "gone", base, "a" * 40, pr=9)  # refs/pull/9/head does not exist on the remote
    write_case(cases, "present", base, head)
    ctx, run = start([APPROVE, APPROVE])
    fetches = []

    def spy(args, **kw):
        if "fetch" in args:
            fetches.append(list(args)[-1])
        return proc_run(args, **kw)

    ctx.git = Git(ctx.clone, ctx.root, runner=spy)
    assert eval_cli(cases, "--runs", "1") == 1
    assert fetches == [
        "refs/pull/5/head",
        "refs/pull/9/head",
    ]  # one fetch each; none for the head already here
    out = capsys.readouterr().out
    assert "refused gone: head aaaaaaaa is not in the clone and fetching PR #9 failed" in out
    assert [c["case"] for c in result_file(clone)["cases"]] == ["fetched", "present", "gone"]
    assert len(run.calls) == 2


def test_a_head_still_missing_after_a_good_fetch_is_refused(lab, capsys):
    gh, start, cases, base, head, clone = lab
    git(
        "push", "--quiet", "origin", "main:refs/pull/3/head", cwd=clone
    )  # the ref exists, the head is not in it
    write_case(cases, "late", base, "b" * 40, pr=3)
    ctx, run = start([APPROVE])
    assert eval_cli(cases) == 1 and run.calls == []
    assert (
        "refused late: head bbbbbbbb is not in the clone, even after fetching PR #3"
        in capsys.readouterr().out
    )


@pytest.mark.parametrize(
    "mutate, reason",
    [
        ({"head": "abc123"}, "`head` must be a full 40-character"),
        ({"base": "z" * 40}, "`base` must be a full 40-character"),
        ({"tier": "T9"}, "`tier` must be one of"),
        ({"expect": "maybe"}, '`expect` must be "p1" or "clean"'),
        ({"files": None}, "needs `files`"),
        ({"files": []}, "needs `files`"),
        ({"pr": 0}, "`pr` must be a positive integer"),
        ({"builder": None}, "`builder` must be"),
    ],
)
def test_a_malformed_case_is_refused_by_name_and_the_others_run(lab, mutate, reason, capsys):
    gh, start, cases, base, head, clone = lab
    write_case(cases, "bad", **{"base": base, "head": head, **mutate})
    write_case(cases, "good", base=base, head=head)
    ctx, run = start([APPROVE])
    assert eval_cli(cases, "--runs", "1") == 1
    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if line.startswith("refused bad:"))
    assert reason in line
    assert len(run.calls) == 1 and "1 case(s) x 1 run(s) = 1 engine session(s)" in out


@pytest.mark.parametrize("missing", ["case.toml", "ticket.md", "handoff.md"])
def test_a_case_with_a_missing_file_is_refused(lab, missing, capsys):
    gh, start, cases, base, head, clone = lab
    d = write_case(cases, "bad", base, head)
    (d / missing).unlink()
    write_case(cases, "good", base, head)
    ctx, run = start([APPROVE])
    assert eval_cli(cases, "--runs", "1") == 1
    assert f"refused bad: {missing}: cannot read it" in capsys.readouterr().out
    assert len(run.calls) == 1


def test_an_unknown_case_name_is_refused(lab, capsys):
    gh, start, cases, base, head, clone = lab
    write_case(cases, "good", base, head)
    ctx, run = start([APPROVE])
    assert eval_cli(cases, "--runs", "1", "--case", "good", "--case", "nope") == 1
    assert "refused nope: no such case directory" in capsys.readouterr().out
    assert len(run.calls) == 1


class NoGitHub:
    def __getattr__(self, name):
        raise AssertionError(f"vigia eval touched GitHub: gh.{name}")


def test_nothing_reaches_github_or_the_ledger(lab):
    gh, start, cases, base, head, clone = lab
    write_case(cases, "c", base, head)
    ctx, run = start([p1(), APPROVE])
    ledger = clone / ".vigia" / "ledger.jsonl"
    ledger.parent.mkdir(exist_ok=True)
    ledger.write_text('{"event": "kept"}\n')
    ctx.gh = NoGitHub()
    assert eval_cli(cases) == 0
    assert ledger.read_text() == '{"event": "kept"}\n'
    assert gh.thread == {} and gh.merged == [] and gh.prs_ == {}


def test_the_json_holds_every_case_run_outcome_and_finding(lab, capsys):
    gh, start, cases, base, head, clone = lab
    write_case(cases, "c", base, head)
    write_case(cases, "bad", base, head, expect="x")
    ctx, run = start([p1(), APPROVE])
    eval_cli(cases)
    out = capsys.readouterr().out
    data = result_file(clone)
    assert f"result: {next((clone / '.vigia' / 'evals').glob('gate-*.json'))}" in out
    by_name = {c["case"]: c for c in data["cases"]}
    assert by_name["bad"]["status"] == "refused" and "expect" in by_name["bad"]["reason"]
    c = by_name["c"]
    assert (c["pr"], c["tier"], c["head"], c["files"]) == (17, "T1", head, [DEFECT])
    assert c["seat"] == {"engine": "codex", "model": "gpt-6.1-sol", "effort": "medium"}
    first, second = c["runs"]
    assert (first["run"], first["outcome"], first["p1"], first["p2"]) == (1, "caught", 1, 1)
    assert [f["title"] for f in first["findings"]] == ["wrong", "meh"]
    assert first["findings"][0]["scenario"] == "f() returns 1"
    assert (second["outcome"], second["findings"]) == ("missed", [])
    assert data["differing"] == ["c"] and data["unavailable"] == 0


def test_the_worktree_is_removed_even_when_the_engine_raises(lab):
    gh, start, cases, base, head, clone = lab
    write_case(cases, "c", base, head)
    ctx, run = start([])
    seen = []

    def boom(cmd, cwd, input, timeout, log, env=None, keep_fds=()):
        seen.append(cwd)
        raise RuntimeError("engine blew up")

    ctx.engines._run = boom
    assert eval_cli(cases, "--runs", "1") == 1
    assert seen and not seen[0].exists()
    assert not [p for p in ctx.root.iterdir() if p.name.startswith("eval-")]
    assert str(seen[0]) not in git("worktree", "list", cwd=clone)
    run_result = result_file(clone)["cases"][0]["runs"][0]
    assert run_result["outcome"] == "unavailable" and "engine blew up" in run_result["failure"]


def test_a_timeout_or_failed_exit_is_unavailable_never_a_pass(lab):
    gh, start, cases, base, head, clone = lab
    write_case(cases, "c", base, head, expect="clean", files=None)
    ctx, run = start([])
    from vigia.proc import Result

    ctx.engines._run = lambda cmd, **kw: Result(1, "", "", timed_out=False)
    assert eval_cli(cases, "--runs", "1") == 1
    assert result_file(clone)["cases"][0]["runs"][0]["outcome"] == "unavailable"


def test_a_malformed_verdict_is_unavailable_and_the_eval_goes_on(lab):
    gh, start, cases, base, head, clone = lab
    write_case(cases, "a", base, head)
    write_case(cases, "b", base, head)
    ctx, run = start([{"verdict": "APPROVE", "findings": 42}, p1()])
    assert eval_cli(cases, "--runs", "1") == 1
    assert len(run.calls) == 2  # the second session still ran
    first, second = (c["runs"][0] for c in result_file(clone)["cases"])
    assert first["outcome"] == "unavailable" and "unparsable verdict" in first["failure"]
    assert second["outcome"] == "caught"
    assert not [p for p in ctx.root.iterdir() if p.name.startswith("eval-")]


def test_two_evaluations_in_the_same_second_keep_both_result_files(lab, monkeypatch):
    gh, start, cases, base, head, clone = lab
    write_case(cases, "c", base, head)

    class Frozen(cli.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 29, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(cli, "datetime", Frozen)
    start([p1(), APPROVE])
    assert eval_cli(cases, "--runs", "1") == 0
    assert eval_cli(cases, "--runs", "1") == 0
    first, second = sorted((clone / ".vigia" / "evals").glob("gate-*.json"))
    outcomes = {json.loads(f.read_text())["cases"][0]["runs"][0]["outcome"] for f in (first, second)}
    assert outcomes == {"caught", "missed"}


def test_score_reads_only_p1_on_exact_paths():
    case = evals.Case("c", 1, "a" * 40, "b" * 40, "T1", Seat("claude", "m"), "p1", ("src/a.py",), "", "", "")
    parsed = {"findings": [{"severity": "P2", "file": "src/a.py"}]}
    assert evals.score(case, parsed) == ("missed", False)  # a P2 on the right file is no catch
    parsed = {"findings": [{"severity": "P1", "file": "src/a.py.bak"}]}
    assert evals.score(case, parsed) == ("missed", True)
    assert evals.score(replace(case, expect="clean", files=()), {"findings": []}) == ("clean", False)
    assert evals.score(case, None) == ("unavailable", False)


def test_no_cases_directory_is_an_error(lab, tmp_path):
    gh, start, cases, base, head, clone = lab
    start([])
    with pytest.raises(SystemExit, match="is not a directory of eval cases"):
        eval_cli(tmp_path / "nowhere")
