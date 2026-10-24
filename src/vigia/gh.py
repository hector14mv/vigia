"""GitHub through the `gh` CLI. vigia never holds a token: `gh` uses whatever you logged in with."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import proc
from .proc import CommandError

Runner = Callable[..., proc.Result]


def ci_state(rollup: list[dict] | None) -> str:
    """passing | pending | failing | none, from `statusCheckRollup` (the checks of the PR's head commit)."""
    if not rollup:
        return "none"
    state = "passing"
    for item in rollup:
        if item.get("__typename") == "StatusContext" or "state" in item and "conclusion" not in item:
            s = (item.get("state") or "").upper()
            if s in ("ERROR", "FAILURE"):
                return "failing"
            if s != "SUCCESS":
                state = "pending"
            continue
        if (item.get("status") or "").upper() != "COMPLETED":
            state = "pending"
            continue
        if (item.get("conclusion") or "").upper() not in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            return "failing"
    return state


def _pr(data: dict) -> dict:
    return {
        "number": data["number"],
        "title": data.get("title", ""),
        "body": data.get("body") or "",
        "url": data.get("url", ""),
        "state": data.get("state", "OPEN"),
        "draft": bool(data.get("isDraft")),
        "head": data.get("headRefOid", ""),
        "branch": data.get("headRefName", ""),
        "base": data.get("baseRefName", ""),
        "labels": sorted(label["name"] for label in data.get("labels") or []),
        "mergeable": data.get("mergeable") or "UNKNOWN",
        "ci": ci_state(data.get("statusCheckRollup")),
    }


PR_FIELDS = "number,title,body,url,state,isDraft,headRefOid,headRefName,baseRefName,labels,mergeable,statusCheckRollup"


class GitHub:
    def __init__(self, repo: str, runner: Runner = proc.run, cwd: Path | None = None):
        self.repo = repo
        self._run = runner
        self._cwd = cwd  # gh is always called with -R; a neutral cwd keeps it away from local branches
        self._login: str | None = None

    # -- plumbing --
    def _gh(self, *args: str, input: str | None = None) -> str:
        return self._run(["gh", *args], cwd=self._cwd, input=input).stdout

    def _api(self, method: str, path: str, payload: dict | None = None, paginate: bool = False) -> Any:
        args = ["api", "--method", method, path]
        if paginate:
            args += ["--paginate", "--slurp"]
        if payload is not None:
            args += ["--input", "-"]
        out = self._gh(*args, input=json.dumps(payload) if payload is not None else None)
        data = json.loads(out) if out.strip() else None
        if paginate and isinstance(data, list):
            return [item for page in data for item in (page if isinstance(page, list) else [page])]
        return data

    # -- identity --
    def login(self) -> str:
        if self._login is None:
            self._login = self._gh("api", "user", "--jq", ".login").strip()
        return self._login

    # -- issues --
    def open_issues(self) -> list[dict]:
        """Every open issue, whatever its labels, across all pages (pull requests excluded)."""
        rows = self._api("GET", f"repos/{self.repo}/issues?state=open&per_page=100", paginate=True) or []
        return [
            {
                "number": r["number"],
                "title": r["title"],
                "url": r.get("html_url", ""),
                "labels": sorted(x["name"] for x in r.get("labels") or []),
            }
            for r in rows
            if "pull_request" not in r
        ]

    def issues(self, label: str) -> list[dict]:
        out = self._gh(
            "issue",
            "list",
            "-R",
            self.repo,
            "--state",
            "open",
            "--label",
            label,
            "--limit",
            "100",
            "--json",
            "number,title,body,labels,url",
        )
        rows = json.loads(out or "[]")
        return sorted(
            (
                {
                    "number": r["number"],
                    "title": r["title"],
                    "body": r.get("body") or "",
                    "url": r.get("url", ""),
                    "labels": sorted(label["name"] for label in r.get("labels") or []),
                }
                for r in rows
            ),
            key=lambda r: r["number"],
        )

    def create_issue(self, title: str, body: str, labels: list[str]) -> int:
        args = ["issue", "create", "-R", self.repo, "--title", title, "--body-file", "-"]
        for name in labels:
            args += ["--label", name]
        url = self._gh(*args, input=body).strip().splitlines()[-1]
        return int(url.rstrip("/").rsplit("/", 1)[-1])

    def issue(self, number: int) -> dict:
        r = json.loads(
            self._gh(
                "issue", "view", str(number), "-R", self.repo, "--json", "number,title,body,labels,url,state"
            )
        )
        return {
            "number": r["number"],
            "title": r["title"],
            "body": r.get("body") or "",
            "url": r.get("url", ""),
            "state": r.get("state", "OPEN"),
            "labels": sorted(label["name"] for label in r.get("labels") or []),
        }

    def add_labels(self, number: int, *labels: str) -> None:
        if labels:
            self._api("POST", f"repos/{self.repo}/issues/{number}/labels", {"labels": list(labels)})

    def remove_label(self, number: int, label: str) -> None:
        try:
            self._api("DELETE", f"repos/{self.repo}/issues/{number}/labels/{quote(label, safe='')}")
        except CommandError as exc:
            if "404" not in str(exc) and "Not Found" not in (exc.stderr or ""):
                raise

    def comment(self, number: int, body: str) -> dict:
        data = self._api("POST", f"repos/{self.repo}/issues/{number}/comments", {"body": body})
        return {"id": data.get("id"), "url": data.get("html_url")}

    def comments(self, number: int) -> list[dict]:
        rows = (
            self._api("GET", f"repos/{self.repo}/issues/{number}/comments?per_page=100", paginate=True) or []
        )
        return [
            {
                "author": (r.get("user") or {}).get("login"),
                "body": r.get("body") or "",
                "ts": r.get("created_at"),
            }
            for r in rows
        ]

    def label_event(self, number: int, label: str) -> dict | None:
        """{'actor', 'ts', 'id'} of the latest time `label` was added (from the issue's event history), or None.

        `id` is GitHub's own event id; it grows with each event, so two ids compare in GitHub's order.
        """
        rows = self._api("GET", f"repos/{self.repo}/issues/{number}/events?per_page=100", paginate=True) or []
        found = None
        for r in rows:
            if r.get("event") == "labeled" and (r.get("label") or {}).get("name") == label:
                found = {
                    "actor": (r.get("actor") or {}).get("login"),
                    "ts": r.get("created_at") or "",
                    "id": r.get("id"),
                }
        return found

    def ensure_label(self, name: str, color: str, description: str) -> None:
        self._gh(
            "label",
            "create",
            name,
            "-R",
            self.repo,
            "--color",
            color,
            "--description",
            description,
            "--force",
        )

    # -- pull requests --
    def prs(self, label: str) -> list[dict]:
        """The open PRs carrying `label`, sorted by number.

        `gh pr list` only picks the numbers: its `statusCheckRollup` can lag minutes behind GitHub, so each
        PR's state is read with `gh pr view`, the same call `pr()` makes. The view is fresher than the
        listing, so a PR closed or stripped of `label` in between is dropped.
        """
        out = self._gh(
            "pr",
            "list",
            "-R",
            self.repo,
            "--state",
            "open",
            "--label",
            label,
            "--limit",
            "100",
            "--json",
            "number",
        )
        numbers = sorted(r["number"] for r in json.loads(out or "[]"))
        fresh = [self.pr(n) for n in numbers]
        return [p for p in fresh if p["state"] == "OPEN" and label in p["labels"]]

    def pr(self, number: int) -> dict:
        return _pr(json.loads(self._gh("pr", "view", str(number), "-R", self.repo, "--json", PR_FIELDS)))

    def create_pr(self, branch: str, base: str, title: str, body: str) -> int:
        url = (
            self._gh(
                "pr",
                "create",
                "-R",
                self.repo,
                "--head",
                branch,
                "--base",
                base,
                "--title",
                title,
                "--body-file",
                "-",
                input=body,
            )
            .strip()
            .splitlines()[-1]
        )
        return int(url.rstrip("/").rsplit("/", 1)[-1])

    def merge(self, number: int, sha: str) -> None:
        """Squash-merge only if the PR's head is still exactly `sha` (GitHub refuses otherwise)."""
        self._gh("pr", "merge", str(number), "-R", self.repo, "--squash", "--match-head-commit", sha)

    def failed_log(self, branch: str, sha: str, lines: int = 200) -> str:
        """The tail of the failed steps' log of the latest failed CI run on exactly `sha`."""
        runs = json.loads(
            self._gh(
                "run",
                "list",
                "-R",
                self.repo,
                "--branch",
                branch,
                "--limit",
                "20",
                "--json",
                "databaseId,headSha,conclusion",
            )
            or "[]"
        )
        for r in runs:
            if r.get("headSha") == sha and r.get("conclusion") == "failure":
                out = self._gh("run", "view", str(r["databaseId"]), "-R", self.repo, "--log-failed")
                return "\n".join(out.splitlines()[-lines:])
        return ""

    def delete_branch(self, branch: str) -> None:
        # Already gone, or protected: not worth failing a merge over.
        with contextlib.suppress(CommandError):
            self._api("DELETE", f"repos/{self.repo}/git/refs/heads/{branch}")
