# One identity per workload, each with only what it needs.

resource "google_service_account" "api" {
  account_id   = "relay-api-${local.suffix}"
  display_name = "Relay API runtime (${var.environment})"
}

resource "google_service_account" "worker" {
  account_id   = "relay-worker-${local.suffix}"
  display_name = "Relay worker runtime (${var.environment})"
}

# The only identity allowed to call the worker. Pub/Sub push and Cloud
# Scheduler present OIDC tokens minted for it.
resource "google_service_account" "invoker" {
  account_id   = "relay-invoker-${local.suffix}"
  display_name = "Relay worker invoker (${var.environment})"
}

resource "google_service_account" "deployer" {
  account_id   = "relay-deployer-${local.suffix}"
  display_name = "Relay CI deployer (${var.environment})"
}

# Runtime identities publish to the delivery topic and nothing else in Pub/Sub.
resource "google_pubsub_topic_iam_member" "publishers" {
  for_each = {
    api    = google_service_account.api.member
    worker = google_service_account.worker.member
  }

  topic  = google_pubsub_topic.deliveries.name
  role   = "roles/pubsub.publisher"
  member = each.value
}

# Each runtime reads only the secrets it is given.
resource "google_secret_manager_secret_iam_member" "api" {
  for_each = google_secret_manager_secret.relay

  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.api.member
}

resource "google_secret_manager_secret_iam_member" "worker" {
  for_each = {
    for key, secret in google_secret_manager_secret.relay : key => secret
    if contains(["database-url", "demo-destination-token"], key)
  }

  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.worker.member
}

# Dead-lettering is performed by the Pub/Sub service agent: it must be able to
# publish to the dead-letter topic and acknowledge on the source subscription.
locals {
  pubsub_agent = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_pubsub_topic_iam_member" "dead_letter_publisher" {
  topic  = google_pubsub_topic.dead_letter.name
  role   = "roles/pubsub.publisher"
  member = local.pubsub_agent
}

resource "google_pubsub_subscription_iam_member" "dead_letter_subscriber" {
  subscription = google_pubsub_subscription.push.name
  role         = "roles/pubsub.subscriber"
  member       = local.pubsub_agent
}

# The Pub/Sub service agent mints OIDC tokens as the invoker for push delivery.
resource "google_service_account_iam_member" "pubsub_token_creator" {
  service_account_id = google_service_account.invoker.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = local.pubsub_agent
}

# --- GitHub Actions deploys with short-lived credentials (no JSON keys) --------

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "relay-github-${local.suffix}"
  display_name              = "Relay GitHub Actions (${var.environment})"

  depends_on = [google_project_service.enabled]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub OIDC"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }

  # Tokens from any other repository are rejected at the pool boundary.
  attribute_condition = "assertion.repository == \"${var.github_repository}\""

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "github_impersonates_deployer" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repository}"
}

resource "google_artifact_registry_repository_iam_member" "deployer_pushes_images" {
  repository = google_artifact_registry_repository.images.name
  location   = var.region
  role       = "roles/artifactregistry.writer"
  member     = google_service_account.deployer.member
}

# Deploy new revisions of the two services, and nothing broader.
resource "google_cloud_run_v2_service_iam_member" "deployer_api" {
  name     = google_cloud_run_v2_service.api.name
  location = var.region
  role     = "roles/run.developer"
  member   = google_service_account.deployer.member
}

resource "google_cloud_run_v2_service_iam_member" "deployer_worker" {
  name     = google_cloud_run_v2_service.worker.name
  location = var.region
  role     = "roles/run.developer"
  member   = google_service_account.deployer.member
}

# Deploying a revision that runs as a service account requires actAs on it.
resource "google_service_account_iam_member" "deployer_acts_as_runtime" {
  for_each = {
    api    = google_service_account.api.name
    worker = google_service_account.worker.name
  }

  service_account_id = each.value
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.deployer.member
}
