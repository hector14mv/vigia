"""Fuel: how much of each lab's subscription quota is left, read from files the CLIs already write.

Codex CLI records its rate-limit windows in every session rollout under `~/.codex/sessions/`. Claude Code
keeps no such file, so its quota is reported as unknown: read it with `/usage` in Claude Code.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

WEEK_MINUTES = 7 * 24 * 60


def codex_sessions() -> Path:
    return Path.home() / ".codex" / "sessions"


def _latest_rollouts(folder: Path, limit: int = 20) -> list[Path]:
    if not folder.is_dir():
        return []
    files = sorted(folder.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:limit]


def codex_weekly(folder: Path | None = None) -> dict | None:
    """{'used_percent', 'resets_at' (ISO, UTC), 'observed' (ISO)} of Codex's weekly window, from the newest
    rollout that reports it; None when no rollout does."""
    for path in _latest_rollouts(folder or codex_sessions()):
        found = None
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"rate_limits"' not in line:
                        continue
                    try:
                        found = _weekly(json.loads(line)) or found
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
        if found:
            observed = datetime.fromtimestamp(path.stat().st_mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            return {**found, "observed": observed}
    return None


def _weekly(row: object) -> dict | None:
    """The weekly window inside one rollout line, wherever the CLI nests `rate_limits`."""
    stack = [row]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            limits = item.get("rate_limits")
            if isinstance(limits, dict):
                for key in ("primary", "secondary"):
                    window = limits.get(key)
                    if isinstance(window, dict) and window.get("window_minutes") == WEEK_MINUTES:
                        resets = window.get("resets_at")
                        return {
                            "used_percent": float(window.get("used_percent") or 0),
                            "resets_at": datetime.fromtimestamp(resets, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                            if isinstance(resets, int | float)
                            else None,
                        }
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return None
