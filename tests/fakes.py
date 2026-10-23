"""Test doubles: an in-memory GitHub on top of a real local git remote, and scripted engines.

Nothing here talks to the network or to a real repository: the "remote" is a bare repo in tmp_path.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

from vigia.proc import Result


class Clock:
    """One clock for the ledger and the fake GitHub, one second per reading, so event order is exact."""

    def __init__(self) -> None:
        self.seconds = 0

    def __call__(self) -> str:
        self.seconds += 1
        minutes, seconds = divmod(self.seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"2026-10-24T{hours:02d}:{minutes:02d}:{seconds:02d}Z"


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A bare 'origin' and a clone of it with one commit on main."""
    origin = tmp_path / "origin.git"
    git("init", "--quiet", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    clone = tmp_path / "clone"
    git("clone", "--quiet", str(origin), str(clone), cwd=tmp_path)
    for key, value in (
        ("user.name", "Test"),
        ("user.email", "test@example.com"),
        ("commit.gpgsign", "false"),
    ):
        git("config", key, value, cwd=clone)
    (clone / "textkit.py").write_text("def shout(text):\n    return text.upper()\n")
    git("add", "-A", cwd=clone)
    git("commit", "--quiet", "-m", "initial", cwd=clone)
    git("push", "--quiet", "origin", "HEAD:main", cwd=clone)
    return origin, clone


class FakeGitHub:
    def __init__(
        self, origin: Path, repo: str = "acme/textkit", login: str = "owner", clock: Clock | None = None
    ):
        self.origin = origin
        self.clock = clock or Clock()
        self.repo = repo
        self._login = login
        self.issues_: dict[int, dict] = {}
        self.prs_: dict[int, dict] = {}
        self.thread: dict[int, list[dict]] = {}
        self.label_events: dict[int, list[tuple[str, str, str, int]]] = {}  # (label, actor, ts, event id)
        self.event_ids = 0
        self.ci: dict[str, str] = {}  # sha -> ci state (default passing)
        self.merged: list[tuple[int, str]] = []
        self.next = 1

    # helpers for tests
    def add_issue(self, title: str, body: str, labels: list[str]) -> int:
        n = self.next
        self.next += 1
        self.issues_[n] = {
            "number": n,
            "title": title,
            "body": body,
            "labels": sorted(labels),
            "url": "",
            "state": "OPEN",
        }
        return n

    def branch_sha(self, branch: str) -> str:
        return git("rev-parse", f"refs/heads/{branch}", cwd=self.origin)

    def human_label(self, n: int, actor: str = "owner") -> None:
        self.add_labels(n, "vigia:human-ok")
        self.event_ids += 1
        self.label_events.setdefault(n, []).append(("vigia:human-ok", actor, self.clock(), self.event_ids))

    def bodies(self, n: int) -> list[str]:
        return [c["body"] for c in self.thread.get(n, [])]

    # the GitHub interface
    def login(self) -> str:
        return self._login

    def _item(self, n: int) -> dict:
        return self.issues_.get(n) or self.prs_[n]

    def issues(self, label: str) -> list[dict]:
        return [dict(i) for i in self.issues_.values() if i["state"] == "OPEN" and label in i["labels"]]

    def issue(self, n: int) -> dict:
        return dict(self.issues_[n])

    def add_labels(self, n: int, *labels: str) -> None:
        item = self._item(n)
        item["labels"] = sorted(set(item["labels"]) | set(labels))

    def remove_label(self, n: int, label: str) -> None:
        item = self._item(n)
        item["labels"] = sorted(set(item["labels"]) - {label})

    def comment(self, n: int, body: str) -> dict:
        self.thread.setdefault(n, []).append({"author": self._login, "body": body, "ts": ""})
        return {"id": len(self.thread[n]), "url": ""}

    def comments(self, n: int) -> list[dict]:
        return list(self.thread.get(n, []))

    def label_event(self, n: int, label: str) -> dict | None:
        found = [(a, ts, i) for name, a, ts, i in self.label_events.get(n, []) if name == label]
        return {"actor": found[-1][0], "ts": found[-1][1], "id": found[-1][2]} if found else None

    def ensure_label(self, name: str, color: str, description: str) -> None:
        pass

    def _view(self, p: dict) -> dict:
        view = dict(p)
        if view["state"] == "OPEN":
            view["head"] = self.branch_sha(p["branch"])
        view["ci"] = self.ci.get(view["head"], "passing")
        view["labels"] = list(p["labels"])
        return view

    def prs(self, label: str) -> list[dict]:
        return [self._view(p) for p in self.prs_.values() if p["state"] == "OPEN" and label in p["labels"]]

    def pr(self, n: int) -> dict:
        return self._view(self.prs_[n])

    def create_pr(self, branch: str, base: str, title: str, body: str) -> int:
        n = self.next
        self.next += 1
        self.prs_[n] = {
            "number": n,
            "title": title,
            "body": body,
            "url": "",
            "state": "OPEN",
            "draft": False,
            "head": self.branch_sha(branch),
            "branch": branch,
            "base": base,
            "labels": [],
            "mergeable": "MERGEABLE",
        }
        return n

    def merge(self, n: int, sha: str) -> None:
        p = self.prs_[n]
        if self.branch_sha(p["branch"]) != sha:
            raise AssertionError("merge with a stale --match-head-commit")
        p["state"], p["head"] = "MERGED", sha
        self.merged.append((n, sha))

    def delete_branch(self, branch: str) -> None:
        pass

    def failed_log(self, branch: str, sha: str) -> str:
        return "E   AssertionError: tests failed"


def engine(verdicts: list[dict], edits: Callable[[Path, int], None] | None = None):
    """A scripted engine runner: builders edit the worktree; reviewers return the next verdict."""
    calls: list[dict] = []

    def default_edit(cwd: Path, k: int) -> None:
        with (cwd / "textkit.py").open("a") as fh:
            fh.write(f"\n\ndef change_{k}():\n    return {k}\n")

    def run(cmd, cwd, input, timeout, log):
        reviewer = "--json-schema" in cmd or "--output-schema" in cmd
        calls.append(
            {"engine": cmd[0], "role": "reviewer" if reviewer else "builder", "cwd": cwd, "prompt": input}
        )
        if reviewer:
            verdict = json.dumps(verdicts.pop(0))
            if cmd[0] == "codex":
                Path(cmd[cmd.index("-o") + 1]).write_text(verdict)
                return Result(0, "", "")
            return Result(0, json.dumps({"is_error": False, "result": verdict}), "")
        run.edit(Path(cwd), len(calls))  # type: ignore[attr-defined]
        (Path(cwd) / ".vigia-handoff.md").write_text(
            "Commit: feat: scripted change\n## Summary\n- edited textkit.py\n## Tests run\n- none\n"
            "## Deviations\n- none\n## Risks\n- none\n## Follow-ups\n- none\n"
        )
        if cmd[0] == "codex":
            Path(cmd[cmd.index("-o") + 1]).write_text("done")
            return Result(0, "", "")
        return Result(0, json.dumps({"is_error": False, "result": "done"}), "")

    run.calls = calls  # type: ignore[attr-defined]
    run.edit = edits or default_edit  # type: ignore[attr-defined]  # tests may swap it between ticks
    return run


APPROVE = {"verdict": "APPROVE", "summary": "Looks correct.", "findings": []}
CHANGES = {
    "verdict": "CHANGES",
    "summary": "One blocking defect.",
    "findings": [
        {
            "severity": "P1",
            "file": "textkit.py",
            "line": 4,
            "title": "wrong result",
            "scenario": "f() returns 1",
        }
    ],
}
