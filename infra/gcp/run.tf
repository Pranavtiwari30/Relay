locals {
  common_env = {
    RELAY_ENV                   = "cloud"
    RELAY_LOG_FORMAT            = "json"
    RELAY_QUEUE_BACKEND         = "pubsub"
    RELAY_PUBSUB_PROJECT_ID     = var.project_id
    RELAY_PUBSUB_TOPIC          = google_pubsub_topic.deliveries.name
    RELAY_INTERNAL_AUTH_MODE    = "oidc"
    RELAY_DB_POOL_SIZE          = "2" # small: serverless Postgres has few connections
    RELAY_SESSION_COOKIE_SECURE = "true"
    # The UI reaches the API through a same-origin proxy, so Lax is sufficient.
    RELAY_SESSION_COOKIE_SAMESITE = "lax"
    RELAY_CORS_ALLOWED_ORIGINS    = var.web_origin
    RELAY_AI_PROVIDERS            = var.ai_providers
  }
}

# --- API: public, scales to zero ---------------------------------------------

resource "google_cloud_run_v2_service" "api" {
  name                = "relay-api-${local.suffix}"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = var.environment == "production"
  labels              = local.labels

  template {
    service_account                  = google_service_account.api.email
    timeout                          = "60s"
    max_instance_request_concurrency = 40

    scaling {
      min_instance_count = 0 # no idle cost
      max_instance_count = var.max_instances
    }

    containers {
      image   = var.api_image
      command = ["uvicorn"]
      args    = ["relay_api.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle          = true # request-based billing: CPU only while serving
        startup_cpu_boost = false
      }

      dynamic "env" {
        for_each = merge(local.common_env, {
          RELAY_INTERNAL_OIDC_AUDIENCE = local.worker_audience
        })
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.secrets
        content {
          name = env.value
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.relay[env.key].secret_id
              version = "latest"
            }
          }
        }
      }

      startup_probe {
        http_get {
          path = "/healthz"
        }
        initial_delay_seconds = 1
        period_seconds        = 3
        failure_threshold     = 10
      }
    }
  }

  # Terraform creates the service; the deploy workflow rolls new image digests.
  # Ignoring the image here stops a later `terraform apply` from reverting a release.
  lifecycle {
    ignore_changes = [template[0].containers[0].image, client, client_version]
  }

  depends_on = [google_secret_manager_secret_iam_member.api]
}

# Webhooks and the UI's API calls arrive unauthenticated at the edge; the
# application authenticates them (HMAC signatures and sessions).
resource "google_cloud_run_v2_service_iam_member" "api_public" {
  name     = google_cloud_run_v2_service.api.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# --- Worker: not reachable from the internet -----------------------------------

resource "google_cloud_run_v2_service" "worker" {
  name     = "relay-worker-${local.suffix}"
  location = var.region
  # "Internal" still admits Pub/Sub push and Cloud Scheduler from this project.
  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  custom_audiences    = [local.worker_audience]
  deletion_protection = var.environment == "production"
  labels              = local.labels

  template {
    service_account = google_service_account.worker.email
    # Longer than one delivery attempt (<= 30s) and one dispatch cycle (<= 20s).
    timeout                          = "120s"
    max_instance_request_concurrency = 10

    scaling {
      min_instance_count = 0
      max_instance_count = var.max_instances
    }

    containers {
      image   = var.worker_image
      command = ["uvicorn"]
      args    = ["relay_worker.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8080"]

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle          = true
        startup_cpu_boost = false
      }

      dynamic "env" {
        for_each = merge(local.common_env, {
          RELAY_INTERNAL_OIDC_AUDIENCE         = local.worker_audience
          RELAY_INTERNAL_OIDC_SERVICE_ACCOUNTS = google_service_account.invoker.email
          RELAY_DEMO_DESTINATION_BASE_URL      = google_cloud_run_v2_service.api.uri
        })
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = {
          for key, name in local.secrets : key => name
          if contains(["database-url", "demo-destination-token"], key)
        }
        content {
          name = env.value
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.relay[env.key].secret_id
              version = "latest"
            }
          }
        }
      }

      startup_probe {
        http_get {
          path = "/healthz"
        }
        initial_delay_seconds = 1
        period_seconds        = 3
        failure_threshold     = 10
      }
    }
  }

  lifecycle {
    ignore_changes = [template[0].containers[0].image, client, client_version]
  }

  depends_on = [google_secret_manager_secret_iam_member.worker]
}

# Only the invoker identity may call the worker; there is no allUsers binding.
resource "google_cloud_run_v2_service_iam_member" "worker_invoker" {
  name     = google_cloud_run_v2_service.worker.name
  location = var.region
  role     = "roles/run.invoker"
  member   = google_service_account.invoker.member
}

# --- Dispatcher trigger -------------------------------------------------------

resource "google_cloud_scheduler_job" "dispatch" {
  count = var.enable_scheduler ? 1 : 0

  name             = "relay-dispatch-${local.suffix}"
  region           = var.region
  description      = "Publish due outbox messages and recover expired processing leases"
  schedule         = var.dispatch_schedule
  time_zone        = "Etc/UTC"
  attempt_deadline = "60s"

  retry_config {
    retry_count = 1
  }

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.worker.uri}/internal/outbox/dispatch"

    oidc_token {
      service_account_email = google_service_account.invoker.email
      audience              = local.worker_audience
    }
  }

  depends_on = [google_project_service.enabled]
}
