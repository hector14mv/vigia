"""Test doubles: an in-memory GitHub on top of a real local git remote, and scripted engines.

Nothing here talks to the network or to a real repository: the "remote" is a bare repo in tmp_path.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from vigia.proc import CommandError, Result


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
        self.timeline: dict[
            int, list[dict]
        ] = {}  # labeled and renamed events, oldest first, as `approval()` pages through them
        self.edited: dict[int, str] = {}  # issue -> when its body was last edited (GitHub's lastEditedAt)
        self.permissions: dict[str, str] = {login: "write"}  # anyone else reads as `read`, like a public repo
        self.event_ids = 0
        self.ci: dict[str, str] = {}  # sha -> ci state (default passing)
        self.merged: list[tuple[int, str]] = []
        self.next = 1
        self.known_labels: set[str] | None = None  # None: every label exists

    # helpers for tests
    def add_issue(self, title: str, body: str, labels: list[str], by: str | None = None) -> int:
        """An issue opened by `by` (default: this login) with `labels`: GitHub records a `labeled` event by the
        author for each of them, the template's labels included."""
        n = self.next
        self.next += 1
        self.issues_[n] = {
            "number": n,
            "title": title,
            "body": body,
            "labels": [],
            "url": "",
            "state": "OPEN",
        }
        self.add_labels(n, *labels, by=by)
        return n

    def edit(self, n: int, body: str | None = None, title: str | None = None) -> None:
        """Edit an issue the way a person does on GitHub: a body edit moves `lastEditedAt`, a title change is a
        `renamed` event."""
        if body is not None:
            self.issues_[n]["body"] = body
            self.edited[n] = self.clock()
        if title is not None:
            self.issues_[n]["title"] = title
            self.timeline.setdefault(n, []).append({"event": "renamed", "ts": self.clock()})

    def branch_sha(self, branch: str) -> str:
        return git("rev-parse", f"refs/heads/{branch}", cwd=self.origin)

    def human_label(self, n: int, actor: str = "owner") -> None:
        self.add_labels(n, "vigia:human-ok", by=actor)
        self._labeled(n, "vigia:human-ok", actor)  # a new event even when the label was already on

    def _labeled(self, n: int, label: str, actor: str) -> None:
        self.event_ids += 1
        ts = self.clock()
        self.label_events.setdefault(n, []).append((label, actor, ts, self.event_ids))
        self.timeline.setdefault(n, []).append(
            {
                "event": "labeled",
                "id": f"LE_{self.event_ids}",
                "label": label,
                "actor": actor,
                "user": not actor.endswith("[bot]"),
                "ts": ts,
            }
        )

    def bodies(self, n: int) -> list[str]:
        return [c["body"] for c in self.thread.get(n, [])]

    # the GitHub interface
    def login(self) -> str:
        return self._login

    def _item(self, n: int) -> dict:
        return self.issues_.get(n) or self.prs_[n]

    def issues_by(self, creator: str, since: str | None) -> list[dict]:
        return [dict(i) for i in self.issues_.values()]  # every issue in the fake was filed by its one login

    def open_issues(self) -> list[dict]:
        return [dict(i) for i in self.issues_.values() if i["state"] == "OPEN"]

    def issues(self, label: str) -> list[dict]:
        return [dict(i) for i in self.issues_.values() if i["state"] == "OPEN" and label in i["labels"]]

    def issue(self, n: int) -> dict:
        return dict(self.issues_[n])

    def create_issue(self, title: str, body: str, labels: list[str]) -> int:
        unknown = [name for name in labels if self.known_labels is not None and name not in self.known_labels]
        if unknown:  # what `gh issue create` does with a label the repo does not have
            raise CommandError(
                ["gh", "issue", "create"], 1, "", f"could not add label: '{unknown[0]}' not found"
            )
        return self.add_issue(title, body, labels)

    def add_labels(self, n: int, *labels: str, by: str | None = None) -> None:
        """Like GitHub: a `labeled` event, by `by` (default: this login), for each label not already on."""
        item = self._item(n)
        for label in sorted(set(labels) - set(item["labels"])):
            self._labeled(n, label, by or self._login)
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

    def ticket(self, n: int) -> dict:
        i = self.issues_[n]
        return {"title": i["title"], "body": i["body"]}

    def approval(self, n: int, label: str) -> dict:
        events = self.timeline.get(n, [])
        at = max(
            (k for k, e in enumerate(events) if e["event"] == "labeled" and e["label"] == label), default=None
        )
        e = events[at] if at is not None else None
        return {
            "ready": {k: e[k] for k in ("id", "actor", "user", "ts")} if e else None,
            "renamed": at is not None and any(x["event"] == "renamed" for x in events[at + 1 :]),
            "edited": self.edited.get(n),
            "present": label in self.issues_[n]["labels"],
        }

    def permission(self, login: str) -> str:
        return self.permissions.get(login, "read")

    def ensure_label(self, name: str, color: str, description: str) -> None:
        if self.known_labels is not None:
            self.known_labels.add(name)

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

    def merged_at(self, n: int) -> str | None:
        return "2026-10-24T00:00:00Z" if self.prs_[n]["state"] == "MERGED" else None

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

    def run(cmd, cwd, input, timeout, log, env=None, keep_fds=()):
        reviewer = "--json-schema" in cmd or "--output-schema" in cmd
        if cmd[0] == "opencode":  # its read-only rule travels in the environment, not on the command line
            reviewer = "permission" in json.loads((env or {}).get("OPENCODE_CONFIG_CONTENT", "{}"))
        calls.append(
            {
                "engine": cmd[0],
                "role": "reviewer" if reviewer else "builder",
                "cwd": cwd,
                "prompt": input,
                "keep_fds": keep_fds,
            }
        )
        if reviewer:
            verdict = json.dumps(verdicts.pop(0))
            if cmd[0] == "codex":
                Path(cmd[cmd.index("-o") + 1]).write_text(verdict)
                return Result(0, "", "")
            if cmd[0] == "opencode":
                return Result(0, opencode_events(verdict), "")
            return Result(0, json.dumps({"is_error": False, "result": verdict}), "")
        run.edit(Path(cwd), len(calls))  # type: ignore[attr-defined]
        (Path(cwd) / ".vigia-handoff.md").write_text(
            "Commit: feat: scripted change\n## Summary\n- edited textkit.py\n## Tests run\n- none\n"
            "## Deviations\n- none\n## Risks\n- none\n## Follow-ups\n- none\n"
        )
        if cmd[0] == "codex":
            Path(cmd[cmd.index("-o") + 1]).write_text("done")
            return Result(0, "", "")
        if cmd[0] == "opencode":
            return Result(0, opencode_events("done"), "")
        return Result(0, json.dumps({"is_error": False, "result": "done"}), "")

    run.calls = calls  # type: ignore[attr-defined]
    run.edit = edits or default_edit  # type: ignore[attr-defined]  # tests may swap it between ticks
    return run


