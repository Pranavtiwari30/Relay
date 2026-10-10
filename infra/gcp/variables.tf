variable "project_id" {
  description = "GCP project ID. Billing must be enabled on it even when usage stays inside free allowances."
  type        = string
}

variable "region" {
  description = "Region for every resource. Cloud Run's free tier is applied at Tier 1 prices; us-central1 is Tier 1."
  type        = string
  default     = "us-central1"
}

variable "environment" {
  description = "Deployment environment name; used as a suffix on resource names."
  type        = string

  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment must be 'staging' or 'production'."
  }
}

variable "api_image" {
  description = "API container image pinned by digest (…@sha256:…). The same digest is promoted between environments."
  type        = string

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.api_image))
    error_message = "api_image must be pinned by digest (…@sha256:<64 hex>), not by tag."
  }
}

variable "worker_image" {
  description = "Worker container image pinned by digest (…@sha256:…)."
  type        = string

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.worker_image))
    error_message = "worker_image must be pinned by digest (…@sha256:<64 hex>), not by tag."
  }
}

variable "web_origin" {
  description = "Exact origin of the web UI (for example https://relay.pages.dev). Used for the CORS allowlist and CSRF origin check."
  type        = string

  validation {
    condition     = can(regex("^https://[^/*]+$", var.web_origin))
    error_message = "web_origin must be a single https origin with no path and no wildcard."
  }
}

variable "github_repository" {
  description = "GitHub repository allowed to deploy through Workload Identity Federation, as 'owner/name'."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "github_repository must look like 'owner/name'."
  }
}

variable "max_instances" {
  description = "Upper bound on instances per Cloud Run service. A cost and blast-radius ceiling, not a performance target."
  type        = number
  default     = 2

  validation {
    condition     = var.max_instances >= 1 && var.max_instances <= 5
    error_message = "max_instances must be between 1 and 5."
  }
}

variable "dispatch_schedule" {
  description = <<-EOT
    Cron schedule for the outbox dispatcher (retries, publish recovery, lease recovery).
    The default of every 15 minutes is deliberate: a serverless Postgres that suspends
    after 5 idle minutes (Neon's free plan) is kept awake all month by a 1-minute poll,
    which exceeds its free compute hours. Use "* * * * *" only with a database that is
    not billed by active time. See docs/deployment/free-tier-cost-model.md.
  EOT
  type        = string
  default     = "*/15 * * * *"
}

variable "enable_scheduler" {
  description = "Create the single Cloud Scheduler job. Three jobs per billing account are free; this uses one."
  type        = bool
  default     = true
}

variable "ai_providers" {
  description = "Ordered model providers for investigations. Empty (default) means the deterministic diagnosis only, at no cost."
  type        = string
  default     = ""
}
