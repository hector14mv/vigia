"""`vigia init`: check the tools, choose the engines per tier, write vigia.toml, create the labels."""

from __future__ import annotations

import json
import shutil
import tomllib
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from . import config as config_mod
from . import proc, tickets
from .config import TIERS, Config, Labels, Seat, default_tiers, other
from .gh import GitHub
from .proc import CommandError

Ask = Callable[[str, str], str]
Say = Callable[[str], None]


def label_specs(labels: Labels) -> list[tuple[str, str, str]]:
    return [
        (labels.ready, "0E8A16", "Ticket is complete: vigia may build it"),
        (labels.building, "FBCA04", "A vigia builder is working on it"),
        (labels.review, "1D76DB", "Managed by vigia: CI, cross-lab gate, tier rules, merge"),
        (labels.parked, "B60205", "vigia stopped here; the comment says why. Remove to resume"),
        (labels.human_ok, "5319E7", "A human checked this exact head (T2)"),
        (labels.tier("T0"), "C2E0C6", "Cosmetic, docs, tests, internal tooling"),
        (labels.tier("T1"), "FEF2C0", "Product logic with no money and no other people's data"),
        (labels.tier("T2"), "F9D0C4", "Money, auth, data, migrations, customer messages, credentials"),
    ]


def _ok(runner: Callable[..., proc.Result], args: list[str]) -> proc.Result | None:
    try:
        return runner(args, check=False, timeout=60)
    except CommandError:
        return None


def preflight(runner: Callable[..., proc.Result] = proc.run) -> list[tuple[str, bool, str]]:
    """(check, ok, detail) for git, gh, claude and codex."""
    out = []
    r = _ok(runner, ["git", "--version"])
    out.append(("git", bool(r and r.code == 0), (r.stdout.strip() if r else "not found")))
    r = _ok(runner, ["gh", "auth", "status"])
    out.append(
        ("gh logged in", bool(r and r.code == 0), "run `gh auth login`" if not (r and r.code == 0) else "ok")
    )
    r = _ok(runner, ["claude", "auth", "status"])
    claude_ok = False
    if r and r.code == 0:
        try:
            claude_ok = bool(json.loads(r.stdout).get("loggedIn"))
        except json.JSONDecodeError:
            claude_ok = "logged in" in r.stdout.lower()
    out.append(
        (
            "claude logged in",
            claude_ok,
            "ok" if claude_ok else "install Claude Code and run `claude auth login`",
        )
    )
    r = _ok(runner, ["codex", "login", "status"])
    text = ((r.stdout + r.stderr) if r else "").lower()
    codex_ok = bool(r and r.code == 0 and "logged in" in text and "not logged in" not in text)
    out.append(("codex logged in", codex_ok, "ok" if codex_ok else "install Codex CLI and run `codex login`"))
    return out


def _detect(runner: Callable[..., proc.Result], cwd: Path) -> tuple[str | None, str]:
    r = _ok(runner, ["gh", "repo", "view", "--json", "nameWithOwner,defaultBranchRef"])
    if r and r.code == 0:
        try:
            data = json.loads(r.stdout)
            return data.get("nameWithOwner"), (data.get("defaultBranchRef") or {}).get("name") or "main"
        except json.JSONDecodeError:
            pass
    return None, "main"


def _test_command(root: Path) -> str:
    if (root / "pyproject.toml").exists() or (root / "setup.py").exists():
        python = "python" if shutil.which("python") else "python3"  # macOS ships only python3
        return f"{python} -m pytest -q"
    if (root / "package.json").exists():
        return "npm test"
    if (root / "go.mod").exists():
        return "go test ./..."
    if (root / "Cargo.toml").exists():
        return "cargo test"
    return ""


def _ask_seat(ask: Ask, what: str, seat: Seat) -> Seat:
    model = ask(f"  {what} model ({seat.engine})", seat.model).strip() or seat.model
    effort = ask(f"  {what} effort", seat.effort or "").strip() or seat.effort
    return replace(seat, model=model, effort=effort or None)


