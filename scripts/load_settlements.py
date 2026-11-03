"""Load a settlegen CSV into BigQuery recon.settlements - idempotently.

Rule (debug-log N-14): exactly-once is a destination property. One MERGE on
settlement_id absorbs retransmissions; re-running the same file must change
nothing - the printed digest (count + fingerprint) must not move.

v2 changes (external review follow-up; each maps to one review point):
  1. recon.settlements is DAY-partitioned on settlement_date
     (infra/gcp/settlements.tf) and the MERGE carries the batch's dates in
     its ON clause, so the target scan touches only the partitions the file
     names. The contract that makes this safe: one settlement_id maps to
     one settlement_date forever (per-day generation + the in-file gate
     below). If that contract ever breaks, re-point the MERGE at the full
     key and accept the full scan.
  2. The digest is scoped to the same dates and uses BIT_XOR, not SUM.
     SUM of uniform INT64 fingerprints overflows within tens of rows
     (BigQuery raises an Integer Overflow error). XOR cannot overflow and
     is just as change-sensitive; COALESCE keeps the empty case at 0.
  3. Staging loads with load_table_from_uri from a DEDUPED copy in GCS
     (curated/...). Loading the raw file would carry exact duplicate rows
     into the MERGE: two identical source rows that match no target row
     BOTH insert - a silent double count. The raw copy still lands in
     inbound/ as provenance (what arrived, byte for byte).
  4. project / dataset / bucket: flags > env > defaults, so the same
     script runs against another environment without edits.
  5. ingested_at + source_file audit columns, written on INSERT only.
     A retransmission must not move them - or the digest would move too.

Duplicate handling: a retransmitted settlement arrives as a second CSV row
with the SAME settlement_id and a byte-identical payload (the anomaly tag
lives only in data/settle_ledger.jsonl, never in the CSV - ADR-002
parallel). Identical => absorb it and count it. Differing payload under one
settlement_id => hard stop, nothing loads.

Pipeline: gates on the local file -> upload raw to gs://<bucket>/inbound/ ->
upload deduped copy to gs://<bucket>/curated/ -> load staging from the
curated URI -> one pruned MERGE -> verify the row arithmetic -> print the
digest.

Usage:
  python scripts/load_settlements.py --csv data/settlements/settlements_2026-10-31.csv
  python scripts/load_settlements.py --csv ... --dry-run   # local gates only

  flags: --project --dataset --bucket
  env:   GCP_PROJECT_ID, BQ_DATASET, GCS_SETTLEMENT_BUCKET
         (unset flags and env keep the demo defaults)

Auth: Application Default Credentials (gcloud auth application-default login).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import os
import re
import sys
from datetime import date
from pathlib import Path

FIELDS = ["settlement_id", "auth_id", "rrn", "amount_minor", "currency",
          "fee_minor", "settlement_date", "scheme"]
INT_FIELDS = ("amount_minor", "fee_minor")
# Destination-generated audit columns. Never carried by the CSV: the file
# does not get to claim when or from where it was ingested.
AUDIT_FIELDS = ["ingested_at", "source_file"]
DEFAULT_PROJECT = "privacyai-501020"
DEFAULT_DATASET = "recon"
TABLE = "settlements"
STAGING = "settlements_staging"
SAFE_FILE_RE = re.compile(r"[A-Za-z0-9._-]+")

SCHEMA_SPEC = [
    ("settlement_id", "STRING"),
    ("auth_id", "STRING"),
    ("rrn", "STRING"),
    ("amount_minor", "INT64"),
    ("currency", "STRING"),
    ("fee_minor", "INT64"),
    ("settlement_date", "DATE"),
    ("scheme", "STRING"),
]


def die(msg: str) -> None:
    print(f"GATE FAIL - {msg}", file=sys.stderr)
    sys.exit(1)


def table_id(project: str, dataset: str, name: str) -> str:
    return f"{project}.{dataset}.{name}"


def resolve_settings(project: str | None, dataset: str | None,
                     bucket: str | None, env: dict[str, str]) -> tuple[str, str, str]:
    """flags > env > defaults. The bucket default follows the project."""
    p = project or env.get("GCP_PROJECT_ID") or DEFAULT_PROJECT
    d = dataset or env.get("BQ_DATASET") or DEFAULT_DATASET
    b = bucket or env.get("GCS_SETTLEMENT_BUCKET") or f"{p}-settlements"
    return p, d, b


def read_and_gate(path: Path) -> tuple[list[dict], int]:
    """Local gates 1-3. Returns (unique_rows, retransmitted_row_count)."""
    if not path.exists():
        die(f"file not found: {path}")
    text = path.read_bytes().decode("utf-8")  # decode error = transport damage
    if not text.splitlines() or text.splitlines()[0].split(",") != FIELDS:
        die(f"header mismatch: want {FIELDS}")
    seen: dict[str, dict] = {}
    retrans = 0
    for i, row in enumerate(csv.DictReader(io.StringIO(text)), start=2):
        for fld in INT_FIELDS:
            try:
                row[fld] = int(row[fld])
            except (TypeError, ValueError):
                die(f"line {i}: {fld} is not an integer: {row[fld]!r}")
        try:
            date.fromisoformat(row["settlement_date"])
        except ValueError:
            die(f"line {i}: settlement_date not ISO YYYY-MM-DD: "
                f"{row['settlement_date']!r}")
        if not row["settlement_id"] or not row["auth_id"]:
            die(f"line {i}: empty settlement_id or auth_id")
        prev = seen.get(row["settlement_id"])
        if prev is None:
            seen[row["settlement_id"]] = row
        elif prev == row:
            retrans += 1  # byte-identical retransmission: absorb, count
        else:
            die(f"line {i}: settlement_id {row['settlement_id']} reappears "
                "with a DIFFERENT payload - corrupt file, refusing to load")
    return list(seen.values()), retrans


def canonical_payload(rows: list[dict]) -> bytes:
    """The deduped copy as CSV bytes. This - never the raw file - feeds
    staging, so the MERGE can never see two identical source rows."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(FIELDS)
    for r in rows:
        writer.writerow([r[f] for f in FIELDS])
    return buf.getvalue().encode("utf-8")


