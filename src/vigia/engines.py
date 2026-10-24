"""Engines: Claude Code (`claude -p`) and Codex CLI (`codex exec`), run headless, one session at a time.

A builder session may edit files and run commands inside its worktree. A reviewer session only reads.
Both return their final message; reviewers must return the verdict JSON described by `VERDICT_SCHEMA`.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import jobs, proc
from .config import DEFAULT_ENGINE_ARGS, Config, Seat

SessionRunner = Callable[..., proc.Result]

VERDICT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "summary", "findings"],
    "properties": {
        "verdict": {"type": "string", "enum": ["APPROVE", "CHANGES"]},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "file", "line", "title", "scenario"],
                "properties": {
                    "severity": {"type": "string", "enum": ["P1", "P2"]},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "title": {"type": "string"},
                    "scenario": {"type": "string"},
                },
            },
        },
    },
}


@dataclass
class Outcome:
    ok: bool
    text: str  # the session's final message
    code: int
    timed_out: bool
    seconds: float
    log: Path


class Engines:
    def __init__(self, cfg: Config, logs: Path, runner: SessionRunner = proc.run_session):
        self.cfg = cfg
        self.logs = logs
        self._run = runner

    def command(
        self, seat: Seat, role: str, workdir: Path, out_file: Path, schema_file: Path | None
    ) -> list[str]:
        extra = list(
            self.cfg.engine_args.get(seat.engine, {}).get(role, DEFAULT_ENGINE_ARGS[seat.engine][role])
        )
        if role == "reviewer":  # whatever the overrides say, a reviewer never gets write access
            if seat.engine == "codex":
                extra = [
                    a
                    for i, a in enumerate(extra)
                    if a != "--sandbox" and (i == 0 or extra[i - 1] != "--sandbox")
                ]
                extra += ["--sandbox", "read-only"]
            else:
                # a deny rule beats any allow rule, even under bypassPermissions: no shell, no writes
                extra += ["--disallowedTools", "Bash", "Edit", "Write", "NotebookEdit"]
        if seat.engine == "claude":
            cmd = ["claude", "-p", "--model", seat.model]
            if seat.effort:
                cmd += ["--effort", seat.effort]
            cmd += extra + ["--output-format", "json"]
            if schema_file is not None:
                cmd += ["--json-schema", schema_file.read_text(encoding="utf-8")]
            return cmd
        if seat.engine == "codex":
            cmd = ["codex", "exec", "-m", seat.model]
            if seat.effort:
                cmd += ["-c", f"model_reasoning_effort={seat.effort}"]
            cmd += extra + ["-C", str(workdir), "-o", str(out_file)]
            if schema_file is not None:
                cmd += ["--output-schema", str(schema_file)]
            return cmd + ["-"]  # the prompt comes on stdin
        raise ValueError(f"unknown engine {seat.engine!r}")

    def run(self, seat: Seat, role: str, prompt: str, workdir: Path, name: str) -> Outcome:
        """Run one session to completion (or to the session time cap) and return its final message."""
        self.logs.mkdir(parents=True, exist_ok=True)
        out_file = self.logs / f"{name}.last.txt"
        out_file.unlink(missing_ok=True)
        schema_file = None
        if role == "reviewer":
            schema_file = self.logs / f"{name}.schema.json"
            schema_file.write_text(json.dumps(VERDICT_SCHEMA), encoding="utf-8")
        cmd = self.command(seat, role, workdir, out_file, schema_file)
        started = time.monotonic()
        held = jobs.held()
        result = self._run(
            cmd,
            cwd=workdir,
            input=prompt,
            timeout=self.cfg.caps.session_minutes * 60,
            log=self.logs / f"{name}.log",
            **({"keep_fds": held} if held else {}),
        )
        seconds = round(time.monotonic() - started, 1)
        text = _final_text(seat.engine, result.stdout, out_file)
        ok = result.code == 0 and not result.timed_out and bool(text.strip())
        if seat.engine == "claude":
            ok = ok and not _claude_error(result.stdout)
        return Outcome(ok, text, result.code, result.timed_out, seconds, self.logs / f"{name}.log")


def _claude_error(stdout: str) -> bool:
    try:
        return bool(json.loads(stdout).get("is_error"))
    except (json.JSONDecodeError, AttributeError):
        return False


def _final_text(engine: str, stdout: str, out_file: Path) -> str:
    if engine == "claude":
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return stdout
        if isinstance(data, dict):
            if isinstance(data.get("structured_output"), dict):
                return json.dumps(data["structured_output"])
            return str(data.get("result") or "")
        return stdout
    if out_file.exists():
        return out_file.read_text(encoding="utf-8")
    return stdout


def parse_verdict(text: str) -> dict | None:
    """The reviewer's verdict as {'verdict', 'summary', 'findings', 'p1', 'p2'}, or None if there is none.

    No parsable verdict means GATE_UNAVAILABLE: content, never presence, counts as a review.
    """
    if not text:
        return None
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(body[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("verdict") not in ("APPROVE", "CHANGES"):
        return None
    findings = []
    for f in data.get("findings") or []:
        if not isinstance(f, dict) or f.get("severity") not in ("P1", "P2"):
            continue
        try:
            line = int(f.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        findings.append(
            {
                "severity": f["severity"],
                "file": str(f.get("file") or "?"),
                "line": line,
                "title": str(f.get("title") or "").strip(),
                "scenario": str(f.get("scenario") or "").strip(),
            }
        )
    p1 = sum(1 for f in findings if f["severity"] == "P1")
    verdict = data["verdict"] if p1 == 0 else "CHANGES"
    return {
        "verdict": verdict,
        "summary": str(data.get("summary") or "").strip(),
        "findings": findings,
        "p1": p1,
        "p2": len(findings) - p1,
    }


_HANDOFF_COMMIT = re.compile(r"^\s*Commit:\s*(.+?)\s*$", re.M)


def handoff_commit(text: str, fallback: str) -> str:
    """The commit subject the builder proposed in its handoff file, or `fallback`."""
    m = _HANDOFF_COMMIT.search(text or "")
    subject = m.group(1).strip().strip("`\"'") if m else ""
    if len(subject) > 72:  # keep git's one-line convention without cutting a word in half
        words = subject[:72].rsplit(" ", 1)[0].rstrip(",;:").split(" ")
        while len(words) > 3 and words[-1].lower() in {
            "and",
            "or",
            "the",
            "a",
            "an",
            "of",
            "to",
            "with",
            "for",
        }:
            words.pop()
        subject = " ".join(words).rstrip(",;:")
    return subject or fallback


def handoff_body(text: str) -> str:
    """The handoff file without its Commit: line."""
    return _HANDOFF_COMMIT.sub("", text or "", count=1).strip()
