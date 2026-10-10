# Free-tier cost model

**Goal:** $0 per month at demonstration volume, using standing free allowances — not trial credits.

**This is a target under stated assumptions, not a guarantee.** Free tiers change, most Google
Cloud services require a billing account even at zero usage, and usage beyond an allowance is
billed. A budget alert notifies you; **it does not cap spending**.

**Nothing has been deployed.** The figures below are provider-published allowances and arithmetic
from them. No bill, usage report or measurement backs them yet.

## Sources

Every figure was read from the provider's own page on **2026-10-11**. Re-check before deploying.

| Service | Allowance as published | Source |
|---|---|---|
| **Cloud Run** (request-based billing) | 180,000 vCPU-seconds, 360,000 GiB-seconds and 2 million requests free per month. "Aggregated across projects by billing account"; applied at Tier 1 prices. Outbound data transfer billed separately, with 1 GiB free within North America per month. | https://cloud.google.com/run/pricing |
| **Pub/Sub** | First 10 GiB of throughput per month free per billing account, then $40/TiB. A minimum of 1 KB is assessed per request. Storage ($0.27/GiB-month) applies to topic retention, retained acknowledged messages, snapshots, and unacknowledged messages older than one day on subscriptions that retain longer than a day. | https://cloud.google.com/pubsub/pricing |
| **Cloud Scheduler** | 3 jobs per month free **per billing account** (not per project); $0.10 per additional job per 31 days. | https://cloud.google.com/scheduler/pricing |
| **Secret Manager** | 6 active secret versions, 10,000 access operations and 3 rotation notifications free per month per account. | https://cloud.google.com/secret-manager/pricing |
| **Artifact Registry** | 0.5 GiB-month storage free per account, then about $0.10/GiB-month. Transfer within the same location is free. | https://cloud.google.com/artifact-registry/pricing |
| **Cloud Logging** | First 50 GiB of log storage per project per month free; 30 days retention included. | https://cloud.google.com/stackdriver/pricing |
| **Neon** (Free plan) | $0/month: 100 compute-hours (CU-hours) per project, up to 2 CU, **compute suspends after 5 minutes of inactivity**, 1 GB storage per project. | https://neon.com/pricing |
| **Cloudflare Pages** (Free) | 500 builds per month, 1 concurrent build. Pages Functions requests are billed as Workers requests: 100,000 per day on the free plan, shared with Workers. | https://developers.cloudflare.com/pages/platform/limits/ · https://developers.cloudflare.com/pages/functions/pricing/ |
| **GitHub Actions** | Free for public repositories on standard runners. Private repositories on GitHub Free: 2,000 minutes and 500 MB artifact storage per month. | https://docs.github.com/en/billing/concepts/product-billing/github-actions |

Not read, so not relied on: Neon's network-transfer allowance, and Cloud Build pricing (Cloud Build
is not used; images are built in GitHub Actions).

## Assumed demonstration volume

Stated so the arithmetic can be redone with other numbers.

| Assumption | Value |
|---|---|
| Events ingested | 1,000 per month |
| Delivery attempts per event (average) | 1.5 |
| UI/API requests | 20,000 per month |
| Dispatcher ticks | every 15 minutes → 2,920 per month |
| Cloud Run instance size | 1 vCPU, 512 MiB, both services |
| Billed time per request | 0.2 s typical; 2 s for a slow delivery attempt |

## Estimated usage against each allowance

| Resource | Estimate | Allowance | Share used |
|---|---|---|---|
| Cloud Run requests | 1,000 webhooks + 1,500 pushes + 20,000 UI + 2,920 ticks ≈ **25,400** | 2,000,000 | ~1.3% |
| Cloud Run CPU | (22,000 × 0.2 s) + (1,500 × 2 s) + (2,920 × 0.5 s) ≈ **8,900 vCPU-s** | 180,000 | ~5% |
| Cloud Run memory | 8,900 s × 0.5 GiB ≈ **4,450 GiB-s** | 360,000 | ~1.2% |
| Pub/Sub throughput | 1,500 messages × 2 (publish + deliver) × 1 KB minimum ≈ **3 MB** | 10 GiB | <0.1% |
| Pub/Sub storage | no topic retention, no retained acked messages, subscription retention 1 day | — | $0 by configuration |
| Scheduler jobs | **1** | 3 per billing account | 33% |
| Secret versions | **3** (database URL, demo webhook secret, demo destination token) | 6 | 50% |
| Secret access operations | read at instance start only; tens per month | 10,000 | <1% |
| Artifact Registry storage | 2 images × ~5 retained ≈ **0.3–0.6 GiB** depending on image size | 0.5 GiB | **near the limit** |
| Neon compute | see next section — **~61 CU-hours** from the dispatcher alone | 100 | ~61% |
| Neon storage | 1,000 events × a few KB, plus history ≈ **tens of MB** | 1 GB | few % |
| Pages Functions | 20,000 proxied API requests per month | 100,000 **per day** | <1% |

