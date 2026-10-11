# Deployment runbook

> **Status: not yet deployed.** The Terraform passes `terraform validate` and the workflows pass
> `actionlint`, but no cloud resource has been created. The verification jobs have run on GitHub;
> the build-and-push and release jobs have only ever been skipped. Expect to
> fix small things on the first real apply. Every step that creates, changes or destroys cloud
> resources is a deliberate manual action — nothing in this repository does it automatically.

## What gets created

| Where | Resources |
|---|---|
| Google Cloud | 2 Cloud Run services (API public, worker internal), 2 Pub/Sub topics and 2 push subscriptions, 1 Cloud Scheduler job, 1 Artifact Registry repository, 3 Secret Manager secrets, 4 service accounts, 1 Workload Identity pool and provider |
| Neon | 1 project (PostgreSQL) |
| Cloudflare | 1 Pages project with a Functions proxy |
| GitHub | Environments `staging` and `production`, repository variables and secrets |

Read [free-tier-cost-model.md](free-tier-cost-model.md) first. A billing account is required.

## First-time setup

### 1. Database (Neon)

Create a project and copy its connection string. Convert it to the SQLAlchemy form Relay expects:
`postgresql+psycopg://USER:PASSWORD@HOST/DB?sslmode=require`. Use the **pooled** host if offered.

