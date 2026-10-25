"""The merge decision: pure Python, no model, no I/O.

`decide()` takes what vigia observed about one pull request and returns the single next step. Every
input is explicit, so the whole decision table is unit-tested (tests/test_rules.py). The watcher
executes the step; nothing here can be talked into a merge.

Order of the checks, and why:
1. Closed, parked or draft PRs are left alone.
2. Structural problems park immediately (no tier, unknown builder, a same-lab gate, conflicts, a
   destination other than the configured base: the reviewed diff is only right for that base).
3. CI runs before any review: a red CI repaired after the gate would move the head and waste a round.
4. The cross-lab gate reviews the exact head. A P1 finding asks for one fix round; the gate then
   reviews the new head. At most `caps.gate_rounds` heads are ever gated.
5. T1/T2: a stronger reviewer reviews the same exact head.
6. T2: a human adds the human label on that exact head.
7. Merge, with `--match-head-commit`, so only the head that was judged can land, and only where GitHub
   refuses a merge onto a base that moved after vigia's last check.
Wherever 4-7 would review or merge a head whose base moved, the step is `update` instead: the base is
brought in first and the new head is reviewed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .config import Config

Kind = Literal["gate", "review"]
Action = Literal["merge", "gate", "review", "fix", "park", "wait", "update"]


@dataclass(frozen=True)
class Verdict:
    """One review result on one exact head. UNAVAILABLE = the engine ran but returned no usable verdict."""

    kind: Kind
    sha: str
    verdict: Literal["APPROVE", "CHANGES", "UNAVAILABLE"]
    p1: int = 0
    p2: int = 0

    @property
    def content(self) -> bool:
        return self.verdict != "UNAVAILABLE"

    @property
    def passes(self) -> bool:
        # A verdict that says APPROVE while listing a P1 is read as CHANGES: the finding wins.
        return self.verdict == "APPROVE" and self.p1 == 0


@dataclass(frozen=True)
class PRState:
    number: int
    head: str
    tier: str | None
    builder_family: str | None
    ci: Literal["passing", "pending", "failing", "none"]
    mergeable: Literal["MERGEABLE", "CONFLICTING", "UNKNOWN"]
    open: bool = True
    draft: bool = False
    parked: bool = False
    human_ok: bool = False
    on_base: bool = True  # the PR targets the configured base branch
    behind: bool = False  # the base moved since this head branched off it (read only when it matters)
    # GitHub itself refuses to merge this PR onto a base that moved after vigia's last check (read before a merge)
    base_guarded: bool = True
    # Everything below counts only since the PR was last unparked: unparking is a human's fresh start.
    verdicts: tuple[Verdict, ...] = field(default_factory=tuple)
    fix_rounds: int = 0
    failures: int = 0  # failed builder/fixer runs on the current head
    updated_heads: frozenset[str] = frozenset()  # heads vigia made only to bring the base in: no round spent


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str
    sha: str
    stage: str = ""  # for fix: "ci" | "gate" | "review"; for gate/review: the round number as text

    def __str__(self) -> str:
        return f"{self.action}: {self.reason}"


def _gated_heads(verdicts: tuple[Verdict, ...], kind: Kind) -> list[str]:
    heads: list[str] = []
    for v in verdicts:
        if v.kind == kind and v.content and v.sha not in heads:
            heads.append(v.sha)
    return heads


def decide(pr: PRState, cfg: Config) -> Decision:
    head, caps = pr.head, cfg.caps

    def wait(reason: str) -> Decision:
        return Decision("wait", reason, head)

    def park(reason: str) -> Decision:
        return Decision("park", reason, head)

    if not pr.open:
        return wait("closed")
    if pr.parked:
        return wait("parked — remove the parked label to resume")
    if pr.draft:
        return wait("draft")
    if not pr.on_base:
        return park(
            f"the PR does not target the base branch `{cfg.base}`: reviews cover the diff against it only"
        )
    if pr.tier not in cfg.tiers:
        return park("no tier label: add exactly one of " + ", ".join(cfg.labels.tier(t) for t in cfg.tiers))
    policy = cfg.policy(pr.tier)
    if pr.builder_family is None:
        return park("unknown builder: no vigia handoff with an `Engine:` line")
    if policy.gate.family == pr.builder_family:
        return park(f"the {pr.tier} gate ({policy.gate.engine}) is from the builder's own lab")
    if pr.mergeable == "CONFLICTING":
        return park("merge conflicts with the base branch")
    if pr.failures >= caps.attempts:
        return park(f"{pr.failures} failed engine runs on this head")

    gated = _gated_heads(pr.verdicts, "gate")
    # A head that only brought the moved base in is reviewed again, but it does not use up a round.
    gate_rounds_left = len([h for h in gated if h not in pr.updated_heads]) < caps.gate_rounds

    # The base moved since this head: bring it in before any review, a human's approval or a merge, so what is
    # reviewed is what the merge produces. The new head is reviewed again without using up a round.
    update = Decision("update", "the base moved since this head: bring it in and review again", head)

    def update_first(d: Decision) -> Decision:
        return update if pr.behind else d

    def fix_or_park(stage: str, why: str) -> Decision:
        if pr.fix_rounds >= caps.fix_rounds:
            return park(f"{why}; fix rounds used up ({pr.fix_rounds}/{caps.fix_rounds})")
        if not gate_rounds_left:
            return park(f"{why}; a fix would need another gate round and all {caps.gate_rounds} are used")
        return Decision("fix", why, head, stage)

    # 3 — CI before review.
    if pr.ci == "pending":
        return wait("CI running on head")
    if pr.ci == "none" and cfg.require_ci:
        return wait("no CI result on head yet")
    if pr.ci == "failing":
        return fix_or_park("ci", "CI is red on head")

    # 4 — cross-lab gate on the exact head.
    on_head = [v for v in pr.verdicts if v.kind == "gate" and v.sha == head]
    with_content = [v for v in on_head if v.content]
    if not with_content:
        unavailable = len(on_head)
        if unavailable >= caps.attempts:
            return park(
                f"GATE_UNAVAILABLE: no verdict from the other lab on head after {unavailable} attempts"
            )
        if not gate_rounds_left and head not in pr.updated_heads:
            return park(f"gate rounds used up ({caps.gate_rounds}) and the head moved after the last one")
        return update_first(
            Decision("gate", f"gate round {len(gated) + 1} on head", head, str(len(gated) + 1))
        )
    gate = with_content[-1]
    if not gate.passes:
        return fix_or_park("gate", f"gate round {gated.index(head) + 1}: {gate.p1} P1 finding(s) on head")

    # 5 — stronger reviewer (T1/T2) on the same exact head.
    if policy.reviewer is not None:
        reviewed = _gated_heads(pr.verdicts, "review")
        on_head_r = [v for v in pr.verdicts if v.kind == "review" and v.sha == head]
        with_content_r = [v for v in on_head_r if v.content]
        if not with_content_r:
            if len(on_head_r) >= caps.attempts:
                return park(f"reviewer returned no verdict on head after {len(on_head_r)} attempts")
            if (
                len([h for h in reviewed if h not in pr.updated_heads]) >= caps.review_rounds
                and head not in pr.updated_heads
            ):
                return park(
                    f"review rounds used up ({caps.review_rounds}) and the head moved after the last one"
                )
            return update_first(Decision("review", f"{pr.tier} review on head", head, str(len(reviewed) + 1)))
        review = with_content_r[-1]
        if not review.passes:
            return fix_or_park("review", f"{pr.tier} reviewer: {review.p1} P1 finding(s) on head")

    if pr.behind:
        return update

    # 6 — T2: a human on the exact head.
    if policy.human and not pr.human_ok:
        return wait(f"{pr.tier}: waiting for a human to add {cfg.labels.human_ok} on {head[:7]}")

    # 7 — merge.
    if pr.mergeable != "MERGEABLE":
        return wait("GitHub has not computed mergeability yet")
    # vigia's own check of the base is a moment old by the merge; only GitHub can hold it at the merge itself.
    if not pr.base_guarded:
        return wait(
            f"GitHub would merge onto a `{cfg.base}` that moved after vigia's last check: require branches to be "
            "up to date before merging (branch protection including administrators, or a ruleset vigia's login "
            "cannot bypass)"
        )
    return Decision("merge", f"{pr.tier} requirements hold on head", head)
