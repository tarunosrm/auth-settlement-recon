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

resource "google_bigquery_table" "auth_events" {
  dataset_id          = google_bigquery_dataset.recon.dataset_id
  table_id            = "auth_events"
  deletion_protection = false   # safe for demo environments

  schema = jsonencode([
    { name = "auth_id",       type = "STRING",  mode = "REQUIRED" },
    { name = "event_kind",    type = "STRING" },
    { name = "rrn",           type = "STRING" },
    { name = "stan",          type = "STRING" },
    { name = "card_token",    type = "STRING" },
    { name = "card_scheme",   type = "STRING" },
    { name = "merchant_id",   type = "STRING" },
    { name = "merchant_name", type = "STRING" },
    { name = "mcc",           type = "STRING" },
    { name = "amount_minor",  type = "INT64" },
    { name = "currency",      type = "STRING" },
    { name = "response_code", type = "STRING" },
    { name = "timestamp_utc", type = "STRING" },
    { name = "ingest_time",   type = "TIMESTAMP" },
  ])
  depends_on = [google_project_service.services]
}

resource "google_bigquery_table" "auth_dead_letter" {
  dataset_id          = google_bigquery_dataset.recon.dataset_id
  table_id            = "auth_dead_letter"
  deletion_protection = false

  schema = jsonencode([
    { name = "raw_payload", type = "STRING" },
    { name = "error",       type = "STRING" },
    { name = "timestamp",   type = "TIMESTAMP" },
  ])
  depends_on = [google_project_service.services]
}