def date_literals(rows: list[dict]) -> list[str]:
    """Canonical DATE literals for the partitions this batch can touch.

    Dates are re-serialized from the parsed date object, so the SQL text is
    always plain YYYY-MM-DD no matter what spelling the CSV used.
    """
    days = sorted({date.fromisoformat(r["settlement_date"]) for r in rows})
    return [f"DATE '{d.isoformat()}'" for d in days]


def safe_source_file(name: str) -> str:
    """Inline literal hygiene: the file name goes into the MERGE text, so it
    must be plain. Generator names are settlements_YYYY-MM-DD.csv - anything
    outside [A-Za-z0-9._-] is a foreign file and gets stopped here."""
    if not SAFE_FILE_RE.fullmatch(name):
        die(f"file name has characters outside [A-Za-z0-9._-]: {name!r}")
    return name


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


def matched_sql(target: str, staging: str, date_lits: list[str]) -> str:
    dates = ", ".join(date_lits)
    return (
        f"SELECT COUNT(*) AS c\n"
        f"FROM `{target}` T JOIN `{staging}` S USING (settlement_id)\n"
        # Same predicate as the MERGE ON clause, so
        # inserts = len(rows) - matched stays consistent with a pruned MERGE.
        f"WHERE T.settlement_date IN ({dates})"
    )


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


def _digest(client, target: str, date_lits: list[str]) -> tuple[int, int]:
    row = next(iter(client.query(digest_sql(target, date_lits)).result()))
    return row["c"], row["fp"]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Load settlegen CSV into recon.settlements (idempotent).")
    ap.add_argument("--csv", required=True, type=Path)
    ap.add_argument("--dry-run", action="store_true",
                    help="run the local gates only; no cloud calls")
    ap.add_argument("--project", default=None)
    ap.add_argument("--dataset", default=None)
    ap.add_argument("--bucket", default=None)
    args = ap.parse_args()

    rows, retrans = read_and_gate(args.csv)
    if not rows:
        print("no data rows after gating - nothing to load.")
        return 0
    sha = hashlib.sha256(args.csv.read_bytes()).hexdigest()
    print(f"file: {args.csv.name}  sha256={sha[:16]}...  "
          f"rows={len(rows) + retrans}  unique_ids={len(rows)}  "
          f"retransmitted={retrans}")
    if args.dry_run:
        print("dry-run: local gates passed; no cloud calls made.")
        return 0
    if retrans:
        print(f"note: {retrans} retransmitted row(s) will be absorbed by the "
              "MERGE (same settlement_id, identical payload)")

    from google.cloud import bigquery, storage  # deferred: dry-run needs none

    project, dataset, bucket = resolve_settings(
        args.project, args.dataset, args.bucket, os.environ)
    target = table_id(project, dataset, TABLE)
    staging = table_id(project, dataset, STAGING)
    source_file = safe_source_file(args.csv.name)
    day = date.today().isoformat()

    stc = storage.Client(project=project)
    raw_path = f"inbound/{day}/{args.csv.name}"
    stc.bucket(bucket).blob(raw_path).upload_from_filename(args.csv)
    print(f"upload: gs://{bucket}/{raw_path} (raw, provenance)")

    curated_path = f"curated/{day}/{args.csv.name}"
    stc.bucket(bucket).blob(curated_path).upload_from_string(
        canonical_payload(rows), content_type="text/csv; charset=utf-8")
    curated_uri = f"gs://{bucket}/{curated_path}"
    print(f"upload: {curated_uri} (deduped, feeds staging)")

    client = bigquery.Client(project=project)
    schema = [bigquery.SchemaField(n, t, mode="REQUIRED")
              for n, t in SCHEMA_SPEC]
    job = client.load_table_from_uri(
        curated_uri, staging,
        job_config=bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.CSV,
            skip_leading_rows=1,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            schema=schema,
        ))
    job.result()  # raises on any load error - loud, nothing merged
    print(f"staging: {job.output_rows} rows")

    date_lits = date_literals(rows)
    print(f"partitions in scope: {', '.join(date_lits)}")
    pre_c, pre_fp = _digest(client, target, date_lits)
    matched = next(iter(client.query(
        matched_sql(target, staging, date_lits)).result()))["c"]

    client.query(merge_sql(target, staging, date_lits, source_file)).result()

    post_c, post_fp = _digest(client, target, date_lits)
    inserts = len(rows) - matched
    if post_c != pre_c + inserts:
        die(f"row arithmetic broken: pre={pre_c} inserted={inserts} "
            f"post={post_c} - investigate before trusting the table")
    print(f"merge: matched={matched} updated={matched} inserted={inserts} "
          f"rows_in_scope_now={post_c}")
    print(f"digest(pre):  count={pre_c} fp={pre_fp}")
    print(f"digest(post): count={post_c} fp={post_fp}")
    print("idempotency check: re-run the same command - inserted must be 0 "
          "and digest(post) must equal digest(pre).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
