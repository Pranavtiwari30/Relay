# UI and UX

Relay's interface is an **operations console**: dense enough to work in, calm enough to read
during an incident. It is not a marketing page and has no decorative elements, hero panels or
animation.

Screenshots in [screenshots/](screenshots/) were captured from a real run of the pipeline in a
throwaway database (`RELAY_CAPTURE_SCREENSHOTS=1 npx playwright test screenshots`). Nothing in them
is mocked.

## Principles

1. **Every number is real.** Figures come from `GET /dashboard/summary`, which computes them from
   stored rows. Nothing is hard-coded or estimated client-side.
2. **Missing is not zero.** "No finished events yet" renders as "—", never "0%" or "100%".
3. **Demo data says so.** A demo workspace shows a persistent banner on every page.
4. **State is always explained.** Every data view has loading, empty, error and permission-denied
   states. Errors show the server's message and a reference id.
5. **Colour never carries meaning alone.** Statuses have an icon and a text label as well as a tint.
6. **Hypotheses are labelled.** An assistant's "likely cause" is titled *hypothesis* and shown apart
   from observed facts.
7. **Destructive or consequential actions are explicit.** Replay, pause, disable and activate each
   open a confirmation that says what will happen.
8. **The UI is not the control.** Hidden buttons are a courtesy; the API enforces permissions.

## Navigation

Overview · Integrations · Events · Failures · Investigations · Audit log · Settings.

A left sidebar on wide screens; a horizontally scrolling bar at the top below 900 px. The footer of
the sidebar shows the workspace (a selector if the user has more than one), the signed-in email,
the role, and sign-out. A skip link is the first focusable element.

## Screens

| Screen | Purpose | Key content |
|---|---|---|
| **Sign in** | Start a session | Plain-language errors for bad credentials and rate limiting. |
| **Overview** | "Is delivery healthy?" | Time range (1 h / 24 h / 7 d / 30 d); tiles for received, delivered, failed, in flight, success rate, delivery time; a *Needs attention now* panel that ignores the time range; events-per-bucket chart; integration health; recent failures. |
| **Integrations** | List and create | Status, source → destination, version, last change. |
| **Integration detail** | Operate and configure | Activate / pause / resume / disable with confirmation; route and webhook URL; the editor; version history. |
| **Integration editor** | Change configuration safely | Form for basics, routing, filters, mappings, schemas, reliability; a JSON tab for the whole config; *Test with a sample* (dry run); *Validate* (without saving); save with a change note → new version. |
| **Events** | Find an event | Filters for status, integration and exact id; filters live in the URL; "Load more". |
| **Event detail** | "What happened to this event?" | Status, ids, correlation id, version used; merged timeline of state changes and attempts; attempts table with expandable request/response; redacted payloads side by side; replays; investigations. Actions: Investigate, Replay. |
| **Failures** | Triage | Tabs for dead-lettered, validation-failed and retrying, each with a one-line explanation of what to do. |
| **Investigations** | Read a diagnosis | Mode banner (AI-assisted or rule-based, and any fallback reason); summary; observed facts with evidence chips; likely cause with confidence; uncertainty; missing evidence; recommendations with approve/reject; how it was produced. |
| **Audit log** | "Who changed what?" | Actor, action, resource, details; filter by kind of action. |
| **Settings** | Administer | Connections (with secret-reference status), members and a role explainer, demo tools. |

### Replay confirmation

The dialog states that existing history is kept, that the replay is audited under the user's name,
and — because a timed-out attempt may already have been processed — that the same idempotency key
is sent. A reason is required. The user chooses the current or the original configuration version.
The dialog generates one idempotency key when it opens, so a double submit cannot replay twice.

### Role-dependent behaviour

| Element | Viewer | Operator | Administrator |
|---|---|---|---|
| Replay, Investigate, approve/reject | hidden, with a note explaining why | shown | shown |
| Pause / Resume | hidden | shown | shown |
| Activate / Disable, editor, New integration | read-only JSON view | read-only JSON view | shown |
| Connections: create / edit / delete | hidden, with a note | hidden, with a note | shown |
| Members: add / change role / remove | list only | list only | shown |
| Demo tools | state visible, controls disabled | shown | shown |

