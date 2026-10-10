terraform {
  required_version = ">= 1.9"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.0"
    }
  }

  # State holds resource identifiers and must not live in the repository.
  # Configure a remote backend at init time, for example:
  #   terraform init -backend-config="bucket=<your-state-bucket>" -backend-config="prefix=relay/<environment>"
  backend "gcs" {}
}

provider "google" {
  project = var.project_id
  region  = var.region
}
