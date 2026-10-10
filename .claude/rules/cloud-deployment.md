---
paths:
  - "infra/**"
  - ".github/workflows/**"
  - "Dockerfile"
  - "docker-compose.yml"
  - "scripts/smoke-test.sh"
  - "docs/deployment/**"
---

# Cloud and deployment rules

## Do not touch live infrastructure without explicit permission

Never run, unless the user has clearly asked for that specific action in this conversation:

- `terraform apply`, `terraform destroy`, `terraform import`, or `terraform state` mutations;
- any `gcloud` command that creates, updates or deletes a resource, or changes billing or IAM;
- a deploy, a `git push` that triggers one, or a manual workflow dispatch.

Always safe: editing infrastructure code, `terraform fmt`, `terraform init -backend=false`,
`terraform validate`, linting workflows, running `scripts/smoke-test.sh` against localhost.

Never print account ids, state contents or secret values.

## Cost

- Target: $0/month at demo volume on standing free allowances. **Re-read the provider pricing
  pages before changing anything cost-relevant**, and update the table and the "checked on" date in
  `docs/deployment/free-tier-cost-model.md`. Do not quote allowances from memory.
- Keep: Cloud Run `min_instances = 0`, bounded `max_instances`, `cpu_idle = true`; no Pub/Sub topic
  retention; `retain_acked_messages = false`; Artifact Registry cleanup policies.
- The dispatcher schedule defaults to every 15 minutes because a more frequent poll keeps a
  scale-to-zero database awake past its free compute hours. Do not shorten it without updating the
  cost model.
- Do not add Cloud SQL, Memorystore, GKE, VMs, VPC connectors, NAT, load balancers, paid monitoring
  or GPUs without an ADR and a cost-viable plan.
- A budget alert is not a spending cap. Never describe it as one. Never describe the stack as
  "guaranteed free".

## Security

- Least privilege: one service account per workload; resource-level IAM bindings, not project-wide.
- The worker stays internal-ingress with no public invoker.
- GitHub → GCP via Workload Identity Federation restricted to this repository. No JSON keys.
- Secret values are added out of band; Terraform manages containers only.

## Delivery

- Build once, deploy by **digest**; production promotes the digest staging ran.
- Failed checks block deployment (the deploy workflow calls `ci.yml`).
- Production deploys are manual and gated by a protected environment.
- Migrations run before new code and must be backward compatible with the running revision.
- Keep rollback documented in `docs/deployment/runbook.md`, and keep GitHub Actions pinned by SHA.

## Honesty

Nothing has been deployed from this repository. Until it has, describe infrastructure as
"validated, not applied" and list cloud behaviour under unverified assumptions in
`docs/PROJECT_STATE.md`.
