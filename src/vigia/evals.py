"""Replay past pull requests through the tier's gate and score it against what is known about each one.

A case is a directory with `case.toml`, `ticket.md` and `handoff.md`. A run is one gate session as `gate.review()`
runs it (same prompt, same engine path, same parser) minus everything that touches GitHub or the ledger. Scoring
is deterministic: no model reads a finding.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import prompts
from .config import TIERS, Seat, lab_of
from .context import Ctx
from .engines import parse_verdict
from .proc import CommandError

_SHA = re.compile(r"[0-9a-f]{40}")
_KNOWN = {"pr", "head", "base", "tier", "builder", "expect", "files", "why"}

CAUGHT, MISSED, CLEAN, FALSE_ALARM, UNAVAILABLE = "caught", "missed", "clean", "false alarm", "unavailable"


class CaseError(ValueError):
    """A case that cannot run: the message names the problem."""


@dataclass(frozen=True)
class Case:
    name: str
    pr: int
    head: str
    base: str
    tier: str
    builder: Seat
    expect: str
    files: tuple[str, ...]
    why: str
    ticket: str
    handoff: str


def _path(name: str) -> str:
    return name.removeprefix("./")


def load_case(path: Path) -> Case:
    """The case in directory `path`, or CaseError naming what is wrong with it."""
    name = path.name
    texts = {}
    for file in ("case.toml", "ticket.md", "handoff.md"):
        try:
            texts[file] = (path / file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise CaseError(
                f"{file}: cannot read it ({exc.strerror if isinstance(exc, OSError) else exc})"
            ) from exc
    try:
        data = tomllib.loads(texts["case.toml"])
    except tomllib.TOMLDecodeError as exc:
        raise CaseError(f"case.toml: not valid TOML ({exc})") from exc
    unknown = sorted(set(data) - _KNOWN)
    if unknown:
        raise CaseError(f"case.toml: unknown keys {unknown}")
    pr = data.get("pr")
    if not isinstance(pr, int) or isinstance(pr, bool) or pr < 1:
        raise CaseError("case.toml: `pr` must be a positive integer")
    for key in ("head", "base"):
        value = data.get(key)
        if not isinstance(value, str) or not _SHA.fullmatch(value):
            raise CaseError(f"case.toml: `{key}` must be a full 40-character lowercase sha")
    tier = data.get("tier")
    if tier not in TIERS:
        raise CaseError(f"case.toml: `tier` must be one of {list(TIERS)}, got {tier!r}")
    raw = data.get("builder")
    if (
        not isinstance(raw, dict)
        or not all(isinstance(raw.get(k), str) and raw[k].strip() for k in ("engine", "model"))
        or set(raw) - {"engine", "model"}
    ):
        raise CaseError('case.toml: `builder` must be { engine = "…", model = "…" }')
    expect = data.get("expect")
    if expect not in ("p1", "clean"):
        raise CaseError(f'case.toml: `expect` must be "p1" or "clean", got {expect!r}')
    files = data.get("files", [])
    if not isinstance(files, list) or not all(isinstance(f, str) and f.strip() for f in files):
        raise CaseError("case.toml: `files` must be a list of paths")
    if expect == "p1" and not files:
        raise CaseError('case.toml: `expect = "p1"` needs `files`, the file(s) with the real defect')
    why = data.get("why", "")
    if not isinstance(why, str):
        raise CaseError("case.toml: `why` must be a string")
    return Case(
        name=name,
        pr=pr,
        head=data["head"],
        base=data["base"],
        tier=tier,
        builder=Seat(raw["engine"], raw["model"].strip()),
        expect=expect,
        files=tuple(_path(f.strip()) for f in files) if expect == "p1" else (),
        why=why.strip(),
        ticket=texts["ticket.md"],
        handoff=texts["handoff.md"],
    )


def score(case: Case, parsed: dict | None) -> tuple[str, bool]:
    """(outcome, a P1 sits on another file) for a parsed verdict; no verdict is `unavailable`."""
    if parsed is None:
        return UNAVAILABLE, False
    p1 = [f for f in parsed["findings"] if f["severity"] == "P1"]
    if case.expect == "clean":
        return (FALSE_ALARM if p1 else CLEAN), False
    if any(_path(f["file"]) in case.files for f in p1):
        return CAUGHT, False
    return MISSED, bool(p1)


@dataclass
class RunResult:
    case: str
    run: int
    expect: str
    outcome: str
    elsewhere: bool = False
    p1: int = 0
    p2: int = 0
    seconds: float = 0.0
    verdict: str = ""
    summary: str = ""
    findings: list[dict] = field(default_factory=list)
    failure: str = ""

    def line(self) -> str:
        shown = self.outcome + (" (P1 elsewhere)" if self.elsewhere else "")
        tail = f" — {self.failure}" if self.failure else ""
        return f"{self.case}  run {self.run}  expect {self.expect}  {shown}  {self.p1} P1 {self.p2} P2  {self.seconds:.0f}s{tail}"


@dataclass
class Job:
    case: Case
    seat: Seat


@dataclass
class Report:
    directory: Path
    runs: int
    override: Seat | None
    jobs: list[Job] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)
    results: list[RunResult] = field(default_factory=list)

    @property
    def unavailable(self) -> int:
        return sum(1 for r in self.results if r.outcome == UNAVAILABLE)

    @property
    def failed(self) -> bool:
        return bool(self.refused) or self.unavailable > 0

    def differing(self) -> list[str]:
        seen: dict[str, set[str]] = defaultdict(set)
        for r in self.results:
            seen[r.case].add(r.outcome + (" (P1 elsewhere)" if r.elsewhere else ""))
        return [name for name, outcomes in seen.items() if len(outcomes) > 1]

    def seats(self) -> list[str]:
        if self.override:
            return [self.override.label()]
        return list(dict.fromkeys(job.seat.label() for job in self.jobs))

    def summary(self) -> list[str]:
        p1 = [r for r in self.results if r.expect == "p1"]
        clean = [r for r in self.results if r.expect == "clean"]
        differing = self.differing()
        lines = [
            f"caught {sum(r.outcome == CAUGHT for r in p1)}/{len(p1)} p1 runs",
            f"false alarms {sum(r.outcome == FALSE_ALARM for r in clean)}/{len(clean)} clean runs",
            f"unavailable {self.unavailable} runs",
            "differed between runs: " + (", ".join(differing) if differing else "none"),
            "seat: " + (", ".join(self.seats()) or "none"),
        ]
        lines += [f"refused {name}: {reason}" for name, reason in self.refused]
        return lines

    def to_json(self) -> dict:
        cases = []
        for job in self.jobs:
            c = job.case
            cases.append(
                {
                    "case": c.name,
                    "status": "ran",
                    "pr": c.pr,
                    "head": c.head,
                    "base": c.base,
                    "tier": c.tier,
                    "expect": c.expect,
                    "files": list(c.files),
                    "why": c.why,
                    "builder": {"engine": c.builder.engine, "model": c.builder.model},
                    "seat": _seat_json(job.seat),
                    "runs": [r.__dict__ for r in self.results if r.case == c.name],
                }
            )
        cases += [{"case": name, "status": "refused", "reason": reason} for name, reason in self.refused]
        return {
            "directory": str(self.directory),
            "runs": self.runs,
            "seat_override": _seat_json(self.override) if self.override else None,
            "cases": cases,
            "summary": self.summary(),
            "differing": self.differing(),
            "unavailable": self.unavailable,
        }


def _seat_json(seat: Seat) -> dict:
    return {"engine": seat.engine, "model": seat.model, "effort": seat.effort}


def _discover(directory: Path, names: list[str] | None) -> list[tuple[str, Path]]:
    if names:
        return [(n, directory / n) for n in names]
    return [(p.name, p) for p in sorted(directory.iterdir()) if p.is_dir() and not p.name.startswith(".")]


def _ensure_commits(ctx: Ctx, case: Case) -> None:
    """Both commits are in the clone, after one fetch of the PR's head ref at most; CaseError otherwise."""
    if not (ctx.git.has_commit(case.head) and ctx.git.has_commit(case.base)):
        try:
            ctx.git.fetch(f"refs/pull/{case.pr}/head")
        except CommandError as exc:
            raise CaseError(
                f"head {case.head[:8]} is not in the clone and fetching PR #{case.pr} failed ({exc})"
            ) from exc
    for label, sha in (("head", case.head), ("base", case.base)):
        if not ctx.git.has_commit(sha):
            raise CaseError(f"{label} {sha[:8]} is not in the clone, even after fetching PR #{case.pr}")


