## Outcome
The Mesa gets a fifth place, «Office» (route `#/office`, its own icon in the phone tab bar and an entry in the desktop rail), that shows the factory live as a small top-down office: one desk per engine slot, a little figure at each busy desk doing that job, the PRs moving between desks, an out tray of today's merges and a «waiting for you» shelf.

## Scope
Touches `src/vigia/mesa.html` (the single-file Mesa UI) and the Mesa tests (`tests/test_mesa.py`, and `tests/mesa_render.js` only if the harness needs a small extension, e.g. a reduced-motion switch for `matchMedia`).

What the Office shows, built only from the `/api/state` the page already polls every 15 s:
- **Desks**: one desk per slot, using the same slot count the Factory's Sessions section already uses (`state.slots` for a workspace, else each repo's `slots`, never fewer desks than running jobs). A desk with no job is empty and reads as free.
- **Figures**: each entry of `repos[].jobs` is one small figure seated at a desk, labelled with its role (builder / gate / reviewer / fixer — the same words the Factory's Sessions use for `job.action`), its engine and exact model (`job.engine` · `job.model`, shown as given, never shortened or guessed; unknown when null), its PR `#n` or `issue #n` with the short repo name, and the elapsed time since `job.since`. A figure over the repo's `session_minutes` cap is marked the same way the Factory marks it. A figure on a PR links to that PR's existing detail route (`#/pr/owner/repo/n`).
- **PRs moving**: when a gate or review job runs on a PR, that PR's card is shown at the gate or reviewer desk (moving there with a short transition when the poll changes it), not left at the builder's desk.
- **Out tray**: `repos[].merged_today` piles up as a stack in an «Out tray» corner, with a count and each PR's number and title.
- **Waiting-for-you shelf**: parked PRs (`prs[].parked`) and parked issues (`parked_issues`) sit on a «Waiting for you» shelf with a count; the shelf links to `#/turn`.
- **Quiet office**: with no running job anywhere, the Office says the office is quiet (and still shows the empty desks, the out tray and the shelf).
- Unknown is never shown as empty: if a repo's lists could not be read (`unread`, `prs: null`, a repo `error`), the Office says so the way the other places do, instead of an empty shelf or tray.

The office is drawn from scratch inside `mesa.html` with plain HTML/CSS/inline SVG in the page's existing style (the `h()` DOM builder, text nodes only, the `--mesa-*` theme tokens). Calm, legible motion is the point: a builder's figure «types», a gate or reviewer «reads»; nothing that needs a GPU.

Not touched: the merge path and its rules (`src/vigia/rules.py`, `src/vigia/watch.py`, `src/vigia/gate.py`); `src/vigia/mesa.py` and the shape of `/api/state` (no new endpoint; if one turns out to be strictly needed it must be GET-only and read-only, and the PR must say why); the content and behaviour of the Your turn, Factory, Feed and Fuel places, the PR detail screen and the decide flow; `pyproject.toml` and every dependency (no new Python or JS dependency, no CDN, no external asset beyond what `mesa.html` already loads); the server's CSP and host checks.

Must still hold: the four existing places, the read-only page, phone and desktop layouts, both themes and reduced motion, concretely:
- Your turn, Factory, Feed and Fuel render exactly as before on both layouts; every existing test in `tests/test_mesa.py` passes, changed only where it counts the places (e.g. `test_the_page_has_the_four_places_and_writes_nothing` becomes five places, still asserting the page writes nothing).
- The page stays read-only: no `<form>`, no write verb, no `innerHTML`, no `<script src>`, no fetch other than `/api/state` and `/api/pr/`; every API string goes in as text, and the only external links are the GitHub ones the `GH` pattern already allows.
- Works at 375 px wide with no horizontal scroll (the tab bar fits five tabs with a 44 px touch target each) and on the ≥1024 px desktop layout.
- Light and dark theme both read correctly, using only the existing `--mesa-*` tokens (no new hard-coded colours).
- `prefers-reduced-motion: reduce` turns every Office animation and transition off: figures and PRs sit in static positions, as `.pulse` already does.
- The saved last tab (`mesa.tab` in browser storage) accepts `office`, and an unknown or old saved value still lands on Your turn.

## Acceptance criteria
- [ ] `#/office` is a place like the other four: listed in the phone tab bar with its own icon and in the desktop rail, highlighted when active, and remembered as the last tab.
- [ ] With 0 running jobs, `#/office` renders on both layouts with one empty desk per slot and text saying the office is quiet (test via `tests/mesa_render.js`, in the style of `test_every_place_renders_on_both_layouts_and_says_when_nothing_needs_you`).
- [ ] With N running jobs in the state, `#/office` renders exactly N figures, each showing its role, engine, exact model, `#n` (or `issue #n`) and elapsed time; the number of desks equals the slot count shown by the Factory (test with N ≥ 2, including one gate or review job).
- [ ] A gate or review job on PR #n shows PR #n at that desk, not at a builder desk (test).
- [ ] `merged_today` entries appear in the out tray with their count; parked PRs and parked issues appear on the «Waiting for you» shelf with their count and a link to `#/turn` (test).
- [ ] A repo whose lists could not be read shows as unknown in the Office, never as an empty tray or shelf (test).
- [ ] Every Office animation and transition is declared only under `@media (prefers-reduced-motion: no-preference)` (or switched off under `reduce`), so reduced motion leaves static positions (test that reads the page, like the existing page-content tests).
- [ ] The Office's markup uses no colour outside the `--mesa-*` tokens and has no fixed width wider than the viewport at 375 px (test or a stated manual check in the PR with screenshots at 375 px light and dark and at desktop).
- [ ] The read-only and text-only assertions of the existing page tests still pass with the Office included.
- [ ] `ruff check . && ruff format --check . && pytest` all pass.

## Done-test
With `vigia watch` running in a configured repo, run `vigia mesa` and open http://127.0.0.1:8790/#/office: the desks show, one per slot; each running session sits at its desk as a figure labelled with role, engine · model, PR or issue number and elapsed time; today's merges are in the out tray. With no session running, the page says the office is quiet. Switch the theme control and the OS reduced-motion setting: both themes read correctly and nothing moves with reduced motion. Narrow the window to 375 px: no horizontal scroll, five tabs fit.

## Risks
- The phone tab bar goes from four to five items: labels or the badge on Your turn could overflow at 375 px. Check it.
- Animation that keeps running on a page polled every 15 s could re-trigger on every render (the page rebuilds the DOM): a PR should only move when its desk actually changes between polls, and re-renders must not restart animations or steal focus.
- Wrongly showing an unread list as empty would tell the person there is nothing waiting for them; follow the existing «unknown, not none» rule.
- No personal data, money, auth or security surface is touched; the page stays read-only.

Tier: T1 (product UI, no money, no other people's data). Difficulty: acotado (bounded).
