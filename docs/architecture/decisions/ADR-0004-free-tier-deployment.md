# ADR-0004: Deployment target and the cost constraints that shape it

- **Status:** Accepted
- **Date:** 2026-10-11

## Context

The deployment should cost $0 per month at demonstration volume using published free allowances
(not trial credits), while still being real managed infrastructure. Free allowances were read from
the providers' pricing pages on 2026-10-11; figures and sources are in
`docs/deployment/free-tier-cost-model.md`.

## Decision

| Concern | Choice | Why |
|---|---|---|
| Compute | Cloud Run, request-based billing, `min_instances = 0` | No idle cost; free tier covers demo volume. |
| Queue | Pub/Sub push subscription + dead-letter topic | Managed, push fits scale-to-zero, 10 GiB/month free. |
| Database | Neon (serverless PostgreSQL) | Cloud SQL has no free tier and a standing cost. |
| Web UI | Cloudflare Pages, with a Pages Function proxying `/api` | Free static hosting; same-origin cookies. |
| Secrets | Secret Manager, injected as environment variables | Three secrets fit the six free versions. |
| Dispatcher trigger | One Cloud Scheduler job | Three jobs per billing account are free. |
| CI/CD | GitHub Actions with OIDC / Workload Identity Federation | No long-lived keys. |

Explicitly **not** used: Cloud SQL, Memorystore/Redis, GKE, VMs, VPC connectors, NAT, load
balancers, paid monitoring, GPUs.

### The dispatcher schedule is every 15 minutes, on purpose

Neon's free plan allows 100 compute-hours per project per month and suspends compute after five
idle minutes. A dispatcher that queries the database every minute never lets it suspend: about 730
hours at the 0.25 CU minimum is roughly 182 CU-hours, well over the allowance. At 15 minutes the
database is awake at most a third of the time (about 61 CU-hours), leaving headroom for real use.

## Alternatives considered

- **Every minute, on a paid database.** Fine technically; not $0.
- **A VM running everything.** Cheap, but not managed, not scale-to-zero, and it removes the
  cloud-operations content this project is meant to demonstrate.
- **Kubernetes.** A standing control-plane cost and operational weight with no benefit here.

## Consequences

- In the cloud, a retry or a recovered publish can wait up to 15 minutes beyond its computed time.
  First delivery is unaffected (ingestion publishes immediately), and the worker dispatches due
  messages whenever traffic is flowing. Locally the loop runs every second.
- Cold starts add latency to the first request after idle.
- A billing account is required even at $0 of usage, and a budget alert is a notification, not a
  cap. Costs can still occur beyond the allowances (traffic, egress, retained messages, image
  storage).
- **Nothing has been deployed.** The Terraform validates and the workflows lint, but no resource
  has been created; cloud behaviour is unverified until someone with credentials applies it.