Cold starts are billed as instance time and are included in spirit in the per-request figures;
they are not separately measured.

## The constraint that shaped the design: Neon compute hours

Neon bills compute by the time the database is awake, and on the free plan it only suspends after
**five idle minutes**. Any periodic database query shorter than that interval keeps it awake
permanently.

| Dispatcher schedule | Database awake | CU-hours/month at 0.25 CU | Within 100? |
|---|---|---|---|
| every minute | 100% (never idle 5 min) | ~182 | **No** |
| every 5 minutes | ~100% | ~182 | **No** |
| every 10 minutes | ≥ 50% | ~91 | Barely; no headroom |
| **every 15 minutes (default)** | ≥ 33% | **~61** | Yes, ~39 left for real use |
| every 30 minutes | ≥ 17% | ~30 | Yes |

The remaining ~39 CU-hours are about 156 hours of activity from webhooks and UI sessions.

**What this costs functionally:** in the cloud, a scheduled retry or a recovered publish can wait
up to 15 minutes past its due time. The first delivery attempt is not affected (ingestion publishes
immediately), and while messages are flowing the worker publishes due retries itself. If prompt
retries matter more than $0, set `dispatch_schedule = "* * * * *"` and use a database that is not
billed by active time.

## Where money could still be spent

| Risk | Why | Control |
|---|---|---|
| Traffic beyond the free tier | A public webhook URL can be called by anyone. | `max_instances = 2`; body-size limits; signature check before any write; per-connection rate limit. Cloud Run does not bill requests rejected by IAM, but the API is public by design. |
| Outbound data transfer | Billed separately from compute beyond 1 GiB/month in North America. | Payloads are small; responses are read to at most 64 KiB. |
| Image storage creeping past 0.5 GiB | Each deploy pushes two images. | Artifact Registry cleanup policy: keep the 5 most recent, delete older than 14 days. Slim base image. |
| Pub/Sub message storage | Retained messages are billed. | No topic retention; `retain_acked_messages = false`; 1-day subscription retention; bounded redelivery into a dead-letter topic. |
| Log volume | Unlikely at this scale. | One line per request plus one per delivery; payloads are never logged. |
| A second billing-account project using Scheduler | The three free jobs are shared across the billing account. | This stack uses one. |
| Secret versions accumulating | Each *active* version counts. | Disable or destroy the old version after rotating. |
| Hosted model inference | Per-request cost with any hosted provider. | Off by default (`ai_providers = ""`); the deterministic diagnosis is free. |
| Neon over its compute allowance | Compute is suspended for the rest of the month on the free plan. | The 15-minute schedule; watch usage in the Neon console. |

## Guardrails to set up at deploy time

1. A **budget with email alerts** at $1 and $5 on the billing account. This is a notification
   only. It will not stop spending.
2. Confirm `max_instances` and that `min_instances` is 0 on both services.
3. Confirm no other project on the billing account already uses the Scheduler or Secret Manager
   allowances.
4. Read the first week's billing report and compare it with the table above. Until that is done,
   treat these numbers as estimates.

## Local development costs nothing

`make setup && make dev` uses a local PostgreSQL, a PostgreSQL-backed queue and the built-in demo
destination. It needs no cloud account, no billing account and no model API key.

## Teardown

See [runbook.md](runbook.md#teardown). Destroying the Terraform stack removes every billable
Google Cloud resource it created; the Neon project, the Pages project and the Terraform state
bucket are removed separately.
