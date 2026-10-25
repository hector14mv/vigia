# Roadmap

vigia v0.1 is the core of a private factory, rewritten to run anywhere. That factory merged 360 pull
requests in two weeks; the story is in [the article](https://hectormoyanovelez.com/fabrica). v0.2 brings
over the rest, in roughly the order below. This page gives an order, never dates.

## Routing

- Difficulty labels: tickets say how hard they are, not only how risky.
- A router that picks engine, model and effort per tier × difficulty, so cheap work does not burn the best model.
- Quota-aware switching between labs, so work keeps moving when one lab runs low.

## Reviews

- A T1/T2 reviewer that repairs findings itself instead of a separate fix round, which saves a round trip.
- The GitHub Codex connector as an alternative gate, for people who already run it.
- A third lab (OpenCode) as a reserve gate when one lab's quota runs out, so a merge is not held for a quota.

## Merging

- Base updates: when the base moved after the review, vigia brings it into the PR and reviews the new head
  before it merges, so what was reviewed is what lands ([#39](https://github.com/hector14mv/vigia/issues/39)).
  Built and tested, held back from v0.1 by its review: a check made before the merge can race a moving
  base unless GitHub enforces up-to-date branches, and it must survive a crash between push and record.

- A final full CI run on the exact accepted head, so what merges is what was reviewed and tested.
- A classifier that parks any database migration that is not purely additive, because those need a person.

## Sessions

- Memory, disk and load gates per slot, so parallel sessions do not starve the machine.
- Resuming a finished session in its own context, so a follow-up does not start from zero.
- Warm dependency caches per lockfile, so a new session does not reinstall everything.

## The person's loop

- Decision cards with options and a recommendation, so a question to you takes seconds to answer.
- A "Probar" tray to confirm a change works or file a bug, so checking a result is one tap.
- Buttons on the Mesa once it has its own login, so you can act where you read.

## Notifications

- Phone cards with buttons, so you can answer without opening a laptop.

## Fuel and cost

- Claude quota reading, so vigia knows how much is left in each tank.
- A fuel rule that holds or reroutes work when a tank runs low, instead of failing mid-session.
- Tokens and cost per merged PR, so you can see what the factory costs.

## QA

- Visual QA at phone and desktop widths, so layout breaks are caught before merge.
- Capture cards after merge, so you can see what shipped.
- Automatic mutation checks, so tests that cannot fail are found without a person looking.

## Records

- A weekly engine table, so you can compare engines on real results.
- Routing verdicts that move a router cell with data, so routing improves from evidence.

## Operations

- Nightly CLI updates with a smoke test and rollback, so engine updates do not break the factory.
- The watcher deploying its own new main after its tests pass, so vigia keeps itself current.
