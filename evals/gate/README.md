# Gate evals

Each directory here is one **case**: a past pull request with a known outcome. `vigia eval gate` replays the
cases through the tier's gate and says how often the gate catches the known defect and how often it blocks a
clean change, so a gate model or prompt change can be compared before it reaches real PRs.

```
vigia eval gate [DIR] [--runs N] [--case NAME]... [--engine E --model M [--effort L]]
```

A run is exactly one gate session as `vigia review --kind gate` runs it (same prompt, same read-only engine
path, same verdict parser) on a detached worktree at the case's `head`, minus GitHub and the ledger: it never
comments, labels or appends to `.vigia/ledger.jsonl`. It only fetches `refs/pull/<pr>/head` when the clone
lacks the head. Each run is a real session from your quota; the count is printed before anything starts.

## A case

```
evals/gate/<name>/
  case.toml
  ticket.md     the ticket body, as the builder saw it
  handoff.md    the builder's handoff for that head
```

```toml
pr = 17                                   # the PR it came from, for people; also used to fetch the head
head = "<40-char sha>"                    # the commit the gate reviews
base = "<40-char sha>"                    # the diff is base...head
tier = "T1"                               # T0, T1 or T2: picks the gate seat from vigia.toml
builder = { engine = "claude", model = "claude-sonnet-5-5" }   # who wrote the change
expect = "p1"                             # "p1": a real defect is there; "clean": nothing should block
files = ["src/vigia/gh.py"]               # p1 only: the file(s) where the real defect is
why = "one sentence on the real defect and the commit that fixed it"
```

A case with a missing file, a short sha, an unknown tier, an `expect` other than `p1`/`clean`, or `expect = "p1"`
with no `files` is refused with its name and the problem; the other cases still run. So is a case whose gate
seat comes from the builder's own lab (or whose lab is unknown), and one whose `head` or `base` is still not in
the clone after fetching `refs/pull/<pr>/head` once.

## Scoring

Deterministic, no model:

| Case | Outcome |
| --- | --- |
| `p1` | **caught**: a P1 finding whose `file` equals one of `files` (a leading `./` is ignored). **missed**: anything else; a P1 on another file is shown as "P1 elsewhere". |
| `clean` | **clean**: no P1. **false alarm**: any P1. |
| either | **unavailable**: no parsable verdict, a timeout, a non-zero exit or an engine error. Never caught or clean. |

The output has one line per case and run, then caught runs / p1 runs, false alarms / clean runs, unavailable
runs, the cases whose outcome differed between runs and the seat used. The full result, every finding included,
is written to `.vigia/evals/gate-<UTC timestamp>.json`. The exit code is 0 when every run finished (whatever the
score) and 1 when a case was refused or a run was unavailable.