def prepare(
    ctx: Ctx, directory: Path, runs: int, names: list[str] | None = None, override: Seat | None = None
) -> Report:
    """Load every case and refuse the ones that cannot run, so the session count is exact before any starts."""
    report = Report(directory, runs, override)
    for name, path in _discover(directory, names):
        try:
            if not path.is_dir():
                raise CaseError("no such case directory")
            case = load_case(path)
            seat = override or ctx.cfg.policy(case.tier).gate
            seat_lab, builder_lab = (
                lab_of(seat.engine, seat.model),
                lab_of(case.builder.engine, case.builder.model),
            )
            if seat_lab is None or builder_lab is None or seat_lab == builder_lab:
                raise CaseError(
                    f"refusing the gate: {seat.engine} (lab {seat_lab}) reviewing work of lab {builder_lab}"
                )
            _ensure_commits(ctx, case)
        except CaseError as exc:
            report.refused.append((name, str(exc)))
            continue
        report.jobs.append(Job(case, seat))
    return report


def run_once(ctx: Ctx, case: Case, seat: Seat, run: int) -> RunResult:
    """One gate session on the case's head, as `gate.review()` runs it, without GitHub or the ledger."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", case.name)
    name = f"eval-{safe}-{run}-{os.getpid()}"
    result = RunResult(case.name, run, case.expect, UNAVAILABLE)
    wt = None
    try:
        diff = ctx.git.diff(case.base, case.head)
        wt = ctx.git.worktree_detached(ctx.root / name, case.head)
        pr = {"number": case.pr, "title": f"eval case {case.name}"}
        prompt = prompts.reviewer(
            "gate",
            ctx.cfg.repo,
            pr,
            case.tier,
            case.head,
            case.base,
            {"body": case.ticket},
            case.handoff,
            diff,
            [],
            [],
        )
        outcome = ctx.engines.run(seat, "reviewer", prompt, wt, name=name)
    except Exception as exc:  # one failed session must not end the whole eval; it is scored unavailable
        result.failure = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        if wt is not None:
            ctx.git.remove_worktree(wt)
    result.seconds = outcome.seconds
    parsed = parse_verdict(outcome.text) if outcome.code == 0 and not outcome.timed_out else None
    if parsed is None:
        result.failure = (
            "timed out"
            if outcome.timed_out
            else f"exit {outcome.code}, no parsable verdict (log: {outcome.log.name})"
        )
        return result
    result.outcome, result.elsewhere = score(case, parsed)
    result.verdict, result.summary = parsed["verdict"], parsed["summary"]
    result.p1, result.p2, result.findings = parsed["p1"], parsed["p2"], parsed["findings"]
    return result


def execute(ctx: Ctx, report: Report, say: Callable[[str], None] = print) -> Report:
    """Run every prepared case `report.runs` times, one line per run."""
    for job in report.jobs:
        for n in range(1, report.runs + 1):
            result = run_once(ctx, job.case, job.seat, n)
            report.results.append(result)
            say(result.line())
    return report
