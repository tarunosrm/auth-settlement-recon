# Verification record — Phase 2 commissioning (settlements landing)

**Date:** 2026-11-03 · **Project:** `privacyai-501020` · **Dataset:** `recon`
**Scope:** first end-to-end commissioning of the Phase 2 lane:
settlegen CSV → GCS → staging → partition-pruned MERGE → `recon.settlements`.
Every claim below is a quoted run output, not a description.

## Gate summary

| Gate | Result | Evidence |
|---|---|---|
| Lint | PASS | `ruff check .` → `All checks passed!` |
| Unit tests | PASS | `pytest -q` → `40 passed in 3.83s` |
| Cloud imports | PASS | `imports ok` |
| Local gates (no cloud) | PASS | dry-run accepted the file |
| Infra plan shape | PASS | `Plan: 3 to add, 0 to change, 0 to destroy` |
| First load | PASS | `inserted=39`, row arithmetic holds |
| Retransmission no-op | PASS | `inserted=0`, digest byte-identical |
| Audit columns | PASS | `ingested_at` + `source_file` populated, INSERT-only |

## T1 — Infra apply (fresh)

```
Plan: 3 to add, 0 to change, 0 to destroy.
Apply complete! Resources: 3 added, 0 changed, 0 destroyed.
```

Created:
- `google_storage_bucket.settlements` — `privacyai-501020-settlements`,
  ASIA-SOUTH1, `public_access_prevention=enforced`,
  `uniform_bucket_level_access=true`
- `google_bigquery_table.settlements` — 10-column schema (8 CSV + 2 audit),
  DAY partition on `settlement_date`
- `google_bigquery_table.settlements_staging` — 8-column CSV schema

The nine Phase-1 resources refreshed unchanged (project services, topic
`auth-events`, subscription `auth-events-sub`, dataset `recon`,
`auth_events`, `auth_dead_letter`).

## T2 — Local gates, zero cloud calls

```
file: settlements_2026-10-31.csv  sha256=8ea522afcdce9697...  rows=40  unique_ids=39  retransmitted=1
dry-run: local gates passed; no cloud calls made.
```

40 CSV rows carry 39 unique settlement ids: the file contains one
byte-identical retransmission (the generator's DUPLICATE anomaly). The gate
absorbed it and counted it — before any network call.

## T3 — First load (empty table → 39 rows)

```
upload: gs://privacyai-501020-settlements/inbound/2026-11-03/settlements_2026-10-31.csv (raw, provenance)
upload: gs://privacyai-501020-settlements/curated/2026-11-03/settlements_2026-10-31.csv (deduped, feeds staging)
staging: 39 rows
partitions in scope: DATE '2026-10-31'
merge: matched=0 updated=0 inserted=39 rows_in_scope_now=39
digest(pre):  count=0 fp=0
digest(post): count=39 fp=-4671242148151063469
```

- Row arithmetic holds: pre 0 + inserted 39 = post 39.
- Staging held 39, not 40 — only the deduped copy feeds the MERGE.
- `digest(pre) count=0 fp=0` shows the empty-partition case (COALESCE).

## T4 — Retransmission proof (same command, second run)

```
merge: matched=39 updated=39 inserted=0 rows_in_scope_now=39
digest(pre):  count=39 fp=-4671242148151063469
digest(post): count=39 fp=-4671242148151063469
```

This is the N-14 gate demonstrated on real infrastructure: the same file
delivered twice **changed nothing** — matched everything, inserted nothing,
digest fingerprint identical across runs. Exactly-once lives at the
destination.

## T5 — Audit columns (INSERT-only trail)

```
| settlement_id      | ingested_at         | source_file                |
| scbe476e11666ffc2  | 2026-11-03 15:40:42 | settlements_2026-10-31.csv |
| sc37ad9f77ff86cc7  | 2026-11-03 15:40:42 | settlements_2026-10-31.csv |
| s2262ed48b8c068cd  | 2026-11-03 15:40:42 | settlements_2026-10-31.csv |
| s5398952f604585c6  | 2026-11-03 15:40:42 | settlements_2026-10-31.csv |
| s9947cb3478f966d2  | 2026-11-03 15:40:42 | settlements_2026-10-31.csv |
```

5 of 39 rows shown. Every `ingested_at` equals the T3 insert time — the
second run (T4) touched no audit column. First-write audit trail confirmed.

The fingerprint `fp=-4671242148151063469` at `count=39` is this table's
commissioning digest — the value any future re-run must reproduce.

## Ops notes (recorded, not hidden)

| Incident | Resolution |
|---|---|
| GCS state read dropped mid-apply (`wsarecv ... forcibly closed by remote host`) | Transport-level, before any change; retried |
| Stale state lock left by the dropped run (HTTP 412, conditionNotMet) | Verified no terraform process live → `force-unlock` → apply succeeded |
| `settlements_old.tf` declared duplicate resources | File removed; backups belong to git history, not to folder copies |

## Sign-off

Phase 2 acceptance: **PASS on all gates.**
Lane closed: settlegen CSV → GCS (`inbound/` raw, `curated/` deduped) →
`recon.settlements_staging` (WRITE_TRUNCATE) → pruned MERGE on
`settlement_id` → `recon.settlements` (DAY-partitioned, audited,
idempotent).
