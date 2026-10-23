import json
from pathlib import Path

from vigia.config import Config, Seat, default_tiers
from vigia.engines import Engines, handoff_body, handoff_commit, parse_verdict
from vigia.proc import Result

CFG = Config(repo="o/r", base="main", tiers=default_tiers())


def test_claude_builder_command(tmp_path):
    cmd = Engines(CFG, tmp_path).command(
        Seat("claude", "claude-sonnet-5-5", "medium"), "builder", tmp_path, tmp_path / "o", None
    )
    assert cmd[:6] == ["claude", "-p", "--model", "claude-sonnet-5-5", "--effort", "medium"]
    assert "acceptEdits" in cmd and cmd[-2:] == ["--output-format", "json"]


def test_codex_reviewer_command_is_read_only_with_schema(tmp_path):
    schema = tmp_path / "s.json"
    schema.write_text("{}")
    cmd = Engines(CFG, tmp_path).command(
        Seat("codex", "gpt-6.1-sol", "high"), "reviewer", tmp_path, tmp_path / "o", schema
    )
    assert cmd[:4] == ["codex", "exec", "-m", "gpt-6.1-sol"]
    assert "model_reasoning_effort=high" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert cmd[cmd.index("--output-schema") + 1] == str(schema)
    assert cmd[-1] == "-"  # prompt on stdin


def test_claude_reviewer_cannot_edit(tmp_path):
    schema = tmp_path / "s.json"
    schema.write_text('{"type": "object"}')
    cmd = Engines(CFG, tmp_path).command(
        Seat("claude", "claude-opus-5-5"), "reviewer", tmp_path, tmp_path / "o", schema
    )
    banned = cmd[cmd.index("--disallowedTools") + 1 :]
    assert "Edit" in banned and "Write" in banned
    assert "--json-schema" in cmd


def test_run_reads_codex_final_message_and_claude_json(tmp_path):
    seen = {}

    def runner(cmd, cwd, input, timeout, log):
        seen.update(cmd=cmd, input=input, timeout=timeout)
        if cmd[0] == "codex":
            out = Path(cmd[cmd.index("-o") + 1])
            out.write_text('{"verdict": "APPROVE", "summary": "ok", "findings": []}')
            return Result(0, "", "")
        return Result(0, json.dumps({"is_error": False, "result": "done"}), "")

    eng = Engines(CFG, tmp_path / "logs", runner=runner)
    out = eng.run(Seat("codex", "gpt-6.1-sol"), "reviewer", "PROMPT", tmp_path, "g1")
    assert out.ok and parse_verdict(out.text)["verdict"] == "APPROVE"
    assert seen["input"] == "PROMPT" and seen["timeout"] == CFG.caps.session_minutes * 60
    out = eng.run(Seat("claude", "claude-sonnet-5-5"), "builder", "P", tmp_path, "b1")
    assert out.ok and out.text == "done"


def test_run_is_not_ok_on_error_or_timeout(tmp_path):
    def err(cmd, cwd, input, timeout, log):
        return Result(0, json.dumps({"is_error": True, "result": "rate limited"}), "")

    def slow(cmd, cwd, input, timeout, log):
        return Result(-9, "", "", timed_out=True)

    seat = Seat("claude", "claude-sonnet-5-5")
    assert not Engines(CFG, tmp_path, runner=err).run(seat, "builder", "p", tmp_path, "x").ok
    assert not Engines(CFG, tmp_path, runner=slow).run(seat, "builder", "p", tmp_path, "y").ok


def test_parse_verdict_accepts_fenced_json_and_counts():
    text = (
        '```json\n{"verdict": "CHANGES", "summary": "s", "findings": ['
        '{"severity": "P1", "file": "a.py", "line": "3", "title": "t", "scenario": "x"},'
        '{"severity": "P2", "file": "b.py", "line": 1, "title": "u", "scenario": ""}]}\n```'
    )
    v = parse_verdict(text)
    assert (v["verdict"], v["p1"], v["p2"]) == ("CHANGES", 1, 1)
    assert v["findings"][0]["line"] == 3


def test_parse_verdict_approve_with_p1_becomes_changes():
    text = json.dumps(
        {
            "verdict": "APPROVE",
            "summary": "",
            "findings": [{"severity": "P1", "file": "a", "line": 1, "title": "t", "scenario": "s"}],
        }
    )
    assert parse_verdict(text)["verdict"] == "CHANGES"


def test_no_verdict_is_none():
    for text in ["", "Looks good to me!", '{"verdict": "LGTM"}', "{not json}"]:
        assert parse_verdict(text) is None


def test_handoff_commit_line():
    notes = 'Commit: "feat: add truncate()"\n## Summary\n- x'
    assert handoff_commit(notes, "fallback") == "feat: add truncate()"
    assert handoff_commit("## Summary", "fallback") == "fallback"
    assert handoff_body(notes).startswith("## Summary")
    long = "Commit: fix: keep ledger events after a torn tail, escape TOML control characters, keep fenced content"
    subject = handoff_commit(long, "fallback")
    assert subject == "fix: keep ledger events after a torn tail, escape TOML control"
    dangling = "Commit: fix: keep approvals and reviews tied to the right head, base and enrollment state"
    assert handoff_commit(dangling, "x") == "fix: keep approvals and reviews tied to the right head, base"
