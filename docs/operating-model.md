# The operating model in seven rules

vigia is the small, runnable core of an AI software factory that merged 360 pull requests in two
weeks (7–21 October 2026): written by Claude or Codex, reviewed by a model from the other lab, and
merged by a Python program that never calls a model. That factory's written process
once had 79 rules; an audit cut them to thirteen. These are the seven that carry the weight, each
with the place in this repository where it is enforced.

The full story, with the measurements behind each rule, is in the article
[Dos laboratorios y un vigía](https://hectormoyanovelez.com/fabrica) (in Spanish).

---

## 1. Damage sets the review, not the author

Every ticket carries one **tier**, and the tier decides how much review the change gets:

| Tier | What it touches | Review stack before the merge |
|---|---|---|
| **T0** | cosmetic changes, docs, tests, internal tooling | CI green → one cross-lab gate |
| **T1** | product logic with no money and no other people's data | CI green → cross-lab gate → a stronger reviewer |
| **T2** | money, auth, personal data, migrations, messages to customers, credentials, the process itself | CI green → cross-lab gate → a stronger reviewer → a human label on the exact head |

When a change fits two tiers, the higher one wins. A ticket that turns out to touch a T2 surface is
re-tiered upward as soon as that is known, never downward mid-flight. The model that wrote the
change does not lower the bar, and a strong model does not buy a lighter review.

*Where:* `[tiers.T0|T1|T2]` in `vigia.toml`; `rules.decide()` steps 4–6. T2 cannot drop the human
(`config.parse` refuses it).

## 2. The ticket says what, not how

The ticket is the builder's whole prompt, so it is written in a fixed shape: **Outcome** (one
sentence, from the user's side), **Scope** with a `Not touched:` line and a `Must still hold:` line
(the existing behaviour this change could break, named concretely), **Acceptance criteria**, a
**Done-test**, and **Risks**. It fixes the outcome and the invariants and leaves the *how* to the
builder, which reads the code. In the factory's own retro, 31 of 43 serious findings came from the
ticket, not the builder: 17 from a ticket that said something false about the code, 14 from one
that left out something that mattered.

*Where:* `tickets.missing()` refuses to build a ticket without every section and both lines, and
says why on the issue. The template is `.github/ISSUE_TEMPLATE/vigia-ticket.md`, written by
`vigia init`.

## 3. A different lab reviews every change

Models from one lab tend to share blind spots, so the reviewer of every change — the **gate** —
comes from the other lab: Claude's work is gated by Codex, Codex's work by Claude. The gate reads
the exact head commit, in a read-only checkout, and must return content: a verdict with findings,
or a clean pass that names the commit it reviewed. A same-lab read never substitutes for it, and
no verdict is **`GATE_UNAVAILABLE`**, which holds the merge. Absence never expires into a pass.

*Where:* `config.parse` refuses a tier whose gate is the builder's lab; `gate.review()` refuses at
run time too; `engines.parse_verdict()` returns nothing for anything that is not a verdict, and
`rules.decide()` treats that as no review at all. Reviewers run with `--sandbox read-only` (Codex)
or without edit tools (Claude), whatever the config says.

## 4. One gate per round, on a frozen head, two rounds at most

A review covers exactly one commit. While it runs nobody pushes; if the head moves, the old verdict
no longer covers the PR. The gate runs once per round, and a second round exists only after a fix
for an accepted finding has been pushed. After the second round there is no third: the PR is parked
and a human decides. Repeated review rounds were the factory's single largest waste of quota.

*Where:* every verdict in the ledger is keyed by the full SHA; `rules.decide()` counts gated heads
against `caps.gate_rounds` (default 2) and parks when they are used.

## 5. A finding blocks only if it fails on this commit

A P1 is a defect on the path the change touches — wrong behaviour, data loss, a security hole, an
acceptance criterion not met, a `Must still hold:` invariant broken — with a concrete scenario that
fails on this exact commit, at a file and line. A P1 blocks the merge and is fixed before it, never
deferred. A P2 is real but does not block. Style, naming and preferences are not findings, and a
finding without a failing scenario on this commit is dismissed. This keeps review noise from eating
rounds while making sure the defects that matter are never traded away.

*Where:* the reviewer prompt's severity rules (`prompts.SEVERITY`); `rules.Verdict.passes` (an
APPROVE that lists a P1 is read as CHANGES).

## 6. Sessions end at the handoff; every fix is a fresh session

The builder works in its own worktree, writes a **handoff** (what changed, the tests it ran,
deviations from the ticket, risks, follow-ups) and stops. It does not wait for review. When a round
returns accepted findings, a **new** session gets a closed brief — the PR, the exact head, the
findings with file and line, the checks to run — repairs, hands off and stops. Long-lived sessions
that wait for their own reviews re-read an ever-growing context; in the factory that was most of
the token bill.

*Where:* `builder.build()` and `builder.fix()`. The engine only edits files; vigia commits, pushes,
opens the PR and posts the handoff (`## Handoff <full sha>` with the `Engine:` line), so every step
that touches GitHub is deterministic and identical for every engine.

## 7. A script merges, never a model

The merge is a decision table, not a judgement: CI green on the head, the gate approved that head,
the tier's extra requirements hold on that head, no conflicts. If all hold, `gh pr merge --squash
--match-head-commit <sha>` — so only the exact commit that was judged can land, and anything pushed
after the approval makes the merge fail. If not, the PR waits or is **parked** with its reason
written on it. Caps are hard (gate rounds, fix rounds, review passes, failed sessions, session
length); hitting one parks, it never loops. Every action is one line in an append-only ledger, and
nobody — no model, no session — approves its own work.

*Where:* `rules.decide()` is pure Python with no I/O and is tested row by row
(`tests/test_rules.py`); `watch.merge()` re-reads the PR and re-decides right before merging; the
ledger is `.vigia/ledger.jsonl`, opened in append mode only (`ledger.Ledger`).

---

## What a human still does

- **Decides what to build**: writes, or approves, the ticket and its tier.
- **Owns T2**: adds `vigia:human-ok` on the exact head of every T2 change. A label added before the
  head moved never counts (`watch.human_approved` compares GitHub's own event order, not clocks).
- **Unparks**: reads the reason, deals with it, removes `vigia:parked`. The round and fix counters
  start again from zero.
- **Owns the credentials** the engines run with (see [SECURITY.md](../SECURITY.md)).
