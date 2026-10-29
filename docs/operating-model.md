# The operating model in seven rules

vigia is a rewrite, to run anywhere, of a private AI software factory that merged 360 pull requests in two
weeks (7–21 October 2026), written by Claude or Codex, reviewed by a model from the other lab, and merged by
`rules.decide()`, a pure function (no model, no I/O) over CI status, verdicts and labels on the exact head. That factory's written process
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
| **T1** | product logic with no money and no other people's data | CI green → cross-lab gate → a second reviewer (the default; `vigia.toml` may leave it out) |
| **T2** | money, auth, personal data, migrations, messages to customers, credentials, the process itself | CI green → cross-lab gate → a second reviewer → a human label on the exact head |

When a change fits two tiers, the higher one wins. A ticket that turns out to touch a T2 surface is
re-tiered upward as soon as that is known, never downward mid-flight. The model that wrote the
change does not lower the bar, and a strong model does not buy a lighter review.

*Where:* `[tiers.T0|T1|T2]` in `vigia.toml`; `rules.decide()` steps 4–6. The T1 second reviewer is the
default and `vigia.toml` may leave it out: a T1 PR then merges on green CI plus the gate on the exact head. T2
can drop neither its second reviewer nor the human (`config.parse` refuses both).

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

*Design assumption:* models from one lab tend to share blind spots. One measurement points that way: of 16
defects Codex's GitHub code-review bot had found and that were later fixed, Claude Sonnet 5.5 found 1 and Claude Opus 5.5
found 5 ([the article](https://hectormoyanovelez.com/fabrica), 9 October). So the reviewer of every change — the **gate** — comes from the
other lab: Claude's work is gated by Codex, Codex's work by Claude. The gate reads
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
and a human decides. On one measured night (29–30 September) the factory spent 1,373 million tokens on 8
merges, and 15% of them went to writing code; review rounds, the watch and waiting sessions took the rest.

A review covers the PR's head, not the base it lands on: when the base moves after the review, the squash
merge combines reviewed code with base changes nobody reviewed together. In v0.1 the guard is GitHub's own:
turn on "Require branches to be up to date before merging" with at least one required check, and GitHub
refuses to merge a stale head; the PR then waits until someone presses "Update branch", and vigia reviews the
new head like any other. Bringing a moved base in and reviewing again by itself is on the roadmap.

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
that wait for their own reviews re-read an ever-growing context; in the factory such sessions were found
re-reading contexts that had grown to 868,000 tokens.

*Where:* `builder.build()` and `builder.fix()`. The engine only edits files; vigia commits, pushes,
opens the PR and posts the handoff (`## Handoff <full sha>` with the `Engine:` line), so every step
that touches GitHub is deterministic and identical for every engine.

## 7. A pure decision table merges

The merge is a decision table, not a judgement: CI green on the head, the gate approved that head,
the tier's extra requirements hold on that head, no conflicts. If all hold, `gh pr merge --squash
--match-head-commit <sha>` — so only the exact commit that was judged can land, and anything pushed
after the approval makes the merge fail. If not, the PR waits or is **parked** with its reason
written on it. Caps are hard (gate rounds, fix rounds, review passes, failed sessions, session
length); hitting one parks, it never loops. Every action is one line in an append-only ledger, and
nobody — no model, no session — approves its own work.

*Where:* `rules.decide()` is pure Python with no I/O and is tested row by row
(`tests/test_rules.py`); `watch.merge()` re-reads the PR and re-decides right before merging; the
ledger is `.vigia/ledger.jsonl`, opened in append mode only (`ledger.Ledger`). When several engine
sessions run at once (`watch.slots`), each holds a lock on its PR or issue for its whole life, taken by
the watcher before the session starts, so no two sessions ever work on the same PR (`jobs.spawn`).

---

## How the parts fit

1. **Plan.** `vigia plan "<goal>"` turns a goal into tickets in the template, every one filed as
   `vigia:proposed`: a person ratifies a ticket by swapping that label for the ready one, so what the factory
   builds next stays a person's call. Work only a person can do becomes a `vigia:human-task` issue.
2. **Build.** `vigia watch` takes the oldest ready ticket into a free slot: a builder session in its own
   worktree, then vigia commits, pushes, opens the PR and posts the handoff.
3. **Check.** CI on the head; then the cross-lab gate; a P1 starts a fresh fix round; on T1 (unless
   `vigia.toml` leaves it out) and T2 the stronger reviewer; on T2, a person's label on the exact head.
4. **Merge or park.** The decision table merges with `--match-head-commit`, or parks with the reason written
   on the PR. Nothing loops past a cap.
5. **See it.** `vigia mesa` shows what needs a person, every PR's path and the last events, on a laptop or a
   phone; `vigia report` turns the ledger into the engine table; `vigia status` prints the same in a terminal.
   One `vigia.workspace.toml` drives several repositories with shared slots, on a laptop or an always-on
   server ([server.md](server.md)).

## What a human still does

- **Ratifies planned tickets** that wait as `vigia:proposed`, and does the `vigia:human-task` issues.
- **Decides what to build**: writes, or approves, the ticket and its tier, and adds `vigia:ready`. vigia builds
  only a label added by someone with write access, on the ticket as it was then; an edit after it needs the label
  again, and removing it withdraws the approval (`builder.authorize`).
- **Owns T2**: adds `vigia:human-ok` on the exact head of every T2 change. A label added before the
  head moved never counts (`watch.human_approved` compares GitHub's own event order, not clocks).
- **Unparks**: reads the reason, deals with it, removes `vigia:parked`. The round and fix counters
  start again from zero.
- **Owns the credentials** the engines run with (see [SECURITY.md](../SECURITY.md)).