def opencode_events(*texts: str | None, error: str | None = None) -> str:
    """What `opencode run --format json` printed on 1.18.32 (one real run, trimmed to the fields vigia reads and a
    few it does not): per step, a `step_start`, its `text` parts and tool calls, then a `step_finish`, whose reason
    is `tool-calls` until the last step's `stop`. Each text in `texts` is one step's message (None: a step with no
    text); a provider error is one `error` event and nothing else."""
    session = "ses_e86c4453bffeOWfUPaGiurPmj3"
    if error is not None:
        return (
            json.dumps(
                {
                    "type": "error",
                    "timestamp": 1793035305507,
                    "sessionID": session,
                    "error": {"name": "APIError", "data": {"message": error, "statusCode": 401}},
                }
            )
            + "\n"
        )
    lines = []
    for k, text in enumerate(texts):
        msg = f"msg_1793bbc12001g5olzjzH6uJU{k:02d}"
        part = {"messageID": msg, "sessionID": session}
        lines.append({"type": "step_start", "sessionID": session, "part": {**part, "type": "step-start"}})
        if text is not None:
            lines.append(
                {"type": "text", "sessionID": session, "part": {**part, "type": "text", "text": text}}
            )
        lines += [
            {
                "type": "step_finish",
                "sessionID": session,
                "part": {
                    **part,
                    "type": "step-finish",
                    "reason": "stop" if k == len(texts) - 1 else "tool-calls",
                    "tokens": {"total": 30387},
                    "cost": 0,
                },
            },
        ]
    return "".join(json.dumps(line) + "\n" for line in lines)


