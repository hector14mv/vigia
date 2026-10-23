"""Tickets are GitHub issues written in a fixed template. The body is the builder's whole prompt.

A ticket says *what* changes and what must keep working; *how* is the builder's job, because the
builder reads the code and the ticket writer only remembers it. vigia refuses to build a ticket
that is missing a section or the two lines that most often prevent a bad PR (`Not touched:` and
`Must still hold:`).
"""

from __future__ import annotations

import re

SECTIONS = ("Outcome", "Scope", "Acceptance criteria", "Done-test", "Risks")
REQUIRED_LINES = {"Scope": ("Not touched:", "Must still hold:")}

TEMPLATE = """\
---
name: vigia ticket
about: Work for an AI builder. The body is its whole prompt, so say what, not how.
labels: ["vigia:ready"]
---

## Outcome
<What changes for the user, in one sentence.>

## Scope
<Files, modules or commands this ticket touches — what changes, not how.>
Not touched: <what this ticket must NOT change>
Must still hold: <existing behaviour this change could break and must not, each one concrete; "none" only for docs>

## Acceptance criteria
- [ ] <verifiable criterion>
- [ ] <verifiable criterion>

## Done-test
<The command or steps a human runs to see it works.>

## Risks
<What could go wrong; data, money or security surfaces touched (if any, the tier is T2).>
"""

_HEADER = re.compile(r"^##\s+(.+?)\s*$", re.M)


def sections(body: str) -> dict[str, str]:
    """Section title -> its text, for `## Title` headers. A header inside a code fence does not start a
    section, but the fenced text still belongs to the section it sits in."""
    body = body or ""
    fences = [m.span() for m in re.finditer(r"```.*?```", body, flags=re.S)]
    matches = [m for m in _HEADER.finditer(body) if not any(a <= m.start() < b for a, b in fences)]
    out: dict[str, str] = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        title = m.group(1).split("(")[0].strip()
        out.setdefault(title, body[m.end() : end].strip())
    return out


_PLACEHOLDER = re.compile(r"<[^>\n]*>")


def _filled(text: str) -> bool:
    """True when the text says something once template placeholders and checkbox markers are removed."""
    stripped = re.sub(r"(?m)^\s*```.*$", "", text)  # a fence marker (or its language tag) is not content
    stripped = _PLACEHOLDER.sub("", stripped)
    stripped = re.sub(r"(?m)^\s*-\s*\[[ xX]?\]\s*", "", stripped)
    return bool(re.search(r"\w", stripped))


def missing(body: str) -> list[str]:
    """What keeps this ticket from being launchable; empty when it can be built."""
    found = {k.lower(): v for k, v in sections(body).items()}
    gaps: list[str] = []
    for name in SECTIONS:
        text = found.get(name.lower())
        if text is None:
            gaps.append(f"section `## {name}`")
        elif not _filled(text):
            gaps.append(f"section `## {name}` is empty")
    for name, lines in REQUIRED_LINES.items():
        text = found.get(name.lower()) or ""
        for line in lines:
            m = re.search(rf"(?m)^\s*{re.escape(line)}(.*)$", text)
            if not m or not _filled(m.group(1)):
                gaps.append(f"`{line}` line in `## {name}`")
    return gaps


def tier(labels: list[str] | set[str] | frozenset[str], prefix: str = "tier:") -> str | None:
    """The single tier label (T0/T1/T2), or None when there is none or more than one."""
    tiers = {name[len(prefix) :] for name in labels if name.startswith(prefix)}
    tiers &= {"T0", "T1", "T2"}
    return tiers.pop() if len(tiers) == 1 else None


def slug(title: str, limit: int = 40) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return (text[:limit].rstrip("-")) or "change"


_CLOSES = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b", re.I)


def linked_issue(pr_body: str) -> int | None:
    m = _CLOSES.search(pr_body or "")
    return int(m.group(1)) if m else None
