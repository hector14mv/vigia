## Outcome
`vigia watch` sees a PR's CI result as soon as GitHub has it, instead of minutes later.

## Scope
`src/vigia/gh.py` (`GitHub.prs()`), and tests for it.
Not touched: `rules.py`, `watch.py`, the shape of the dict `GitHub.pr()` returns.
Must still hold: `prs(label)` returns exactly the open PRs that carry `label`, sorted by number, each in the same dict shape as `GitHub.pr(n)`; `watch.merge()` keeps re-reading the PR with `GitHub.pr()` right before merging.

## Acceptance criteria
- [ ] `prs(label)` takes the PR numbers from `gh pr list` but reads each PR's state (head, labels, mergeable, CI) with `gh pr view`, the same call `GitHub.pr()` makes
- [ ] a test with a fake runner shows that when `gh pr list` and `gh pr view` disagree about the CI rollup, `prs()` returns what `gh pr view` says
- [ ] a test shows `prs()` still returns PRs sorted by number and skips nothing

## Done-test
On a repository with an open vigia PR whose CI just finished, `vigia status` shows `ci passing` within one tick of the run completing.

## Risks
One extra `gh` call per open PR per tick; fine for tens of PRs. Facts: on 23-Oct, hector14mv/vigia#2's CI run completed at 19:29:49Z, but `gh pr list --json statusCheckRollup` kept reporting it as running until about 19:35Z, while `gh pr view --json statusCheckRollup` was already complete.
