# Contributing to vigia

## Set up and check

```bash
uv venv && uv pip install -e . pytest ruff
source .venv/bin/activate
ruff check . && ruff format --check . && pytest
```

Run all three checks before you open a pull request; CI runs the same ones.

## Tests

Tests never touch a real repository or the network. The end-to-end test drives a real git remote
created in a temporary directory, with GitHub and the engines replaced by the fakes in
`tests/fakes.py`. Add new tests on top of those fakes. A test that cannot fail is not a test.

## Propose work

Open an issue from the vigia ticket template (`.github/ISSUE_TEMPLATE/vigia-ticket.md`). The body is
the builder's whole prompt, so say what should change, not how. Give it one tier label, `tier:T0`,
`tier:T1` or `tier:T2`, as [the operating model's table](docs/operating-model.md#1-damage-sets-the-review-not-the-author) defines them; when two
fit, the higher wins.

Maintainers add `vigia:ready` when the ticket is ready to build.

## How this repository's pull requests flow

- vigia builds the pull request from the issue.
- A model from the other lab reviews it on the exact head commit. T1 and T2 get a stronger
  reviewer too, and T2 also needs a human label.
- `vigia watch` merges it only when `rules.decide()`, a pure function (no model, no I/O) over CI status,
  verdicts and labels on the exact head, says so; it is tested row by row in `tests/test_rules.py`.

Pull requests vigia did not build are welcome and join the same flow. A handoff comment starts with
`## Handoff <full 40-character head sha>` and has an `Engine:` line naming who wrote the change, for
example `Engine: claude · claude-opus-5-5`. vigia reads it only from the GitHub account it runs as,
so ask a maintainer to post it. Without it the pull request is parked as having an unknown builder.
A manual pull request also needs two labels, or vigia never picks it up: `vigia:review` and exactly
one `tier:T0`, `tier:T1` or `tier:T2` (with none or several tier labels it is parked).

The gate of each tier is fixed in `vigia.toml`, and it is Codex in this repository. A gate from the
builder's own lab is never used: a pull request whose handoff says `Engine: codex · …` is parked. So here such
a pull request declares `Engine: claude · <model>`. To gate Codex-written work, a maintainer has to change the
`gate` in `vigia.toml` first.

The full operating model is in [docs/operating-model.md](docs/operating-model.md).

## Commits

Use conventional commit subjects: `feat:`, `fix:`, `docs:`, `test:`, `chore:`. One concern per commit.
