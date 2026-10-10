output "api_url" {
  description = "Public URL of the API service (webhooks, control plane, demo destination)."
  value       = google_cloud_run_v2_service.api.uri
}

output "worker_url" {
  description = "Worker URL. Internal ingress only; not reachable from the internet."
  value       = google_cloud_run_v2_service.worker.uri
}

output "image_repository" {
  description = "Artifact Registry path to push images to."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "workload_identity_provider" {
  description = "Value for the GCP_WORKLOAD_IDENTITY_PROVIDER GitHub variable."
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "deployer_service_account" {
  description = "Value for the GCP_DEPLOYER_SERVICE_ACCOUNT GitHub variable."
  value       = google_service_account.deployer.email
}

output "secret_ids" {
  description = "Secret containers that need a value added before the first deploy."
  value       = { for key, secret in google_secret_manager_secret.relay : local.secrets[key] => secret.secret_id }
}
