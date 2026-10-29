## Outcome
After `vigia init`, only the person who set vigia up can approve a T2 change, unless they choose otherwise.

## Scope
`src/vigia/init.py` and `tests/test_init.py`.
Not touched: `config.py` parsing rules, `watch.human_approved`, every other module.
Must still hold: `vigia init --yes` still runs without prompts; the written `vigia.toml` still loads with `vigia.config.load()`; when the login cannot be read, init still writes a valid config.

## Acceptance criteria
- [ ] `init` reads the logged-in GitHub login (`gh api user --jq .login`) and writes it as `checks.humans = ["<login>"]`
- [ ] in interactive mode it asks "Who may add vigia:human-ok on T2 changes? (comma-separated GitHub logins)" with that login as the default; an empty answer keeps the default
- [ ] if the login cannot be read, `humans = []` is written and init prints one line saying anyone with write access can then approve T2
- [ ] tests (fake runner, no real `gh`) cover: login read → humans set; interactive answer with two logins; login unreadable → `[]` and the warning

## Done-test
In a scratch clone, `vigia init --yes --force --no-labels` writes `humans = ["<your login>"]` in `vigia.toml`.

## Risks
T1: it changes who can approve T2 merges by default. Facts: verified by the clean-install test of 23-Oct (`vigia init --yes --force --no-labels` in a fresh clone of vigia-demo turned `humans = ["hector14mv"]` into `humans = []`).
