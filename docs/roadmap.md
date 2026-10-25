# Roadmap

vigia v0.1 is a private factory rewritten to run anywhere: parallel sessions, several repositories, the Mesa,
the planner and the engine report came over in the first weekend. That factory merged 360 pull
requests in two weeks; the story is in [the article](https://hectormoyanovelez.com/fabrica). v0.2 brings over
the rest, in roughly the order below. This page gives an order, never dates.

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
- A merge GitHub refuses because the branch is behind waits for a person with the reason, instead of being
  retried every tick ([#59](https://github.com/hector14mv/vigia/issues/59)).
- A T2 change waiting for its human label holds the merges that would make it conflict, so a person's approval
  is never thrown away by an unrelated merge ([#49](https://github.com/hector14mv/vigia/issues/49)).
- CI on the merge result when the base moved after the review: today CI and every review run on the PR's head,
  so a base that moved since is combined with it untested ([#39](https://github.com/hector14mv/vigia/issues/39),
  [#59](https://github.com/hector14mv/vigia/issues/59)).
- A classifier that parks any database migration that is not purely additive, because those need a person.

## Sessions

- Chosen environment variables passed into engine sessions, for Bedrock, Vertex or a proxy
  ([#42](https://github.com/hector14mv/vigia/issues/42)).
- Memory, disk and load gates per slot, so parallel sessions do not starve the machine.
- Resuming a finished session in its own context, so a follow-up does not start from zero.
- Warm dependency caches per lockfile, so a new session does not reinstall everything.

## The person's loop

- Decision cards with options and a recommendation, answered with a `/vigia decide <option>` comment, so a
  question to you takes seconds to answer.
- The planner promoting independent T0/T1 tickets to the queue by itself, with the dependency and duplicate
  rules its review found; in v0.1 you ratify every planned ticket.
- A post-merge check queue to confirm a change works or file a bug, so checking a result is one tap.
- Actions in the status desk (the Mesa) once it has its own login, so you can act where you read.

## Notifications

- Messages through your own Telegram bot when something needs you ([#44](https://github.com/hector14mv/vigia/issues/44)).
- Mobile notifications with actions, so you can answer without opening a laptop.

## Quota and cost

- Claude quota tracking, so vigia knows how much is left.
- A quota rule that holds or reroutes work when one lab runs low, instead of failing mid-session.
- Tokens and cost per merged PR, so you can see what the factory costs.

## QA

- Visual QA at phone and desktop widths, so layout breaks are caught before merge.
- Post-merge screenshots, so you can see what shipped.
- Automatic mutation checks, so tests that cannot fail are found without a person looking.

## Records

- A weekly engine digest on top of `vigia report`, so you can compare engines on real results without asking.
- Routing verdicts that move a router cell with data, so routing improves from evidence.

## Operations

- Nightly CLI updates with a smoke test and rollback, so engine updates do not break the factory.
- The watcher deploying its own new main after its tests pass, so vigia keeps itself current.
