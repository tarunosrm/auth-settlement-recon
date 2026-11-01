"""Settlement domain models. Money is integer minor units (ADR-001)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class SettlementRow:
    settlement_id: str
    auth_id: str            # joins to recon.auth_events
    rrn: str
    amount_minor: int
    currency: str
    fee_minor: int
    settlement_date: str    # ISO date, T+1 by default
    scheme: str
    anomaly: str            # "", or one of the GroundTruth-style tags


ANOMALY_TAGS = ("FX_DRIFT", "AMOUNT_MISMATCH", "MISSING", "LATE_T5",
                "DUPLICATE", "PARTIAL")
