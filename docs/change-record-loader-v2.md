# Change record — settlements loader v2 (external review follow-up)

**Date:** 2026-11-03
**Trigger:** an external AI review (Gemini) of the Phase 2 batch load job
`scripts/load_settlements.py`. Five suggestions arrived. Four were adopted
(two of them corrected first), one premise was wrong, and the review itself
carried two bugs that are fixed here.
**Files touched:** `scripts/load_settlements.py`, `infra/gcp/settlements.tf`,
`tests/test_load_settlements.py`.
**Not touched:** `read_and_gate` (the in-file gate), the dry-run path, the
N-14 rule itself. The MERGE key stays `settlement_id`.

## 0. Verdicts at a glance

| # | Review said | Verdict | Section |
|---|---|---|---|
| 1 | Prune the MERGE by `settlement_date` | Adopted — but the review assumed a partitioned table; the table was NOT partitioned. Partitioning added first. | 1 |
| 2 | Scope the digest to touched partitions | Adopted — and the review's snippet kept an overflow bug (`SUM`). Fixed to `BIT_XOR`. | 2 |
| 3 | Load staging from the GCS URI | Adopted with a safety correction — loading the RAW file would double-insert duplicate rows. The curated copy feeds staging. | 3 |
| 4 | Env/flags instead of hardcoded ids | Adopted as-is. | 4 |
| 5 | Add `ingested_at` / `source_file` | Adopted with one refinement — INSERT only, never UPDATE. | 5 |

---

## 1. Partitioned target + pruned MERGE

**WHAT.** `recon.settlements` is now DAY-partitioned on `settlement_date`,
and the MERGE carries the batch's own dates in its ON clause.

**WHY.** v1 matched only on `settlement_id`, so every run scanned the whole
target table — forever, at any size. Partition pruning needs a constant
filter on the partition column; that filter did not exist because the table
was never partitioned. The review's premise ("your table is partitioned")
was wrong; the fix is a package: partition first, prune second.

**BEFORE** — `infra/gcp/settlements.tf` (no partitioning):

```hcl
resource "google_bigquery_table" "settlements" {
  # ... schema only, no time_partitioning block
}
```

**AFTER**:

```hcl
  time_partitioning {
    type  = "DAY"
    field = "settlement_date"
  }
```

**BEFORE** — loader v1:

```python
client.query(
    f"MERGE `{table_id(TABLE)}` T\n"
    f"USING `{table_id(STAGING)}` S\n"
    "ON T.settlement_id = S.settlement_id\n"
    "WHEN MATCHED THEN UPDATE SET\n"
    "  auth_id=S.auth_id, rrn=S.rrn, amount_minor=S.amount_minor,\n"
    "  currency=S.currency, fee_minor=S.fee_minor,\n"
    "  settlement_date=S.settlement_date, scheme=S.scheme\n"
    f"WHEN NOT MATCHED THEN INSERT ({cols})\n"
    f"VALUES ({', '.join('S.' + f for f in FIELDS)})"
).result()
```

**AFTER** — loader v2 (`merge_sql`):

```python
def merge_sql(target: str, staging: str, date_lits: list[str],
              source_file: str) -> str:
    dates = ", ".join(date_lits)
    cols = ", ".join(FIELDS + AUDIT_FIELDS)
    vals = ", ".join("S." + f for f in FIELDS)
    return (
        f"MERGE `{target}` T\n"
        f"USING `{staging}` S\n"
        # Partition pruning: the date list comes from the file itself, so the
        # target scan covers only the partitions this batch can touch. Safe
        # under the loader contract (one settlement_id => one settlement_date):
        # rows outside these partitions can never be this batch's own past.
        f"ON T.settlement_id = S.settlement_id\n"
        f"   AND T.settlement_date IN ({dates})\n"
        # settlement_date stays out of UPDATE SET: matched rows keep their
        # own date (equal by contract), so the partition column never moves.
        "WHEN MATCHED THEN UPDATE SET\n"
        "  auth_id=S.auth_id, rrn=S.rrn, amount_minor=S.amount_minor,\n"
        "  currency=S.currency, fee_minor=S.fee_minor, scheme=S.scheme\n"
        # Audit columns are first-write only. Updating them on a
        # retransmission would move the digest and break the no-op proof.
        f"WHEN NOT MATCHED THEN INSERT ({cols})\n"
        f"VALUES ({vals}, CURRENT_TIMESTAMP(), '{source_file}')"
    )
```

Two supporting changes travel with it:

