import json
import random

from settlegen.generator import load_approved_auths, make_settlements, write_files
from settlegen.models import SettlementRow

LEDGER = [
    {"event_kind": "AUTH", "auth_id": "a1", "rrn": "r1", "amount_minor": 1000,
     "currency": "EUR", "card_scheme": "VISA", "ground_truth": ["APPROVED"]},
    {"event_kind": "AUTH", "auth_id": "a2", "rrn": "r2", "amount_minor": 2000,
     "currency": "EUR", "card_scheme": "VISA", "ground_truth": ["DECLINED"]},
    {"event_kind": "AUTH", "auth_id": "a3", "rrn": "r3", "amount_minor": 3000,
     "currency": "USD", "card_scheme": "AMEX",
     "ground_truth": ["APPROVED", "LATE_RESPONSE_UNCERTAIN_SETTLEMENT"]},
    {"event_kind": "REVERSAL", "auth_id": "a9", "rrn": "r9",
     "original_auth_id": "a1", "ground_truth": ["REVERSAL"]},
]


def test_load_excludes_declined_late_and_reversed(tmp_path):
    p = tmp_path / "led.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in LEDGER), encoding="utf-8")
    auths = load_approved_auths(p)
    ids = {a["auth_id"] for a in auths}
    assert "a2" not in ids          # declined
    assert "a3" not in ids          # late response: settlement uncertain
    assert "a1" not in ids          # reversed -> must never settle
    assert ids == set()


CFG = {"anomalies": {"fx_drift_rate": 0.0, "mismatch_rate": 0.0,
                     "missing_rate": 0.0, "late_rate": 0.0,
                     "partial_rate": 0.0, "duplicate_rate": 0.0}}


def test_zero_anomaly_rates_produce_exact_matches():
    auths = [{"auth_id": "a1", "rrn": "r1", "amount_minor": 1000,
              "currency": "EUR", "card_scheme": "VISA"}]
    rows, _entries  = make_settlements(auths, CFG, random.Random(1))
    assert len(rows) == 1
    r = rows[0]
    assert r.amount_minor == 1000 and r.anomaly == ""
    assert r.currency == "EUR" and r.fee_minor == 15


def test_money_is_int_and_ids_unique():
    auths = [{"auth_id": f"a{i}", "rrn": f"r{i}", "amount_minor": 1000 + i,
              "currency": "EUR", "card_scheme": "VISA"} for i in range(60)]
    rows, _ = make_settlements(auths, CFG, random.Random(2))
    assert all(isinstance(r.amount_minor, int) for r in rows)
    assert len({r.settlement_id for r in rows}) == len(rows)


def test_high_anomaly_rates_produce_all_classes():
    cfg = {"anomalies": {"fx_drift_rate": 0.3, "mismatch_rate": 0.3,
                         "missing_rate": 0.2, "late_rate": 0.3,
                         "partial_rate": 0.3, "duplicate_rate": 0.3}}
    auths = [{"auth_id": f"a{i}", "rrn": f"r{i}", "amount_minor": 5000,
              "currency": "EUR", "card_scheme": "VISA"} for i in range(400)]
    _rows, entries = make_settlements(auths, cfg, random.Random(3))
    tags = {e["anomaly"] for e in entries}
    assert {"FX_DRIFT", "AMOUNT_MISMATCH", "MISSING", "LATE_T5",
            "PARTIAL", "DUPLICATE"} <= tags
    # FX drift stays within tolerance band
    for e in entries:
        if e["anomaly"] == "FX_DRIFT":
            assert 1 <= abs(e["settlement_row_amount"] - e["auth_amount"]) <= 5                 if "settlement_row_amount" in e else True


def test_write_files_splits_by_day(tmp_path):
    d1 = SettlementRow("s1", "a1", "r1", 100, "EUR", 10, "2026-01-02", "VISA", "")
    d2 = SettlementRow("s2", "a2", "r2", 200, "EUR", 20, "2026-01-06", "VISA", "")
    paths = write_files([d1, d2], tmp_path)
    assert len(paths) == 2
    text = paths[0].read_text(encoding="utf-8")
    assert "settlement_id,auth_id" in text


def test_duplicate_is_exact_copy_with_tag():
    """Duplicate injection must produce an identical row (slots-safe path)."""
    cfg = {"anomalies": {"fx_drift_rate": 0.0, "mismatch_rate": 0.0,
                         "missing_rate": 0.0, "late_rate": 0.0,
                         "partial_rate": 0.0, "duplicate_rate": 1.0}}
    auths = [{"auth_id": "a1", "rrn": "r1", "amount_minor": 1000,
              "currency": "EUR", "card_scheme": "VISA"}]
    rows, _entries = make_settlements(auths, cfg, random.Random(1))
    assert len(rows) == 2
    assert rows[0].amount_minor == rows[1].amount_minor
    assert rows[0].settlement_id == rows[1].settlement_id
    assert rows[1].anomaly == "DUPLICATE"
    assert rows[0].anomaly == ""