def init(
    cwd: Path,
    ask: Ask,
    say: Say,
    runner: Callable[..., proc.Result] = proc.run,
    repo: str | None = None,
    builder: str | None = None,
    assume_yes: bool = False,
    force: bool = False,
    create_labels: bool = True,
) -> Path:
    top = _ok(runner, ["git", "rev-parse", "--show-toplevel"])
    if not top or top.code != 0:
        raise SystemExit("vigia init must run inside a git clone of the repository vigia will work on")
    root = Path(top.stdout.strip())
    target = root / config_mod.CONFIG_NAME
    if target.exists() and not force:
        raise SystemExit(f"{target} already exists (use --force to overwrite)")

    say("Checking tools")
    results = preflight(runner)
    for name, ok, detail in results:
        say(f"  {'ok ' if ok else 'NO '} {name}" + ("" if ok else f" — {detail}"))
    if not all(ok for _, ok, _ in results):
        raise SystemExit("vigia needs git, gh and both engines logged in: one lab builds, the other reviews")

    detected, default_base = _detect(runner, root)
    repo = repo or (detected if assume_yes and detected else ask("Repository (owner/name)", detected or ""))
    base = default_base if assume_yes else ask("Base branch", default_base)
    if builder is None:
        builder = (
            "claude"
            if assume_yes
            else ask("Which engine builds? (claude or codex; the other one reviews)", "claude")
        )
    builder = builder.strip().lower()
    if builder not in ("claude", "codex"):
        raise SystemExit(f"unknown engine {builder!r}: choose claude or codex")
    tiers = default_tiers(builder)
    say(f"{builder} builds, {other(builder)} gates every PR (the other lab).")
    for tier in TIERS:
        p = tiers[tier]
        say(
            f"  {tier}: builder {p.builder.label()} · gate {p.gate.label()}"
            + (f" · reviewer {p.reviewer.label()}" if p.reviewer else "")
            + (" · human label" if p.human else "")
        )
    if not assume_yes and ask("Change models or effort per tier? (y/N)", "n").strip().lower().startswith("y"):
        for tier in TIERS:
            p = tiers[tier]
            say(f"{tier}")
            tiers[tier] = replace(
                p,
                builder=_ask_seat(ask, "builder", p.builder),
                gate=_ask_seat(ask, "gate", p.gate),
                reviewer=_ask_seat(ask, "reviewer", p.reviewer) if p.reviewer else None,
            )
    guess = _test_command(root)
    test_command = guess if assume_yes else ask("Test command builders run (optional)", guess)

    cfg = Config(
        repo=repo,
        base=base,
        tiers=tiers,
        test_command=test_command.strip() or None,
        engine_args={e: dict(r) for e, r in config_mod.DEFAULT_ENGINE_ARGS.items()},
    )
    text = config_mod.render(cfg)
    config_mod.parse(tomllib.loads(text))  # the file we write must load
    target.write_text(text, encoding="utf-8")
    say(f"Wrote {target}")

    gitignore = root / ".gitignore"
    current = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if ".vigia/" not in current.splitlines():
        gitignore.write_text(
            current + ("\n" if current and not current.endswith("\n") else "") + ".vigia/\n", encoding="utf-8"
        )
        say("Added .vigia/ (ledger, logs, lock) to .gitignore")
    template = root / ".github" / "ISSUE_TEMPLATE" / "vigia-ticket.md"
    if not template.exists():
        template.parent.mkdir(parents=True, exist_ok=True)
        template.write_text(tickets.TEMPLATE, encoding="utf-8")
        say(f"Wrote the ticket template to {template.relative_to(root)}")

    if create_labels:
        gh = GitHub(repo, runner=runner)
        for name, color, description in label_specs(cfg.labels):
            gh.ensure_label(name, color, description)
        say(f"Created {len(label_specs(cfg.labels))} labels on {repo}")
    say("")
    say("Next: commit vigia.toml, .gitignore and the ticket template; open an issue from the template")
    say("with a tier label and `vigia:ready`; then run `vigia run` (one build) or `vigia watch` (the loop).")
    return target