- The `matched` count query got the **same** date predicate (`matched_sql`),
  so `inserts = len(rows) - matched` stays consistent with the pruned MERGE.
- Dates become SQL literals only through `date_literals()`: parsed with
  `date.fromisoformat`, re-serialized with `.isoformat()`. The SQL text is
  always plain `DATE 'YYYY-MM-DD'`, no matter what the CSV spelled.

**The contract this leans on:** one `settlement_id` maps to one
`settlement_date`, forever. True because settlegen writes one day per file
and the in-file gate rejects conflicting payloads. If that contract ever
breaks, drop the date filter from the ON clause and accept the full scan.

---

## 2. Digest: scoped + `BIT_XOR` instead of `SUM`

**WHAT.** The pre/post fingerprint now reads only the batch's partitions and
XORs the row fingerprints instead of summing them.

**WHY.** Two independent reasons.
(a) v1 scanned and hashed the whole table twice per run — cost grows with
history even when the batch touches one day.
(b) `SUM` of INT64 fingerprints is a latent crash: fingerprints are uniform
signed 64-bit values, and the running sum overflows INT64 quickly. Simulated
(sandbox, 2,000 trials each): overflow hits **80.2%** of tables at 50 rows,
**91.5%** at 200, **96.0%** at 1,000. BigQuery raises Integer Overflow —
the job dies. The review's snippet kept `SUM` unchanged; this is fix #1 the
review missed.

**BEFORE** — v1:

```python
def _digest(client) -> tuple[int, int]:
    row = next(iter(client.query(
        f"SELECT COUNT(*) AS c, "
        f"SUM(FARM_FINGERPRINT(TO_JSON_STRING(t))) AS fp "
        f"FROM `{table_id(TABLE)}` AS t"
    ).result()))
    return row["c"], row["fp"] or 0
```

**AFTER** — v2:

```python
def digest_sql(target: str, date_lits: list[str]) -> str:
    dates = ", ".join(date_lits)
    return (
        f"SELECT COUNT(*) AS c, "
        f"COALESCE(BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(t))), 0) AS fp "
        f"FROM `{target}` AS t "
        f"WHERE t.settlement_date IN ({dates})"
        # BIT_XOR, not SUM: SUM of uniform INT64 fingerprints overflows
        # INT64 within tens of rows (BigQuery: Integer Overflow error).
        # XOR cannot overflow, is order-independent, and flips if any row
        # changes. COALESCE pins the empty-partition case to 0.
    )
```

`COUNT` still catches cardinality changes; `BIT_XOR` flips if any row
changes. The no-op proof keeps its meaning: re-run the same file and both
numbers must hold still.

---

## 3. Staging loads from a curated GCS copy (`load_table_from_uri`)

**WHAT.** The raw file is archived to `inbound/` for provenance; the
**deduped** copy goes to `curated/`; staging is loaded from the curated URI,
server-side.

**WHY.** v1 re-uploaded the deduped payload through the client
(`load_table_from_file` on an in-memory buffer) — a second full pass of the
bytes through the loader. Loading from the GCS URI removes that pass and
leaves the exact source URI in the BigQuery job history.

**The correction to the review:** its snippet loads the **raw** GCS object.
That is unsafe here. The raw file contains byte-identical duplicate rows
(the DUPLICATE anomaly). Two identical source rows that match no target row
**both insert** — a silent double count in the recon target. Only the
post-gate copy may feed staging. This is fix #2 the review missed.

**BEFORE** — v1:

```python
blob_path = f"inbound/{date.today().isoformat()}/{args.csv.name}"
storage.Client(project=PROJECT).bucket(BUCKET).blob(blob_path) \
    .upload_from_filename(args.csv)
# ...
buf = io.StringIO()
csv.writer(buf, lineterminator="\n").writerow(FIELDS)
for r in rows:
    csv.writer(buf, lineterminator="\n").writerow([r[f] for f in FIELDS])
payload = buf.getvalue().encode("utf-8")

job = client.load_table_from_file(
    io.BytesIO(payload), table_id(STAGING),
    job_config=bigquery.LoadJobConfig(...))
```

**AFTER** — v2:

```python
stc = storage.Client(project=project)
raw_path = f"inbound/{day}/{args.csv.name}"
stc.bucket(bucket).blob(raw_path).upload_from_filename(args.csv)

curated_path = f"curated/{day}/{args.csv.name}"
stc.bucket(bucket).blob(curated_path).upload_from_string(
    canonical_payload(rows), content_type="text/csv; charset=utf-8")
curated_uri = f"gs://{bucket}/{curated_path}"

job = client.load_table_from_uri(
    curated_uri, staging,
    job_config=bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.CSV,
        skip_leading_rows=1,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        schema=schema,
    ))
```

