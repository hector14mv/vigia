---
name: vigia-ticket
description: Use when writing or completing a ticket for vigia, e.g. "write a ticket", "turn this into an issue", "turn this into a vigia ticket".
---

# Write a vigia ticket

vigia builds GitHub issues with an AI builder. **The issue body is the builder's whole prompt.**
Say *what* changes and what must keep working. Leave *how* to the builder, which reads the code;
you only remember it. If you must suggest a how, put it on one `Suggested approach:` line in
`## Scope`, and have it rest on a verified fact or mark it `(unverified)`. Tickets that told the
builder how without checking the code caused most serious findings.

## 1. Check the code first

Run commands against the current base branch (`git fetch` first) before you write any claim:
`grep`, `git log`, run the tests, open the file. Do not write from memory.

## 2. Write the body, in this order

```markdown
## Outcome
One sentence, from the user's side.

## Scope
What the ticket touches (files, modules, commands), not how.
Not touched: what must NOT change.
Must still hold: existing behaviour this could break, each one concrete.

## Acceptance criteria
- [ ] a criterion someone can verify by running or reading something

## Done-test
The command or steps a human runs to see it work.

## Risks
What could go wrong; any money, auth, personal data or migration surface.

## Facts
- verified (`<command>`): <what it showed>
- hypothesis — disprove if wrong: <claim>
```

- `Must still hold:` names concrete invariants (a function's contract, a test, an output
  format). Write "none" only for docs-only changes.
- Every acceptance criterion is verifiable. "Works well" is not; "`f('Él')` returns `'el'`" is.
- `## Facts`: list every factual claim in the ticket (a `file:line`, current behaviour, an "all
  X" list). Each is `verified (<command>): <what it showed>`, run in this session against the
  current base branch, or `hypothesis — disprove if wrong: …`. Never present a guess as verified.

## 3. Pick the tier

| Tier | What it touches |
|---|---|
| **T0** | cosmetic changes, docs, tests, internal tooling |
| **T1** | product logic with no money and no other people's data |
| **T2** | money, auth, personal data, migrations, messages to customers, credentials, the process itself |

When two fit, the higher wins. The issue gets exactly one `tier:T0`, `tier:T1` or `tier:T2`
label. Add `vigia:ready` only when nothing is left open: every question answered, every claim
in `## Facts`. Otherwise create the issue without `vigia:ready` and say what is missing.

## 4. Before you create the issue

- [ ] `## Outcome` is one sentence and says nothing about how
- [ ] `Not touched:` and `Must still hold:` both have content
- [ ] every acceptance criterion can be checked by running or reading something
- [ ] every factual claim is in `## Facts` as verified (with its command) or as a hypothesis
- [ ] exactly one tier label, the higher if two fit
- [ ] the body passes vigia's check and prints `[]`:
      `python3 -c "import sys; from vigia.tickets import missing; print(missing(sys.stdin.read()))" < body.md`

## 5. Create it

Save the body to `body.md`, then:

```bash
gh issue create --title "<type>: <outcome in a few words>" \
  --label tier:T1 --label vigia:ready --body-file body.md
```

Use the tier you chose. Leave out `--label vigia:ready` if anything is still open.
