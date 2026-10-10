---
paths:
  - "docs/**"
  - "README.md"
  - "CLAUDE.md"
---

# Documentation rules

## Keep it true

- Write what the code does today. If something is planned, unverified or untested, say so in those
  words.
- **No unmeasured claims.** Do not state performance, accuracy, savings, usage, uptime or a
  successful deployment unless it was measured or done, and say how. There are no customers and no
  production usage; do not imply any.
- Guarantees are stated exactly: "at-least-once delivery with idempotency protections", never
  "exactly-once".
- Provider limits and prices carry a source link and the date they were checked.
- Test counts and coverage in docs come from an actual run, with its date.

## Keep it current

`docs/PROJECT_STATE.md` is the committed source of truth for project memory. Update it in the same
change whenever any of these move: phase and completed capabilities, architecture or key
decisions, setup commands, test suites and their last verified status, environment variable
**names** (never values), known bugs and limits, unverified assumptions, next tasks, blockers.

Also update:

- `docs/PRD.md` — when a requirement or acceptance criterion changes;
- the relevant `docs/architecture/*.md` — when behaviour it describes changes;
- `docs/design/api-contracts.md` and `make openapi` — when API conventions or endpoints change;
- `docs/quality/testing-strategy.md` — when a test layer or the AC mapping changes;
- `README.md` — when setup or the demo changes.

## ADRs

Write one in `docs/architecture/decisions/ADR-NNNN-title.md` for a meaningful, hard-to-reverse
choice: context, decision, alternatives considered, consequences, date. Do not rewrite an accepted
ADR to reflect a new decision; add a new one that supersedes it.

## Style

- Lead with what the reader needs to do or know. Prefer a table or a short list to a paragraph of
  options.
- Define a term once and use it consistently (event, attempt, cycle, replay, integration,
  connection, workspace).
- Do not paste large code blocks that will drift; link to the file.
- Never include secret values, real account ids or personal data in examples.