`canonical_payload()` is the extracted v1 dedup-copy builder — same bytes as
before, now a named, tested function.

---

## 4. Environment from flags > env > defaults

**WHAT.** Project, dataset and bucket are no longer module constants.

**WHY.** Hardcoded ids pin the script to one environment. The review's
suggestion was adopted as-is, with the bucket default derived from the
resolved project so the naming convention survives a project switch.

**BEFORE** — v1:

```python
PROJECT = "privacyai-501020"
DATASET = "recon"
BUCKET = "privacyai-501020-settlements"

def table_id(name: str) -> str:
    return f"{PROJECT}.{DATASET}.{name}"
```

**AFTER** — v2:

```python
def resolve_settings(project, dataset, bucket,
                     env: dict[str, str]) -> tuple[str, str, str]:
    """flags > env > defaults. The bucket default follows the project."""
    p = project or env.get("GCP_PROJECT_ID") or DEFAULT_PROJECT
    d = dataset or env.get("BQ_DATASET") or DEFAULT_DATASET
    b = bucket or env.get("GCS_SETTLEMENT_BUCKET") or f"{p}-settlements"
    return p, d, b
```

Demo defaults are unchanged, so existing commands run as before.

---

## 5. Audit columns, written on INSERT only

**WHAT.** `recon.settlements` gains `ingested_at TIMESTAMP` and
`source_file STRING` (NULLABLE), populated at first insert.

**WHY.** Traceability: for an audit break you must be able to say which
file and when created a row. The refinement the review missed: these
columns must be **INSERT-only**. If the UPDATE branch refreshed
`ingested_at`, a retransmission would change table content — the digest
would move, and the "re-run changes nothing" proof dies.

**BEFORE** — v1 schema (8 CSV columns, no audit trail) and v1 INSERT of the
8 CSV fields only.

**AFTER** — tf:

```hcl
    { name = "ingested_at",     type = "TIMESTAMP", mode = "NULLABLE" },
    { name = "source_file",     type = "STRING",    mode = "NULLABLE" },
```

MERGE INSERT branch (see `merge_sql` above):

```sql
WHEN NOT MATCHED THEN INSERT (..., ingested_at, source_file)
VALUES (..., CURRENT_TIMESTAMP(), 'settlements_2026-10-31.csv')
```

The file name is validated before inlining
(`^[A-Za-z0-9._-]+$`, `safe_source_file`) — it is operator input, and it
goes into the SQL text.

---

## 6. Small hardening that travels with v2

- **Empty-batch guard.** `if not rows: return 0` after the gate. v2 builds
  `IN (...)` lists from the file's dates; an empty list would produce
  invalid SQL. v1 had no date lists, v1 needed no guard.
- **Staging comment fixed.** The tf note now states the real danger
  precisely: duplicate source rows that match *no* target row both insert —
  the case the curated copy prevents.

## Untouched by design

- `read_and_gate` — the three local gates, byte for byte.
- The dry-run path — still zero cloud calls, so tests need no GCP.
- `read_and_gate`'s exit-code discipline — any gate failure is
  `exit 1` with a `GATE FAIL` line.

## Verification evidence

| Check | Result |
|---|---|
| `SUM` overflow simulation (uniform INT64, 2,000 trials/size) | 50 rows: 80.2% · 200: 91.5% · 1,000: 96.0% — bug confirmed |
| Loader unit tests | 16 (7 kept + 9 new), all pass |
| Repo test suite (user run, 2026-11-03) | 40 passed |
| `ruff check .` | All checks passed |
| Dry-run on `settlements_2026-10-31.csv` | `rows=40 unique_ids=39 retransmitted=1` — the gate absorbed the file's DUPLICATE anomaly row |
| BigQuery facts checked against docs | `BIT_XOR` exists (aggregate); INT64 overflow raises an error; partition pruning via a filter in the merge condition is the documented pattern |

New tests pin the invariants so they cannot regress silently:
pruned ON clause · partition column never in UPDATE SET · audit columns
absent from UPDATE SET · digest uses `BIT_XOR`, never `SUM`, and carries the
WHERE scope · matched query shares the MERGE predicate · file name
validation · settings precedence.

## Ops note

Applying this change replaces `recon.settlements` once (partitioning cannot
be added in place). Expected plan line: `1 to add, 0 to change, 1 to
destroy`. The table rebuilds from the local CSVs with one loader run — that
is the point of the idempotent loader.
