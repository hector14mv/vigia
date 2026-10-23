# Contributing to vigia

## Set up and check

```bash
uv venv && uv pip install -e . pytest ruff
ruff check . && ruff format --check . && pytest
```

Run all three checks before you open a pull request; CI runs the same ones.

## Tests

Tests never touch a real repository or the network. The end-to-end test drives a real git remote
created in a temporary directory, with GitHub and the engines replaced by the fakes in
`tests/fakes.py`. Add new tests on top of those fakes. A test that cannot fail is not a test.

## Propose work

Open an issue from the vigia ticket template (`.github/ISSUE_TEMPLATE/vigia-ticket.md`). The body is
the builder's whole prompt, so say what should change, not how. Give it one tier label:

- `tier:T0`: cosmetic, docs, tests, internal tooling
- `tier:T1`: product logic with no money and no other people's data
- `tier:T2`: money, auth, data, migrations, messages to customers, credentials

Maintainers add `vigia:ready` when the ticket is ready to build.

## How this repository's pull requests flow

- vigia builds the pull request from the issue.
- A model from the other lab reviews it on the exact head commit. T1 and T2 get a stronger
  reviewer too, and T2 also needs a human label.
- `vigia watch` merges it only if every rule holds. The merge is a script with no AI.

Hand-written pull requests are welcome and join the same flow. A handoff comment starts with
`## Handoff <full 40-character head sha>` and has an `Engine:` line naming who wrote the change, for
example `Engine: claude · claude-opus-5-5`. vigia reads it only from the GitHub account it runs as,
so ask a maintainer to post it. It picks the other lab as reviewer. Without it the pull request is
parked as having an unknown builder. The full
operating model is in [docs/operating-model.md](docs/operating-model.md).

## Commits

Use conventional commit subjects: `feat:`, `fix:`, `docs:`, `test:`, `chore:`. One concern per commit.