# What `opencode --pure debug config` printed on 1.18.32 for an empty configuration home (XDG_CONFIG_HOME a new empty
# directory, OPENCODE_DISABLE_PROJECT_CONFIG=1, a clean ~/.opencode).
OPENCODE_EMPTY_CONFIG = {
    "$schema": "https://opencode.ai/config.json",
    "agent": {},
    "command": {},
    "mode": {},
    "plugin": [],
    "username": "someone",
}

# A stand-in for the `opencode` binary: `--pure debug config` prints the resolved config from `settings.json`
# (or fails), `run` records its argv, environment, working directory and stdin to `runs.jsonl` and prints events.
# Like 1.18.32, both load the global config from $XDG_CONFIG_HOME/opencode (its opencode.json merged into the config,
# its tools/*.ts as tools) and from ~/.opencode, so a test can plant one there and see whether a session got it.
FAKE_OPENCODE = """\
import json, os, sys
from pathlib import Path

here = Path(__file__).resolve().parent
settings = json.loads((here / "settings.json").read_text())
args = sys.argv[1:]
dirs = [Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "opencode", Path.home() / ".opencode"]
tools = sorted(t.stem for d in dirs for sub in ("tool", "tools") if (d / sub).is_dir() for t in (d / sub).iterdir())
loaded = {}
for d in dirs:
    if (d / "opencode.json").is_file():
        loaded.update(json.loads((d / "opencode.json").read_text()))
record = {"argv": args, "env": dict(os.environ), "cwd": os.getcwd(), "tools": tools}
if args[:3] == ["--pure", "debug", "config"]:
    with (here / "configs.jsonl").open("a") as fh:
        fh.write(json.dumps(record) + "\\n")
    if settings.get("config_exit"):
        print("Error: config is broken", file=sys.stderr)
        sys.exit(settings["config_exit"])
    print(settings.get("config_stdout", json.dumps({**settings["config"], **loaded}, indent=2)))
    sys.exit(0)
if args[:1] == ["run"]:
    record["stdin"] = sys.stdin.read()
    with (here / "runs.jsonl").open("a") as fh:
        fh.write(json.dumps(record) + "\\n")
    sys.stdout.write(settings.get("run_stdout", ""))
    sys.exit(settings.get("run_exit", 0))
print("unexpected arguments: " + " ".join(args), file=sys.stderr)
sys.exit(2)
"""


class FakeOpencode:
    """A fake `opencode` executable in `bin_dir`; put `bin_dir` first on PATH."""

    def __init__(self, bin_dir: Path, config: dict, run_stdout: str = "", run_exit: int = 0):
        bin_dir.mkdir(parents=True, exist_ok=True)
        self.dir = bin_dir
        script = bin_dir / "opencode"
        script.write_text(f"#!{sys.executable}\n" + FAKE_OPENCODE)
        script.chmod(0o755)
        self.settings = {"config": config, "run_stdout": run_stdout, "run_exit": run_exit}
        self.save()

    def save(self) -> None:
        (self.dir / "settings.json").write_text(json.dumps(self.settings))

    def set(self, **settings) -> None:
        self.settings.update(settings)
        self.save()

    def _rows(self, name: str) -> list[dict]:
        path = self.dir / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def runs(self) -> list[dict]:
        return self._rows("runs.jsonl")

    def configs(self) -> list[dict]:
        return self._rows("configs.jsonl")


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
