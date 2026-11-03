"""Gates for the settlements loader - the pure parts, no cloud needed.

Covers the loader's local gates (T-01/T-02 discipline: each test can fail
and can pass). The cloud steps (upload, load, MERGE) are gated at runtime
by the loader's own row-arithmetic check and the re-run digest.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "load_settlements", ROOT / "scripts" / "load_settlements.py")
ls = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ls)

HEADER = ",".join(ls.FIELDS)
GOOD = "sid-1,auth-1,rrn-1,1000,USD,15,2026-10-31,VISA"


def write_csv(tmp_path: Path, lines: list[str]) -> Path:
    p = tmp_path / "s.csv"
    p.write_text("\n".join([HEADER, *lines]) + "\n", encoding="utf-8")
    return p


def test_happy_path_single_row(tmp_path):
    rows, retrans = ls.read_and_gate(write_csv(tmp_path, [GOOD]))
    assert len(rows) == 1
    assert retrans == 0
    assert rows[0]["amount_minor"] == 1000  # ADR-001: money stays integer


def test_identical_retransmission_is_absorbed(tmp_path):
    rows, retrans = ls.read_and_gate(write_csv(tmp_path, [GOOD, GOOD]))
    assert len(rows) == 1
    assert retrans == 1


def test_differing_payload_under_same_id_is_fatal(tmp_path):
    other = "sid-1,auth-1,rrn-1,2000,USD,15,2026-10-31,VISA"
    with pytest.raises(SystemExit):
        ls.read_and_gate(write_csv(tmp_path, [GOOD, other]))


def test_bad_header_is_fatal(tmp_path):
    p = tmp_path / "s.csv"
    p.write_text("wrong,header\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        ls.read_and_gate(p)


def test_non_integer_amount_is_fatal(tmp_path):
    bad = "sid-1,auth-1,rrn-1,10.50,USD,15,2026-10-31,VISA"
    with pytest.raises(SystemExit):
        ls.read_and_gate(write_csv(tmp_path, [bad]))


def test_non_iso_date_is_fatal(tmp_path):
    bad = "sid-1,auth-1,rrn-1,1000,USD,15,31/10/2026,VISA"
    with pytest.raises(SystemExit):
        ls.read_and_gate(write_csv(tmp_path, [bad]))


def test_missing_file_is_fatal(tmp_path):
    with pytest.raises(SystemExit):
        ls.read_and_gate(tmp_path / "nope.csv")


# --- v2: partition pruning, scoped digest, curated staging, settings -------


def test_date_literals_sorted_deduped_quoted():
    rows = [{"settlement_date": "2026-11-02"},
            {"settlement_date": "2026-10-31"},
            {"settlement_date": "2026-11-02"}]
    assert ls.date_literals(rows) == ["DATE '2026-10-31'", "DATE '2026-11-02'"]


def test_canonical_payload_carries_header_and_one_row_per_id(tmp_path):
    rows, _ = ls.read_and_gate(write_csv(tmp_path, [GOOD, GOOD]))
    lines = ls.canonical_payload(rows).decode("utf-8").strip().splitlines()
    assert lines[0] == HEADER
    assert lines[1] == GOOD
    assert len(lines) == 2  # duplicate line absorbed before staging


def test_merge_sql_is_partition_pruned():
    sql = ls.merge_sql("p.d.settlements", "p.d.settlements_staging",
                       ["DATE '2026-10-31'"], "settlements_2026-10-31.csv")
    assert "T.settlement_date IN (DATE '2026-10-31')" in sql
    assert "ON T.settlement_id = S.settlement_id" in sql


def test_merge_sql_never_moves_the_partition_column():
    sql = ls.merge_sql("p.d.settlements", "p.d.settlements_staging",
                       ["DATE '2026-10-31'"], "settlements_2026-10-31.csv")
    assert "settlement_date=S.settlement_date" not in sql


def test_merge_sql_writes_audit_columns_on_insert_only():
    sql = ls.merge_sql("p.d.settlements", "p.d.settlements_staging",
                       ["DATE '2026-10-31'"], "settlements_2026-10-31.csv")
    assert "ingested_at, source_file" in sql
    assert "CURRENT_TIMESTAMP()" in sql
    assert "'settlements_2026-10-31.csv'" in sql
    # and the UPDATE SET mentions neither audit column
    update_part = sql.split("WHEN MATCHED THEN UPDATE SET")[1]
    update_part = update_part.split("WHEN NOT MATCHED")[0]
    assert "ingested_at" not in update_part
    assert "source_file" not in update_part


def test_digest_sql_uses_xor_and_scope_not_sum():
    sql = ls.digest_sql("p.d.settlements", ["DATE '2026-10-31'"])
    assert "BIT_XOR" in sql          # XOR cannot overflow
    assert "SUM(" not in sql         # SUM of INT64 fingerprints can
    assert "COALESCE" in sql         # empty partitions -> 0, not NULL
    assert "TO_JSON_STRING" in sql
    assert "WHERE t.settlement_date IN (DATE '2026-10-31')" in sql


def test_matched_sql_shares_the_merge_predicate():
    sql = ls.matched_sql("p.d.settlements", "p.d.settlements_staging",
                         ["DATE '2026-10-31'"])
    assert "WHERE T.settlement_date IN (DATE '2026-10-31')" in sql


def test_source_file_name_is_validated_before_inlining():
    assert ls.safe_source_file("settlements_2026-10-31.csv") \
        == "settlements_2026-10-31.csv"
    with pytest.raises(SystemExit):
        ls.safe_source_file("evil'); drop table settlements; --.csv")


def test_settings_flags_beat_env_beats_defaults():
    assert ls.resolve_settings(None, None, None, {}) == \
        ("privacyai-501020", "recon", "privacyai-501020-settlements")
    # bucket default follows the resolved project
    assert ls.resolve_settings(None, None, None,
                               {"GCP_PROJECT_ID": "p2"}) == \
        ("p2", "recon", "p2-settlements")
    # flags win over env
    assert ls.resolve_settings("p3", None, "b",
                               {"GCP_PROJECT_ID": "p2"}) == \
        ("p3", "recon", "b")
