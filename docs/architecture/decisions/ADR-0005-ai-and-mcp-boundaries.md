# ADR-0005: The investigation assistant is read-only, bounded and optional

- **Status:** Accepted
- **Date:** 2026-10-11

## Context

Diagnosing a failed delivery means reading several records and comparing them — a reasonable task
for a language model. But the model would be reading text controlled by third parties (payloads,
destination responses), inside a system whose whole value is that it does not do surprising things
to data.

## Decision

1. **Only on request.** An investigation runs when an operator asks for one. Event processing never
   calls a model, so Relay behaves identically with every provider absent.
2. **One read-only tool registry**, used both by the in-process investigator and by the MCP server.
   Seven tools; none writes, replays, fetches a URL, queries freely, or touches the filesystem.
3. **Read-only enforced by the database**, not just by code: tools run in a `READ ONLY` transaction.
4. **Model output is untrusted input.** The report is schema-validated; evidence references are
   checked against what the tools actually returned; unsupported "facts" are demoted; every
   recommendation requires human approval; proposed mappings are re-validated server-side.
5. **Approval records a decision and executes nothing.** Acting on it is a separate,
   permission-checked request by the operator.
6. **Bounded:** model turns, tool calls per turn, tool-result size, per-request and total time.
7. **Provider-neutral core with small adapters:** Ollama (local, no cost), any OpenAI-compatible
   endpoint, and Anthropic via its official SDK. Providers are tried in order; any failure falls
   through to the next, then to a **deterministic rule-based diagnosis** that produces the same
   report shape from the same tools.
8. **Final answer via a `submit_report` tool** rather than forced tool choice or sampling
   parameters, which current models reject.

## Alternatives considered

- **Let the assistant apply fixes.** Rejected: a prompt-injected response could then change
  configuration or replay events.
- **LiteLLM or a similar abstraction.** A large dependency for three small adapters; the adapters
  are under 150 lines each and their wire formats are pinned by contract tests.
- **Embeddings / RAG.** Nothing here needs retrieval over a corpus: the evidence for one event is a
  handful of rows fetched by id.
- **The in-process investigator speaking MCP to its own server.** Adds process management for no
  gain; sharing the registry gives the same guarantee, and the MCP server is tested through an
  in-process client.

## Consequences

- The worst outcome of a successful prompt injection is a misleading report that a human reads —
  and its evidence trail shows what was and was not verified.
- Diagnosis quality with a model is not measured beyond nine fixtures (`evals/`), and no accuracy
  claim is made. The deterministic path passes all nine.
- Hosted providers cost money per request and are off unless a key is supplied.
