# ==============================================================================
# SETTLEMENTS LANDING ZONE + TARGETS (settlements.tf) — Phase 2 (v2)
#
# Adds exactly three resources; main.tf is untouched:
#   1. GCS bucket        - where settlement CSVs land (the bank's SFTP drop)
#   2. recon.settlements - the MERGE target, keyed on settlement_id,
#                          DAY-partitioned on settlement_date
#   3. recon.settlements_staging - per-run load buffer (WRITE_TRUNCATE each run)
#
# Gate: a fresh plan must report "3 to add, 0 to change, 0 to destroy".
# If recon.settlements already exists from a pre-v2 apply, the new
# partitioning forces a ONE-TIME replacement: "1 to add, 0 to change,
# 1 to destroy". Expected and safe here - the loader rebuilds the table
# from the local CSVs in seconds. Anything beyond that one destroy: STOP.
# ==============================================================================

resource "google_storage_bucket" "settlements" {
  name          = "privacyai-501020-settlements"
  location      = var.gcp_region
  force_destroy = true # demo posture, same reasoning as deletion_protection=false

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  depends_on = [google_project_service.services["storage.googleapis.com"]]
}

resource "google_bigquery_table" "settlements" {
  dataset_id          = google_bigquery_dataset.recon.dataset_id
  table_id            = "settlements"
  deletion_protection = false # safe for demo environments

  # Schema == the settlegen CSV header exactly (src/settlegen/generator.py
  # FIELDS) plus two audit columns. The CSV carries no anomaly column by
  # design (ADR-002 parallel): anomalies live in data/settle_ledger.jsonl,
  # and Phase 3 detects them from the data itself.
  schema = jsonencode([
    { name = "settlement_id",   type = "STRING",    mode = "REQUIRED" },
    { name = "auth_id",         type = "STRING",    mode = "REQUIRED" },
    { name = "rrn",             type = "STRING",    mode = "REQUIRED" },
    { name = "amount_minor",    type = "INT64",     mode = "REQUIRED" },
    { name = "currency",        type = "STRING",    mode = "REQUIRED" },
    { name = "fee_minor",       type = "INT64",     mode = "REQUIRED" },
    { name = "settlement_date", type = "DATE",      mode = "REQUIRED" },
    { name = "scheme",          type = "STRING",    mode = "REQUIRED" },
    { name = "ingested_at",     type = "TIMESTAMP", mode = "NULLABLE" },
    { name = "source_file",     type = "STRING",    mode = "NULLABLE" },
  ])

  # Audit columns are NULLABLE and written by the loader on INSERT only.
  # A retransmission must not move them: re-running the same file has to
  # leave the table byte-identical, which is what the digest proves.

  # DAY partitions on settlement_date let the loader's MERGE and digest
  # touch only the partitions named in the incoming file (partition
  # pruning). require_partition_filter stays false on purpose: Phase 3
  # recon queries join by auth_id without date bounds and must stay simple.
  time_partitioning {
    type  = "DAY"
    field = "settlement_date"
  }

  depends_on = [google_project_service.services]
}

resource "google_bigquery_table" "settlements_staging" {
  dataset_id          = google_bigquery_dataset.recon.dataset_id
  table_id            = "settlements_staging"
  deletion_protection = false

  # Same 8-column schema as the CSV. The loader loads the DEDUPED copy here
  # (the curated/ GCS object), never the raw file: two identical source rows
  # that match no target row would BOTH insert - a silent double count.
  schema = jsonencode([
    { name = "settlement_id",   type = "STRING", mode = "REQUIRED" },
    { name = "auth_id",         type = "STRING", mode = "REQUIRED" },
    { name = "rrn",             type = "STRING", mode = "REQUIRED" },
    { name = "amount_minor",    type = "INT64",  mode = "REQUIRED" },
    { name = "currency",        type = "STRING", mode = "REQUIRED" },
    { name = "fee_minor",       type = "INT64",  mode = "REQUIRED" },
    { name = "settlement_date", type = "DATE",   mode = "REQUIRED" },
    { name = "scheme",          type = "STRING", mode = "REQUIRED" },
  ])
  depends_on = [google_project_service.services]
}
