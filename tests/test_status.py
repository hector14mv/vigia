import json
import re

import pytest

from fakes import Clock, FakeGitHub, make_repo
from vigia import cli, status
from vigia.config import Config, default_tiers
from vigia.context import Ctx
from vigia.engines import Engines
from vigia.git import Git
from vigia.ledger import Ledger

XSS = "<script>alert(1)</script>"


def data() -> dict:
    return {
        "repo": "owner/repo",
        "base": "main",
        "merged": 4,
        "queue": [
            {"number": 7, "title": f"queued {XSS}", "tier": "T1"},
            {"number": 9, "title": "plain", "tier": None},
        ],
        "building": [{"number": 8, "title": f"building {XSS}"}],
        "parked_issues": [{"number": 5, "title": f"parked issue {XSS}"}],
        "prs": [
            {
                "number": 12,
                "title": f"title {XSS}",
                "url": "https://github.com/owner/repo/pull/12",
                "tier": "T1",
                "head": "0123456789abcdef0123456789abcdef01234567",
                "ci": "passing",
                "next": "merge",
                "reason": f"reason {XSS}",
                "parked": False,
            },
            {
                "number": 13,
                "title": "parked pr",
                "url": 'javascript:alert("x")',
                "tier": "T2",
                "head": "fedcba9876543210fedcba9876543210fedcba98",
                "ci": "failing",
                "next": "park",
                "reason": "fix budget spent",
                "parked": True,
            },
        ],
        "recent": [
            {"ts": "2026-10-24T10:00:00Z", "event": "parked", "pr": 13, "reason": f"why {XSS}"},
            {"ts": "2026-10-24T10:00:01Z", "event": "merged", "pr": 12, "sha": "0123456789abcdef"},
        ],
    }


def test_html_is_self_contained():
    page = status.render_html(data())
    assert page.startswith("<!doctype html>")
    assert "<script" not in page.lower().replace("&lt;script&gt;", "")
    assert not re.search(r"https?://[^\s\"']*\.(js|css|png|woff2?)", page)
    assert "<link" not in page and "<img" not in page and "@import" not in page
    assert "prefers-color-scheme:dark" in page
    assert 'name="viewport"' in page


def test_html_escapes_everything_from_github_or_the_ledger():
    page = status.render_html(data())
    assert XSS not in page
    assert (
        page.count("&lt;script&gt;alert(1)&lt;/script&gt;") == 6
    )  # queue, building, pr title, reason, parked, ledger
    assert 'href="javascript:' not in page  # a hostile URL is never linked
    page = status.render_html({**data(), "repo": XSS, "base": XSS})
    assert XSS not in page


def test_html_shows_each_section():
    page = status.render_html(data())
    assert "owner/repo" in page and "4 merged" in page
    assert "Queue (2 ready)" in page and "#7" in page and ">??<" in page
    assert "<h2>Building</h2>" in page
    assert "Pull requests (2 open)" in page
    assert '<a href="https://github.com/owner/repo/pull/12">#12</a>' in page
    assert "<code>01234567</code>" in page and "<code>01234567" + "89" not in page
    assert "ci passing" in page and "ci failing" in page
    assert "merge" in page and "fix budget spent" in page
    assert page.count("PARKED") == 1 and "is-parked" in page
    assert "<h2>Parked issues</h2>" in page
    assert "<h2>Recent ledger</h2>" in page and "merged 01234567" in page


def test_html_empty_state_and_optional_sections():
    empty = {**data(), "queue": [], "building": [], "parked_issues": [], "prs": [], "recent": []}
    page = status.render_html(empty)
    assert "Queue (0 ready)" in page and "Pull requests (0 open)" in page
    assert page.count("(empty)") == 2 and "(none)" in page
    assert "Building" not in page and "Parked issues" not in page


def test_text_renderer_is_unchanged():
    text = status.render(data())
    assert text.startswith("vigia · owner/repo (base main) · 4 merged by vigia")
    assert "  2026-10-24T10:00:01Z  PR #12   merged 01234567" in text


def test_cli_writes_the_page_and_prints_its_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.Ctx, "load", classmethod(lambda cls, root: object()))
    monkeypatch.setattr(status, "collect", lambda ctx: data())
    out = tmp_path / "out.html"
    assert cli.main(["status", "--html", str(out)]) == 0
    assert capsys.readouterr().out.strip() == str(out.resolve())
    assert "Pull requests (2 open)" in out.read_text(encoding="utf-8")


def test_cli_reports_an_unwritable_path(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.Ctx, "load", classmethod(lambda cls, root: object()))
    monkeypatch.setattr(status, "collect", lambda ctx: data())
    with pytest.raises(SystemExit, match="cannot write"):
        cli.main(["status", "--html", str(tmp_path / "missing" / "out.html")])


def fake_ctx(tmp_path) -> tuple[Ctx, FakeGitHub]:
    origin, clone = make_repo(tmp_path)
    clock = Clock()
    gh = FakeGitHub(origin, clock=clock)
    cfg = Config(repo=gh.repo, base="main", tiers=default_tiers("claude"), test_command="true")
    ctx = Ctx(
        cfg=cfg,
        clone=clone,
        gh=gh,
        git=Git(clone, tmp_path / "work"),
        ledger=Ledger(clone / ".vigia" / "ledger.jsonl", clock=clock),
        engines=Engines(cfg, clone / ".vigia" / "logs", runner=lambda *a, **k: None),
        root=tmp_path / "work",
        say=lambda _: None,
    )
    return ctx, gh


def test_json_has_exactly_the_documented_keys(tmp_path):
    ctx, gh = fake_ctx(tmp_path)
    labels = ctx.cfg.labels
    gh.add_issue("queued", "", [labels.ready, f"{labels.tier_prefix}T0"])
    gh.add_issue("building", "", [labels.ready, labels.building])
    gh.add_issue("parked", "", [labels.parked])
    ctx.ledger.append("merged", pr=1, sha="abc", tier="T0", issue=None)

    out = json.loads(status.show(ctx, as_json=True))

    assert set(out) == {"repo", "base", "queue", "building", "parked_issues", "prs", "recent", "merged"}
    assert out["repo"] == "acme/textkit" and out["base"] == "main" and out["merged"] == 1
    assert [i["title"] for i in out["queue"]] == ["queued"]
    assert [i["title"] for i in out["building"]] == ["building"]
    assert [i["title"] for i in out["parked_issues"]] == ["parked"]
    assert out["prs"] == [] and [r["event"] for r in out["recent"]] == ["merged"]


def test_cli_json_prints_the_same_keys(tmp_path, monkeypatch, capsys):
    ctx, _ = fake_ctx(tmp_path)
    monkeypatch.setattr(cli.Ctx, "load", classmethod(lambda cls, root: ctx))
    assert cli.main(["status", "--json"]) == 0
    assert set(json.loads(capsys.readouterr().out)) == {
        "repo",
        "base",
        "queue",
        "building",
        "parked_issues",
        "prs",
        "recent",
        "merged",
    }


def test_json_and_html_cannot_be_combined(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["status", "--json", "--html", str(tmp_path / "out.html")])
    assert exc.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err
    assert not (tmp_path / "out.html").exists()
