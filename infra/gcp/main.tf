terraform {
  required_version = ">= 1.6.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }

  backend "gcs" {
    bucket = "privacyai-501020-tfstate"
    prefix = "card-recon"
  }
}

provider "google" {
  project = var.gcp_project_id
  region  = var.gcp_region
}

variable "gcp_project_id" {
  type = string
}

variable "gcp_region" {
  type    = string
  default = "asia-south1"
}

resource "google_project_service" "services" {
  for_each = toset([
    "pubsub.googleapis.com",
    "bigquery.googleapis.com",
    "storage.googleapis.com",
    "dataflow.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

resource "google_pubsub_topic" "auth" {
  name       = "auth-events"
  depends_on = [google_project_service.services["pubsub.googleapis.com"]]
}

resource "google_pubsub_subscription" "auth" {
  name                       = "auth-events-sub"
  topic                      = google_pubsub_topic.auth.id
  message_retention_duration = "604800s"
  depends_on                 = [google_project_service.services["pubsub.googleapis.com"]]
}

resource "google_bigquery_dataset" "recon" {
  dataset_id = "recon"
  location   = var.gcp_region
  depends_on = [google_project_service.services["bigquery.googleapis.com"]]
}