## Visual system

Defined once as CSS custom properties in `apps/web/src/styles.css`; components reference roles,
never raw colours.

| Token group | Light | Dark |
|---|---|---|
| Page / surface | `#f9f9f7` / `#fcfcfb` | `#0d0d0d` / `#1a1a19` |
| Ink primary / secondary / muted | `#0b0b0b` / `#52514e` / `#73716c` | `#f0efec` / `#c3c2b7` / `#9a9892` |
| Accent | `#2a78d6` | `#3987e5` |
| Status: good / warning / serious / critical | `#0ca30c` `#fab219` `#ec835a` `#d03b3b` (same in both themes) | |

- **Type:** the system UI font; monospace for ids, JSON and code. 14 px base; headings 20 / 15 / 13.
- **Spacing:** a 4 px scale (4, 8, 12, 16, 24, 32).
- **Theme:** follows `prefers-color-scheme`.
- **Motion:** only a loading spinner, disabled under `prefers-reduced-motion`.
- **Text never takes a status or series colour.** Values and labels stay in ink tokens; a tinted
  badge, dot or swatch beside them carries the meaning.

### Status vocabulary

| Tone | Icon | Used for |
|---|---|---|
| Good | ✓ | Delivered; Active; attempt succeeded |
| Info | ● ◐ ↻ | Received, Pending publish, Queued, Processing, Replay requested |
| Warning | ↻ | Retry scheduled |
| Serious | ❙❙ | Held; Paused |
| Critical | ✕ | Validation failed; Dead-lettered; attempt failed |
| Neutral | – ○ | Filtered; Draft; Disabled |

## The chart

**Form.** Events per hour (or day) is part-to-whole over time, so it is a stacked column chart.
The headline figures are stat tiles, not charts.

**Colour.** The three series — delivered, failed, other — use categorical slots (blue, orange,
aqua) rather than green and red. The obvious green/red pairing was checked with a palette validator
and **failed**: under deuteranopia the two differ by ΔE 4.1, below the threshold of 8, so adjacent
"delivered" and "failed" segments would be nearly indistinguishable to red-green colour-blind
readers. The replacement trio passes every check in both themes.

| Check (light / dark) | Result |
|---|---|
| Lightness band, chroma floor | pass / pass |
| Colour-vision separation, worst adjacent pair | ΔE 9.2 / 9.4 (target ≥ 8) |
| Normal-vision separation | ΔE 27.6 / 26.5 (floor 15) |
| Contrast vs surface | aqua 2.74:1 in light (below 3:1) → mitigated below |

**Not relying on colour.** A legend is always shown. A **View as table** toggle gives the exact
values (the mitigation the contrast warning requires). Each column is a keyboard-focusable target
with an `aria-label` stating its values, and shows a tooltip on hover or focus.

**Marks.** Columns ≤ 24 px wide, rounded only at the data end, a 2 px surface gap between stacked
segments, hairline grid, three y-ticks. The chart is hidden when there are fewer than two buckets,
since a single column is not a trend.

## Accessibility

- Semantic landmarks (`nav`, `main`), one `h1` per page, table headers with `scope`.
- All controls are native elements reachable by keyboard; visible focus ring everywhere.
- Dialogs use the native `<dialog>` element (focus trap, Escape to close) with a labelled title.
- Form fields have associated labels; hints and errors are linked with `aria-describedby`; errors
  use `role="alert"`.
- Loading uses `role="status"`; asynchronous results use `aria-live`.
- Statuses are text + icon; the icon is `aria-hidden` and the label carries the meaning.
- Time is shown in the user's locale with the exact value in a tooltip.

Automated checks cover a subset (labels, roles, keyboard path to the skip link, status text). A
full audit with assistive technology has not been done.

## Known gaps

- Nested `each` mappings are edited in the JSON tab; the form shows them read-only.
- No bulk replay.
- Lists refresh by polling (10 s; 2 s on an in-flight event), not by push.
- Time range on the Overview is a fixed set of presets.
