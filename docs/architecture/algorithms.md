# Algorithms and data structures

Each of these exists because a specific problem needed it. For each: the problem, the mechanism,
its cost, and what happens under concurrency and restarts.

## 1. Deduplication — hash + unique constraint

**Problem.** Senders retry. The same event must not be processed twice, including when two copies
arrive at the same instant on different API instances.

**Mechanism.** `dedup_key = sha256(source_event_id)` (or of the raw body when there is no id).
Ingestion runs `INSERT … ON CONFLICT (integration_id, dedup_key) DO NOTHING RETURNING id`; no row
returned means "duplicate".

**Cost.** One B-tree probe, O(log n).

**Concurrency / restart.** The unique index is the correctness boundary. A concurrent duplicate
blocks on the index until the first insert commits, then conflicts. No in-memory set is used, so
there is nothing to lose on restart and nothing to disagree across instances.
*(`test_concurrent_duplicates_create_exactly_one_event`)*

## 2. Finite-state machine — adjacency table

**Problem.** Status changes scattered across handlers drift into impossible states.

**Mechanism.** `TRANSITIONS: dict[status, frozenset[status]]` plus one function that performs every
change and writes the history row. A static AST test rejects any other assignment to
`Event.status`.

**Cost.** O(1) lookup.

**Concurrency / restart.** The caller holds the event's row lock; the change and its history row
commit together or not at all. The table is immutable, so there is no in-memory state to recover.

## 3. Transactional outbox — claim with lease and `SKIP LOCKED`

**Problem.** Commit-then-publish loses messages on a crash between the two; publish-then-commit
emits messages for rolled-back work.

**Mechanism.** Write an outbox row in the event's transaction. A dispatcher claims due rows with
`SELECT … FOR UPDATE SKIP LOCKED`, stamps a `claim_token` and `lease_expires_at`, commits,
publishes, then marks rows published `WHERE claim_token = ?`.

**Cost.** The claim uses a partial index over pending rows only, so it is O(batch · log p) for *p*
pending rows, independent of how much history the table holds.

**Concurrency.** `SKIP LOCKED` hands concurrent dispatchers disjoint rows without blocking. The
token makes the final update a compare-and-set: a dispatcher that lost its lease updates nothing.
*(`test_concurrent_dispatchers_never_publish_the_same_row_twice`)*

**Restart.** Crash before publish → the lease expires and the row is claimed again. Crash after
publish → the row is published twice and the worker discards the duplicate. The trade is explicit:
duplicates are possible, loss is not.

## 4. Exponential backoff with full jitter

**Problem.** Retrying immediately hammers a struggling destination; retrying on a fixed schedule
synchronises many events into waves.

**Mechanism.** `delay = uniform(0, min(max_delay, base · 2^(attempt−1)))`. A destination's
`Retry-After` raises the delay but is capped at `max_delay`, so a hostile or broken destination
cannot park an event indefinitely. Attempts are capped at 1–10.

**Cost.** O(1). The exponent is clamped, so there is no overflow at any attempt number.

**Determinism.** The random source is injected; a property test checks `0 ≤ delay ≤ max_delay` for
arbitrary inputs. **Restart.** `next_attempt_at` is stored, so a restart neither loses nor resets a
scheduled retry.

## 5. Adapter registry — map from type to implementation

**Problem.** Adding a connector should not mean editing ingestion, the worker and the API.

**Mechanism.** `AdapterRegistry` holds `dict[str, SourceAdapter]` and `dict[str, DestinationAdapter]`.
Core code calls protocol methods only. A contract test iterates over whatever is registered.

**Cost.** O(1) lookup. Built once per process; read-only afterwards, so safe to share across threads.

## 6. Transformation interpreter — bounded tree walk

**Problem.** Mapping one JSON shape to another, configurable at runtime, without running user code.

**Mechanism.** Paths are parsed into a tuple of keys and indexes (`items[0].sku` →
`("items", 0, "sku")`); there are no wildcards, filters or expressions. Each mapping is `get` →
optional whitelisted conversion → `set`. `each` maps over an array's elements with nested mappings.

**Cost.** O(m · d) for *m* mappings of depth *d*, plus O(k) per `each` over *k* items. Every factor
is capped: depth ≤ 12, mappings ≤ 200, `each` nesting ≤ 3, items ≤ 1000, issues reported ≤ 50.

**Properties.** Pure and total: a property test feeds arbitrary JSON and checks it never raises and
always gives the same result. That determinism is what makes replay and dry-run validation
trustworthy. Parsed paths are memoised (bounded LRU); the cache is an optimisation only.

## 7. Keyset pagination — row-value comparison

**Problem.** `OFFSET` gets slower with depth and skips or repeats rows as new events arrive.

**Mechanism.** Order by `(received_at DESC, id DESC)`. The cursor encodes the last row's pair; the
next page is `WHERE (received_at, id) < (:ts, :id) LIMIT n+1`. The extra row tells whether another
page exists. The id breaks ties, so events sharing a timestamp are never lost.

**Cost.** O(log n + page) at any depth, served by a matching composite index.

**Stability.** *(`test_keyset_pagination_is_stable_while_new_events_arrive`,
`test_events_with_identical_timestamps_paginate_without_loss`)*. The cursor is opaque but not
secret: it is validated and only ever used as a bound inside an already workspace-scoped query.

## 8. Rate limiting — fixed window in the database

**Problem.** A limit kept in process memory is multiplied by the instance count and reset by every
cold start, which on a scale-to-zero platform means it limits nothing.

**Mechanism.** `INSERT … ON CONFLICT (scope_key, window_start) DO UPDATE SET count = count + 1
RETURNING count`. Allowed while `count ≤ limit`.

**Cost.** One row write per counted request. That is acceptable at this scale and is the price of
correctness without adding Redis; it would be the first thing to move to a dedicated store at
higher volume.

**Trade-off.** A fixed window admits up to 2× the limit across a window boundary. A sliding log
would be exact at higher cost; the simpler bound was judged sufficient and is stated rather than
hidden. **Concurrency.** The upsert is atomic under the row lock. **Restart.** State is in the
database. Old windows are purged by the dispatcher tick.

## 9. Idempotent mutations — unique keys checked under a lock

**Problem.** A retried or double-clicked request must not act twice.

| Operation | Key | Effect of a repeat |
|---|---|---|
| Ingest | `(integration_id, dedup_key)` | Reports `duplicate`, returns the original event id. |
| Schedule an attempt | `(event_id, cycle, attempt_number)` on the outbox | Re-arms the existing row. |
| Record an attempt | `(event_id, cycle, attempt_number)` on attempts | A redelivered message cannot create a second row. |
| Replay | `(workspace_id, idempotency_key)` | Returns the original operation with `created: false`. |

Replay additionally locks the event row before checking, so two replays with *different* keys are
serialised and exactly one succeeds. *(`test_concurrent_replays_with_different_keys_replay_once`)*

## 10. Lease-based failure detection

**Problem.** Telling "a worker is still delivering this" from "the worker died".

**Mechanism.** Claiming an attempt sets `lease = now + delivery_timeout + margin`. Because the
adapter enforces the timeout as a hard deadline, a healthy worker always finishes inside its lease.
An expired lease therefore means the attempt was abandoned, and the reaper closes it as
`worker_interrupted`.

**Cost.** A partial index over `PROCESSING` rows keeps the reaper's scan proportional to in-flight
work. **Bound.** Interrupted attempts consume the attempt budget, so a crash loop terminates.
