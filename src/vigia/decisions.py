"""Decision issues: a question only a person can answer, with its options and vigia's recommendation.

A decision issue carries the `vigia:decision` label and a body in a fixed shape:

    Question: <one sentence?>
    Blocks: #38, #39
    Due: 2026-10-26
    Origin: <where the question came from>

    ### A. <option title>
    <what happens if you pick it>
    ### B. ...

    Recommendation: B — <one line why>

This module only reads that shape, for the Mesa. Answering stays a GitHub act.
"""

from __future__ import annotations

import re

LABEL = "vigia:decision"
_OPTION = re.compile(r"^###\s+([A-Z])\.\s+(.+?)\s*$")
_REFS = re.compile(r"#(\d+)")


def field(body: str, name: str) -> str | None:
    """The value of a `Name: value` line (first one, case-insensitive), or None."""
    match = re.search(rf"^\s*{re.escape(name)}\s*:\s*(.+?)\s*$", body or "", re.I | re.M)
    return match.group(1) if match else None


def refs(value: str | None) -> list[int]:
    """Issue or PR numbers in a value such as `#38, #39`."""
    return [int(n) for n in _REFS.findall(value or "")]


def blocked_by(body: str) -> list[int]:
    """The numbers in a ticket's `Blocked by:` line."""
    return refs(field(body, "Blocked by"))


def parse(body: str) -> dict | None:
    """The decision's parts, or None when the body has no question or fewer than two options."""
    question = field(body, "Question")
    options: list[dict] = []
    for line in (body or "").splitlines():
        if re.match(r"^\s*Recommendation\s*:", line, re.I):
            break  # the options end where the recommendation starts
        match = _OPTION.match(line)
        if match:
            options.append({"key": match.group(1), "title": match.group(2), "body": []})
        elif options:
            options[-1]["body"].append(line)
    for option in options:
        option["body"] = "\n".join(option["body"]).strip()
    if not question or len(options) < 2:
        return None
    rec, why = None, None
    rec_line = field(body, "Recommendation")
    if rec_line:
        match = re.match(r"([A-Z])\b\s*[—–:-]?\s*(.*)", rec_line)
        if match and match.group(1) in {o["key"] for o in options}:
            rec, why = match.group(1), match.group(2) or None
    return {
        "question": question,
        "options": options,
        "rec": rec,
        "rec_why": why,
        "blocks": refs(field(body, "Blocks")),
        "due": field(body, "Due"),
        "origin": field(body, "Origin"),
    }
