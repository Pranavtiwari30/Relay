# ADR-0003: A restricted declarative DSL for transformation

- **Status:** Accepted
- **Date:** 2026-10-11

## Context

Integrations must reshape payloads, and administrators must be able to change how without a
deploy. Whatever expresses that runs inside the worker on data from outside the organisation.

## Decision

Configuration is plain JSON validated into typed models and interpreted by a small pure function:

- **Paths** with dotted keys and numeric indexes only (`customer.name`, `items[0].sku`); depth ≤ 12.
- **Mappings**: source path or constant → target path, with `required`, `default`, one conversion
  from a fixed list, and `each` for arrays (nesting ≤ 3, ≤ 1000 items).
- **Filters**: eleven enumerated operators, combined with AND.
- **Schemas**: JSON Schema restricted to a keyword allowlist — no `$ref` (no network fetches), no
  `pattern` (no regular-expression backtracking on attacker-supplied strings).

Nothing evaluates an expression. There is no `eval`, no template language and no uploaded code.
One function, `run_pipeline`, is used by ingestion, the worker, the editor preview and the
assistant's dry-run tool.

## Alternatives considered

- **JSONata / JMESPath / jq.** Far more expressive, but each is an interpreter for untrusted
  programs: harder to bound in time and memory, harder to explain errors for, and a larger attack
  surface. The power is not needed for field mapping.
- **User-supplied Python or JavaScript.** Maximum flexibility and an unbounded security and
  operability problem (sandboxing, resource limits, dependency drift).
- **Hard-coded transformations per integration.** Safe, but every change is a code deploy.

## Consequences

- Transformation is deterministic and bounded, which is what makes replay and dry-run validation
  trustworthy.
- Errors are specific: each names a code, the path it concerns and a message, and all problems are
  reported together.
- The DSL cannot compute (no arithmetic, concatenation or conditionals). Payloads that need that
  are out of scope for v1; the right extension is a new enumerated operation, not an expression
  language.
