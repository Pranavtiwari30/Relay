locals {
  suffix = var.environment
  labels = {
    app         = "relay"
    environment = var.environment
    managed-by  = "terraform"
  }

  # The worker verifies this audience on every internal call. A fixed custom
  # audience avoids a dependency cycle on the worker's own URL.
  worker_audience = "relay-worker-${local.suffix}"

  secrets = {
    database-url           = "RELAY_DATABASE_URL"
    demo-webhook           = "RELAY_SECRET_DEMO_WEBHOOK"
    demo-destination-token = "RELAY_SECRET_DEMO_DESTINATION_TOKEN"
  }
}

data "google_project" "this" {}

# Only the APIs this architecture uses. Nothing here has a standing charge.
resource "google_project_service" "enabled" {
  for_each = toset([
    "run.googleapis.com",
    "pubsub.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudscheduler.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "sts.googleapis.com",
  ])

  service            = each.value
  disable_on_destroy = false
}

# --- container images --------------------------------------------------------

resource "google_artifact_registry_repository" "images" {
  repository_id = "relay-${local.suffix}"
  location      = var.region
  format        = "DOCKER"
  description   = "Relay API and worker images"
  labels        = local.labels

  # Storage beyond 0.5 GiB-month is billed, so old images are removed automatically.
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 5
    }
  }
  cleanup_policies {
    id     = "delete-older-than-14-days"
    action = "DELETE"
    condition {
      older_than = "1209600s"
    }
  }

  depends_on = [google_project_service.enabled]
}

# --- secrets -----------------------------------------------------------------
# Terraform creates the secret *containers* only. Values are added out of band
# (see docs/deployment/runbook.md) so they never enter Terraform state or Git.
# Three secrets with one active version each stay within the six free versions.

resource "google_secret_manager_secret" "relay" {
  for_each = local.secrets

  secret_id = "relay-${local.suffix}-${each.key}"
  labels    = local.labels

  replication {
    auto {}
  }

  depends_on = [google_project_service.enabled]
}
