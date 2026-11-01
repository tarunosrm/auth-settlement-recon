"""Generate settlement CSVs from the authgen ledger.

Reads data/auth_ledger.jsonl (ground truth: includes APPROVED tags and
reversal links), then produces one CSV per settlement day.

Design (ADR-002 parallel): the CSV carries NO anomaly column. Anomalies are
recorded in a local JSONL ledger (data/settle_ledger.jsonl) — the recon
engine must detect them from data alone.
"""
from __future__ import annotations

import csv
import json
import random
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from .models import SettlementRow

FIELDS = ["settlement_id", "auth_id", "rrn", "amount_minor", "currency",
          "fee_minor", "settlement_date", "scheme"]

# settlement currency can differ from auth currency (cross-border clearance)
SETTLE_CCY_BY_AUTH = {"USD": "USD", "EUR": "EUR", "GBP": "GBP"}


def load_approved_auths(ledger_path: Path) -> list[dict]:
    """Approved, non-reversed, non-late-response auths are settlement candidates."""
    auths: dict[str, dict] = {}
    reversed_ids: set[str] = set()
    with ledger_path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            tags = rec.get("ground_truth", [])
            if "REVERSAL" in tags and rec.get("original_auth_id"):
                reversed_ids.add(rec["original_auth_id"])
            if (
                rec.get("event_kind") == "AUTH"
                and "APPROVED" in tags
                and "LATE_RESPONSE_UNCERTAIN_SETTLEMENT" not in tags
            ):
                auths[rec["auth_id"]] = rec
    return [a for aid, a in auths.items() if aid not in reversed_ids]


def make_settlements(auths: list[dict], cfg: dict, rng: random.Random,
                     today: date | None = None) -> tuple[list[SettlementRow], list[dict]]:
    """Return (rows_for_csv, ledger_entries). One auth may yield 1..n rows."""
    today = today or date.today()
    a = cfg.get("anomalies", {})
    p_fx = a.get("fx_drift_rate", 0.02)
    p_mis = a.get("mismatch_rate", 0.01)
    p_miss = a.get("missing_rate", 0.02)
    p_late = a.get("late_rate", 0.02)
    p_partial = a.get("partial_rate", 0.01)
    rows, ledger = [], []
    for i, rec in enumerate(auths):
        auth_amt = rec["amount_minor"]
        settle_day = today - timedelta(days=1)
        anomaly = ""
        amt = auth_amt
        n_parts = 1

        r = rng.random()
        if r < p_miss:
            ledger.append({"auth_id": rec["auth_id"], "anomaly": "MISSING"})
            continue
        if rng.random() < p_late:
            settle_day = today - timedelta(days=5)
            anomaly = "LATE_T5"
        if rng.random() < p_partial:
            n_parts = 2
            anomaly = "PARTIAL"
        if anomaly == "" and rng.random() < p_mis:
            amt = auth_amt + rng.randrange(100, 2000) * rng.choice((1, -1))
            anomaly = "AMOUNT_MISMATCH"
        elif anomaly == "" and rng.random() < p_fx:
            amt = auth_amt + rng.randint(1, 5) * rng.choice((1, -1))
            anomaly = "FX_DRIFT"

        ccy = SETTLE_CCY_BY_AUTH.get(rec["currency"], rec["currency"])
        fee = max(10, int(auth_amt * 0.015))
        for part in range(n_parts):
            part_amt = amt // n_parts if n_parts > 1 else amt
            if part == 0 and n_parts > 1:
                part_amt = amt - (amt // n_parts)  # remainder on first row
            row = SettlementRow(
                settlement_id=f"s{rng.getrandbits(64):016x}",
                auth_id=rec["auth_id"], rrn=rec["rrn"],
                amount_minor=part_amt, currency=ccy, fee_minor=fee,
                settlement_date=settle_day.isoformat(),
                scheme=rec["card_scheme"], anomaly=anomaly,
            )
            rows.append(row)
            ledger.append({"settlement_id": row.settlement_id,
                           "auth_id": rec["auth_id"], "anomaly": anomaly,
                           "auth_amount": auth_amt, "part": part})
        # duplicate: same settlement retransmitted (only for single-row settles)
        if n_parts == 1 and rng.random() < a.get("duplicate_rate", 0.015):
            dup = replace(row, anomaly="DUPLICATE")
            rows.append(dup)
            ledger.append({"settlement_id": row.settlement_id,
                           "auth_id": rec["auth_id"], "anomaly": "DUPLICATE"})
    return rows, ledger


def write_files(rows: list[SettlementRow], out_dir: Path) -> list[Path]:
    """One CSV per settlement_date. Returns written paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    by_day: dict[str, list[SettlementRow]] = {}
    for r in rows:
        by_day.setdefault(r.settlement_date, []).append(r)
    paths = []
    for day, day_rows in sorted(by_day.items()):
        p = out_dir / f"settlements_{day}.csv"
        with p.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(FIELDS)
            for r in day_rows:
                w.writerow([getattr(r, fld) if fld != "amount_minor"
                            else r.amount_minor for fld in FIELDS])
        paths.append(p)
    return paths
