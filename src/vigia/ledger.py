"""The ledger: one JSON line per thing vigia did, appended and never rewritten.

It is vigia's memory between ticks (which head the gate reviewed, how many fix rounds a PR used,
who built it) and the audit trail behind every merge. Comments on GitHub are the public copy; the
decisions read the ledger, which only vigia writes.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Ledger:
    path: Path
    clock: Callable[[], str] = utcnow

    def append(self, event: str, **fields: object) -> dict:
        """Write one event. The file is opened in append mode and fsynced: earlier lines are never touched."""
        row = {"ts": self.clock(), "event": event, **fields}
        line = json.dumps(row, ensure_ascii=False, sort_keys=False, default=str)
        # json.dumps escapes newlines inside strings; this guards the one-line-per-event rule anyway.
        if "\n" in line:
            raise ValueError("ledger rows must serialise to a single line")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return row

    def rows(self) -> list[dict]:
        """Every readable row, oldest first. A torn or hand-edited line is skipped, never fatal."""
        return list(self._iter())

    def _iter(self) -> Iterator[dict]:
        try:
            fh = self.path.open(encoding="utf-8")
        except FileNotFoundError:
            return
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and "event" in row:
                    yield row

    def events(self, event: str | None = None, **match: object) -> list[dict]:
        return [
            r
            for r in self._iter()
            if (event is None or r.get("event") == event) and all(r.get(k) == v for k, v in match.items())
        ]

    def last(self, event: str | None = None, **match: object) -> dict | None:
        found = self.events(event, **match)
        return found[-1] if found else None
