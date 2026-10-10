---
paths:
  - "apps/web/**"
---

# Frontend rules

- TypeScript strict, including `noUncheckedIndexedAccess`. No `any`, no non-null assertions.
  `npm run lint`, `npm run typecheck` and `npm test` must pass.
- All API access goes through `src/api/client.ts` (CSRF token, workspace header, typed `ApiError`)
  and the hooks in `src/api/hooks.ts`. Query keys are scoped by workspace.
- **No invented numbers.** Every figure comes from the API. A missing value renders as "—" via
  `lib/format.ts`, never as 0, 0% or 100%.
- Every data view handles loading, empty, error and permission-denied. Use `QueryState`,
  `EmptyState`, `ErrorState`, `MutationError` from `components/ui.tsx`.
- A control either works or is disabled with a stated reason. Do not ship placeholder buttons.
- Permissions: `useAuth().can()` only hides or disables controls. Say why when something is hidden.
  Never treat the UI as the security boundary.
- Consequential actions (replay, pause, disable, activate) need a confirmation that explains the
  effect. Replay generates one idempotency key per dialog.
- Payloads and destination responses are untrusted text: render as text content only. Never use
  `dangerouslySetInnerHTML`.

## Accessibility

- Native elements first. Every input has a label (`Field`). Icons that carry no information are
  `aria-hidden`. Status is always text plus icon, never colour alone.
- Keyboard: everything reachable, focus visible. Dialogs use the shared `Dialog` (native
  `<dialog>`).

## Visual system

- Use the tokens in `src/styles.css`; do not hard-code colours in components.
- Text stays in ink tokens. Status and series colours go on a badge, dot or swatch.
- Charts: pick the form before the colour; never use red/green as adjacent series; always provide a
  legend and a table view. If you change chart colours, re-validate both themes and update
  `docs/design/ui-ux.md`.
- No decorative animation, hero panels or marketing copy. This is an operations console.

## Tests

- Unit and component tests use `src/test/harness.tsx` (`mockApi`, `renderRoute`). An unhandled
  request fails the test.
- Browser tests in `e2e/` must not depend on data created by another test.
