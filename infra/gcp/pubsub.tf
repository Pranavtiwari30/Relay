# Delivery topic. No topic-level message retention: retained messages are
# billed for storage, and the outbox table is already the durable record.
resource "google_pubsub_topic" "deliveries" {
  name   = "relay-deliveries-${local.suffix}"
  labels = local.labels

  depends_on = [google_project_service.enabled]
}

resource "google_pubsub_topic" "dead_letter" {
  name   = "relay-deliveries-dead-letter-${local.suffix}"
  labels = local.labels

  depends_on = [google_project_service.enabled]
}

# Push subscription to the worker. Redelivery here covers *worker* failures
# (crash, timeout, 5xx). Destination retries are scheduled by the application.
resource "google_pubsub_subscription" "push" {
  name   = "relay-deliveries-push-${local.suffix}"
  topic  = google_pubsub_topic.deliveries.id
  labels = local.labels

  # Must exceed the longest delivery attempt (30s timeout) plus bookkeeping.
  ack_deadline_seconds = 60

  # Unacknowledged messages are stored free for up to one day; keep it there.
  message_retention_duration = "86400s"
  retain_acked_messages      = false

  expiration_policy {
    ttl = "" # never expire the subscription
  }

  push_config {
    push_endpoint = "${google_cloud_run_v2_service.worker.uri}/internal/pubsub/push"

    oidc_token {
      service_account_email = google_service_account.invoker.email
      audience              = local.worker_audience
    }
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }

  # Bounded: after 5 failed deliveries the message goes to the dead-letter
  # topic, whose subscriber marks the event DEAD_LETTERED. No infinite loops.
  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter.id
    max_delivery_attempts = 5
  }
}

resource "google_pubsub_subscription" "dead_letter_push" {
  name   = "relay-deliveries-dead-letter-push-${local.suffix}"
  topic  = google_pubsub_topic.dead_letter.id
  labels = local.labels

  ack_deadline_seconds       = 30
  message_retention_duration = "86400s"
  retain_acked_messages      = false

  expiration_policy {
    ttl = ""
  }

  push_config {
    push_endpoint = "${google_cloud_run_v2_service.worker.uri}/internal/pubsub/dead-letter"

    oidc_token {
      service_account_email = google_service_account.invoker.email
      audience              = local.worker_audience
    }
  }

  retry_policy {
    minimum_backoff = "30s"
    maximum_backoff = "600s"
  }
}
