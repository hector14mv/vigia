## Handoff 28df5bdc9de29930b9d9411e17a80d67c8faf1a0
Engine: claude · claude-sonnet-5-5 · effort high · builder · lab anthropic
Ticket: #103 · tier T1
Session: 714 s · exit 0
Tests run by vigia: `uv run --quiet --with pytest --with ruff sh -c 'ruff check . && ruff format --check . && pytest -q'` → passed — ..........................                                               [100%]
Files changed: `src/vigia/mesa.html`, `tests/mesa_render.js`, `tests/test_mesa.py`

## Summary
- `src/vigia/mesa.html`: a fifth place, `office` (`#/office`), added to `PLACES` (phone tab bar, with its own `office` icon, and the desktop rail), so the saved `mesa.tab` accepts it and an unknown or old value still lands on Your turn. `phoneShell`/`deskShell` route to the new `phoneOffice`/`deskOffice`.
- The Office is built only from `/api/state` with the `h()` builder, text nodes and inline SVG (new `sv`/`art` helpers). One desk per slot (`slotsTotal()`, the Factory's own count, never fewer than running jobs; free desks capped at 12 like the Factory's). Each `repos[].jobs` entry is one figure: role (`roleOf`, the Factory's words), `engine · model` as given (`unknown` for a null part, "engine and model unknown" when both are null), repo short name with `#n` or `issue #n`, elapsed time, the Factory's over-the-cap mark, and a link to `#/pr/owner/repo/n` for PR jobs. Builder/fixer figures "type", gate/reviewer figures "read".
- PR cards: a PR with a running job is shown at that job's desk; PRs with no job sit in a "Between desks" lane with their next step; parked PRs go to the shelf. An issue build shows its issue and branch at the builder desk.
- Out tray (`merged_today` across repos, count + each PR's number and title, links to GitHub) and a "Waiting for you" shelf (parked PRs and parked issues, count, link to `#/turn`).
- Quiet office text when no job runs; "unknown, not none" for `error`, `unread`, `prs: null`, `parked_issues: null` and `pending` repos (reusing `repoErrors`, `readingNote`, `prsUnknown`).
- Motion: all Office animation lives in one `@media (prefers-reduced-motion: no-preference)` block (hands tap, sheet scan, arriving PR card). A job keeps its desk between polls (`S.office.seat`); a PR card animates only when its place differs from the last drawn one (`S.office.placed`); the loop animations start at the clock's phase so a rebuilt DOM does not restart them.
- `sessions()` now also returns `tier` (additive; the Factory does not use it). The CSS block is delimited by `/* office: ... */ ... /* /office */`, the JS by `Office:` ... `/* /office */`, which the tests read.
- `tests/mesa_render.js`: small extension: `MESA_STORED` env var seeds browser storage, and each view returns what the page saved (`saved`). Nothing else in the harness changed.
- `tests/test_mesa.py`: the four-places test is now five-places (still asserts the page writes nothing); `PLACES` and the every-place test include `office`; 8 new tests (quiet office with one free desk per slot, figures with role/engine/model/ref/time and the Factory's slot count, gate PR at the gate desk and not at the builder desk, out tray and shelf counts and `#/turn` link, unknown/reading repos, PR moves only when its desk changes across a poll via `--live`, tab bar/rail/remembered tab, reduced-motion and token-only/narrow-width scan of the Office CSS and JS).
- Manual check (headless Chrome, screenshots looked at, not committed): Office at 375 px (inside a 375 px iframe) light and dark, and at 1280 px light and dark: five tabs fit, no horizontal scroll, both themes legible.

## Tests run
- `uv run --quiet --with pytest --with ruff sh -c 'ruff check . && ruff format --check . && pytest -q'`: ruff check passed, 60 files already formatted, all tests passed (0 failed).
- Headless Chrome screenshots of `src/vigia/mesa.html` with a sample state at 375 px and 1280 px, light and dark (manual).

## Deviations
- Tab placement: "Office" is appended as the fifth tab/rail item (after Fuel), as the ticket says "a fifth place".
- "PRs moving": the ticket does not say where a PR sits when no job runs on it. I added a "Between desks" lane for open, non-parked PRs without a running job (shown with `prStatus`), so a PR visibly moves lane → gate desk → lane. Parked PRs show only on the shelf, as specified; PRs waiting for the person's label show in the lane with "Waiting for your label" (the shelf holds only parked items, per the ticket).
- Out tray empty text: `merged_today` comes from this machine's ledger, not GitHub, so an unreadable repo does not make the tray unknown; when a repo could not be read and the tray is empty, it says the ledger is read from this machine (instead of claiming the list may be missing). The top-of-Office notes, shelf and lane say "unknown" for what GitHub could not answer.
- Issue builds' figure is not a link (the ticket links only figures on a PR).
- Harness: also returns `saved` per view (needed to assert `mesa.tab` is remembered).

## Risks
- Desk assignment is in-memory (`S.office.seat`): a page reload reassigns desks by current job order; within a session a job keeps its desk. Jobs have no slot id in the state, so this is the only stable option without touching `/api/state`.
- Look at the `placed`/`arrive` logic (`placeOf`, `officeBody`): it mutates `S.office.placed` during render; a render on another tab does not touch it, so returning to the Office after a change replays one arrival.
- Free desks are capped at 12 (the Factory's own cap), so with more than 12 slots the desk count is the Factory's displayed count, not `slots`.
- The reduced-motion and token tests read the Office CSS/JS by marker comments; moving or deleting those markers breaks them on purpose.
- Inline `animation-delay` styles are set on SVG parts by JS; they only matter when the no-preference block applies.

## Follow-ups
- none