### 2. Google Cloud bootstrap

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
# A bucket for Terraform state (state contains identifiers; keep it private).
gcloud storage buckets create gs://YOUR_PROJECT_ID-relay-tfstate --location=us-central1 --uniform-bucket-level-access
```

### 3. Terraform

```bash
cd infra/gcp
cp terraform.tfvars.example terraform.tfvars      # fill in; contains no secrets
terraform init -backend-config="bucket=YOUR_PROJECT_ID-relay-tfstate" -backend-config="prefix=relay/staging"
terraform plan -out plan.tfplan                    # read it
terraform apply plan.tfplan                        # creates resources
```

The first apply creates the Cloud Run services with the placeholder image digests from
`terraform.tfvars`, so they will not start until step 6 deploys real images. That is expected.
Alternatively build and push images first and put their digests in `terraform.tfvars`.

### 4. Secret values

Terraform creates empty secret containers. Add values out of band so they never enter Terraform
state, shell history or Git:

```bash
printf '%s' "$NEON_SQLALCHEMY_URL" | gcloud secrets versions add relay-staging-database-url --data-file=-
openssl rand -base64 36 | tr -d '\n' | gcloud secrets versions add relay-staging-demo-webhook --data-file=-
openssl rand -base64 36 | tr -d '\n' | gcloud secrets versions add relay-staging-demo-destination-token --data-file=-
```

### 5. GitHub configuration

Create environments `staging` and `production`. On `production`, add **required reviewers**.

| Kind | Name | Value |
|---|---|---|
| Variable | `GCP_PROJECT_ID`, `GCP_REGION` | project id, e.g. `us-central1` |
| Variable | `GCP_WORKLOAD_IDENTITY_PROVIDER` | `terraform output workload_identity_provider` |
| Variable | `GCP_DEPLOYER_SERVICE_ACCOUNT` | `terraform output deployer_service_account` |
| Variable | `CLOUDFLARE_PAGES_PROJECT`, `CLOUDFLARE_ACCOUNT_ID` | Pages project name, account id |
| Secret (per environment) | `DATABASE_URL` | the SQLAlchemy URL (used only to run migrations) |
| Secret | `CLOUDFLARE_API_TOKEN` | a token scoped to Pages: Edit |

No Google credential is stored in GitHub. The workflows exchange GitHub's OIDC token for
short-lived credentials through Workload Identity Federation.

### 6. First deploy

Push to `main`. The **Deploy (staging)** workflow runs every CI gate, builds both images once,
applies migrations, rolls the worker then the API, and runs `scripts/smoke-test.sh`. If the smoke
test fails it returns traffic to the previous revisions.

### 7. Web UI

In the Pages project settings set the environment variable `RELAY_API_ORIGIN` to
`terraform output api_url`. Set `web_origin` in `terraform.tfvars` to the Pages URL and apply
again so the API's CORS allowlist matches.

### 8. Seed the demo workspace

Run once from your machine, with passwords you choose (12+ characters), against the cloud database:

```bash
RELAY_DATABASE_URL="…neon url…" \
RELAY_SECRET_DEMO_WEBHOOK="$(gcloud secrets versions access latest --secret relay-staging-demo-webhook)" \
RELAY_SECRET_DEMO_DESTINATION_TOKEN="$(gcloud secrets versions access latest --secret relay-staging-demo-destination-token)" \
RELAY_SEED_ADMIN_EMAIL=you@example.com RELAY_SEED_ADMIN_PASSWORD='…' \
uv run python scripts/seed.py
```

## Routine release

1. Merge to `main` → staging deploys automatically after CI passes.
2. Check staging.
3. Run **Deploy (production)** manually, pasting the two image digest references printed in the
   staging run's summary. A reviewer approves. The same digests that ran in staging are deployed;
   nothing is rebuilt.

Production and staging here share one GCP project (resource names carry the environment suffix), so
production can pull the image staging pushed. With separate projects, grant the production
project's Cloud Run service agent `roles/artifactregistry.reader` on the staging repository.

## Rollback

**Code.** Cloud Run keeps previous revisions. Send traffic back:

```bash
gcloud run revisions list --service relay-api-production --region us-central1
gcloud run services update-traffic relay-api-production    --region us-central1 --to-revisions REVISION_NAME=100
gcloud run services update-traffic relay-worker-production --region us-central1 --to-revisions REVISION_NAME=100
```

The release workflow does this automatically when its smoke test fails.

**Schema.** Migrations are applied before new code and must be backward compatible with the
running revision (add first; remove in a later release). A code rollback therefore needs no schema
rollback. If a migration itself must be reverted: `uv run relay-migrate downgrade -1` against the
environment's database, after confirming no deployed revision depends on it.

**In-flight events.** Rolling back loses nothing: events and outbox rows are in the database, and
unacknowledged messages are redelivered to whichever revision is serving.

## Operating it

| Symptom | Look at | Likely cause |
|---|---|---|
| Events stuck in *Pending publish* | Dashboard "oldest unpublished queue message"; worker logs `outbox publish failed` | Pub/Sub permission or outage; scheduler not running |
| Events stuck in *Retry scheduled* past their time | Scheduler job history | Scheduler paused or failing auth (`401` in worker logs) |
| Events stuck in *Processing* | Worker logs `recovered interrupted delivery attempt` | Worker crashed mid-attempt; the lease reaper recovers them on the next tick |
| Many dead letters with *auth* | The connection's secret status in Settings | Destination credential rotated or secret not set |
| `readyz` returns 503 | Its `checks` body | Database unreachable, or migrations not applied |
| Worker returns 401 to Pub/Sub | Worker logs `OIDC token rejected` | Audience or invoker service account mismatch |

Find everything about one event with its correlation id:
`gcloud logging read 'jsonPayload.correlation_id="…"' --limit 100`.

**Rotating a secret:** add a new secret version, deploy a new revision (so instances read it),
confirm, then disable the old version so it stops counting toward the free allowance.

## Teardown

Removes every billable Google Cloud resource this stack created. **Destructive and irreversible**
— confirm the project and environment before running.

```bash
cd infra/gcp
terraform plan -destroy          # read what will be removed
# Production services have deletion protection; lift it deliberately first if you mean to remove them:
#   gcloud run services update relay-api-production --region us-central1 --no-deletion-protection
terraform destroy
```

Then, separately:

1. Delete the Terraform state bucket (`gcloud storage rm -r gs://YOUR_PROJECT_ID-relay-tfstate`).
2. Delete the Neon project in the Neon console.
3. Delete the Cloudflare Pages project.
4. Remove the GitHub environments, variables and secrets.
5. Check the billing report a few days later to confirm nothing is still accruing.

`google_project_service` resources are created with `disable_on_destroy = false`, so enabled APIs
stay enabled (they cost nothing by themselves).
