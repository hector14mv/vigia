"""The comments vigia leaves on GitHub: the public record of every step, written for humans to read."""

from __future__ import annotations

import hashlib
import re

from .config import FAMILIES, Seat

HANDOFF_HEAD = re.compile(r"^## Handoff ([0-9a-f]{40})\b")
ENGINE_LINE = re.compile(r"^Engine:\s*(claude|codex)\b", re.M)
FOLLOW_UPS_HEAD = re.compile(r"^##\s+Follow-ups\s*$", re.I)
BULLET = re.compile(r"^[-*+]\s+(.*\S)")
TITLE_MAX = 70
MARKER = re.compile(r"^<!-- vigia:follow-up pr=(\d+) hash=([0-9a-f]{12}) -->$", re.M)


def handoff(
    sha: str,
    seat: Seat,
    role: str,
    issue: int | None,
    tier: str,
    seconds: float,
    code: int,
    tests: str,
    files: list[str],
    body: str,
    note: str = "",
) -> str:
    shown = ", ".join(f"`{f}`" for f in files[:20]) + (
        f" and {len(files) - 20} more" if len(files) > 20 else ""
    )
    lines = [
        f"## Handoff {sha}",
        f"Engine: {seat.engine} · {seat.model}"
        + (f" · effort {seat.effort}" if seat.effort else "")
        + f" · {role} · lab {seat.family}",
        f"Ticket: #{issue} · tier {tier}" if issue else f"Tier: {tier}",
        f"Session: {seconds:.0f} s · exit {code}",
        f"Tests run by vigia: {tests}",
        f"Files changed: {shown or 'none'}",
    ]
    if note:
        lines.append(f"Note: {note}")
    return "\n".join(lines) + "\n\n" + (body.strip() or "_The builder left no handoff notes._") + "\n"


def handoff_for(comments: list[dict], sha: str, login: str | None) -> str:
    """The latest handoff comment for exactly `sha`, posted by vigia's own login."""
    found = ""
    for c in comments:
        m = HANDOFF_HEAD.match(c["body"].lstrip())
        if m and m.group(1) == sha and (login is None or c["author"] == login):
            found = c["body"]
    return found


def handoffs(comments: list[dict], login: str | None) -> list[str]:
    """The body of every handoff comment posted by vigia's own login, oldest first."""
    return [
        c["body"]
        for c in comments
        if HANDOFF_HEAD.match(c["body"].lstrip()) and (login is None or c["author"] == login)
    ]


def follow_ups(body: str) -> list[str]:
    """The bullets of a handoff's `## Follow-ups` section, one string each (wrapped lines joined).

    "none" bullets are dropped here, so what comes back is work somebody should look at.
    """
    items: list[list[str]] = []
    inside = False
    for line in body.splitlines():
        if line.startswith("#"):
            inside = bool(FOLLOW_UPS_HEAD.match(line.rstrip()))
            continue
        if not inside or not line.strip():
            continue
        bullet = BULLET.match(line)
        if bullet:
            items.append([bullet.group(1)])
        elif items:
            items[-1].append(line.strip())
    texts = (" ".join(" ".join(parts).split()) for parts in items)
    return [t for t in texts if t.strip(" .*_`").lower() != "none"]


def follow_up_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def follow_up_title(text: str) -> str:
    """The bullet cut to 70 characters at a word boundary."""
    if len(text) <= TITLE_MAX:
        return text
    cut = text[:TITLE_MAX]
    if not text[TITLE_MAX].isspace() and " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-")


def follow_up_marker(pr: int, digest: str) -> str:
    return f"<!-- vigia:follow-up pr={pr} hash={digest} -->"


def marked_follow_ups(body: str, pr: int) -> set[str]:
    """Hashes of the follow-up markers an issue body carries for PR `pr` (a quoted bullet cannot fake one)."""
    return {digest for number, digest in MARKER.findall(body) if int(number) == pr}


def follow_up_issue(text: str, pr: int, pr_url: str, sha: str) -> tuple[str, str]:
    """Title and body of the issue for one follow-up bullet."""
    quoted = "\n".join(f"> {line}" for line in text.splitlines())
    link = f"{pr_url} (#{pr})" if pr_url else f"#{pr}"
    body = (
        f"{quoted}\n\n"
        f"A builder or fixer noted this outside its ticket in PR {link}, merged at `{sha}`.\n\n"
        "This is a note, not a ticket: rewrite it into the vigia ticket template before it gets `vigia:ready`.\n\n"
        f"{follow_up_marker(pr, follow_up_hash(text))}\n"
    )
    return follow_up_title(text), body


def engine_family(comments: list[dict], login: str | None) -> str | None:
    """The builder's lab, from the first vigia handoff's `Engine:` line."""
    for c in comments:
        if HANDOFF_HEAD.match(c["body"].lstrip()) and (login is None or c["author"] == login):
            m = ENGINE_LINE.search(c["body"])
            if m:
                return FAMILIES[m.group(1)]
    return None


def _finding(f: dict) -> str:
    where = f"`{f['file']}:{f['line']}`" if f.get("line") else f"`{f['file']}`"
    text = f"- **{f['severity']}** {where} — {f['title']}"
    if f.get("scenario"):
        text += f"\n  Scenario: {f['scenario']}"
    return text


def verdict(
    kind: str,
    round_no: int,
    sha: str,
    seat: Seat,
    builder_family: str | None,
    parsed: dict | None,
    seconds: float,
    failure: str = "",
) -> str:
    title = "Gate" if kind == "gate" else "Review"
    name = parsed["verdict"] if parsed else "GATE_UNAVAILABLE"
    out = [
        f"<!-- vigia:{kind} sha={sha} verdict={name} -->",
        f"## vigia {title.lower()} · round {round_no} · {name}",
        f"Reviewed commit: {sha}",
        f"Reviewer: {seat.engine} · {seat.model}"
        + (f" · effort {seat.effort}" if seat.effort else "")
        + f" · lab {seat.family}"
        + (f" (built by lab {builder_family})" if builder_family else ""),
        f"Session: {seconds:.0f} s",
        "",
    ]
    if parsed is None:
        out.append(
            "The reviewer returned no usable verdict, so this head has **no review**. "
            "That holds the merge; it is never read as a pass."
            + (f"\n\nDetail: {failure}" if failure else "")
        )
        return "\n".join(out) + "\n"
    if parsed["summary"]:
        out += [parsed["summary"], ""]
    if parsed["findings"]:
        out += [f"**Findings:** {parsed['p1']} P1 · {parsed['p2']} P2", ""]
        out += [_finding(f) for f in parsed["findings"]]
    else:
        out.append("No findings.")
    return "\n".join(out) + "\n"


def parked(sha: str, reason: str, label: str) -> str:
    return (
        f"PARKED {sha} — {reason}\n\n"
        f"vigia stopped working on this pull request. To resume, deal with the cause and remove the "
        f"`{label}` label: the round and fix counters start again from zero.\n"
    )


def merged(sha: str, tier: str, why: list[str]) -> str:
    return f"Merged by vigia at `{sha}` · tier {tier}\n\n" + "\n".join(f"- {w}" for w in why) + "\n"
