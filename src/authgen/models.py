"""
Domain models for card authorization and reversal events.
Rule: Money is ALWAYS represented in integer minor units (cents/pence) — never floats.
"""

# Enables modern type hinting (e.g., `str | None`) across Python 3.11+
from __future__ import annotations

# Utilities to create clean data classes and dictionary serialization
from dataclasses import asdict, dataclass

# Enum enforces strict sets of valid values, preventing invalid strings/typos
from enum import Enum


class EventKind(str, Enum):
    """
    Message types flowing through the card stream.
    Inheriting from `str` allows direct JSON serialization.
    """

    AUTH = "AUTH"  # Standard payment authorization request/response
    REVERSAL = "REVERSAL"  # Cancellation or void of a previous authorization


class GroundTruth(str, Enum):
    """
    Behind-the-scenes oracle labels recorded ONLY in the audit ledger.
    Downstream recon pipelines never see these; they must detect breaks independently.
    """

    APPROVED = "APPROVED"  # Valid, successful authorization
    DECLINED = "DECLINED"  # Standard decline (e.g., insufficient funds)
    EXPIRED_CARD = "EXPIRED_CARD_DECLINE"  # Organic decline caused by card expiry date
    DUPLICATE_SEND = "DUPLICATE_SEND"  # Retried message with matching auth/RRN
    REVERSAL = "REVERSAL"  # Transaction canceled after approval
    LATE_RESPONSE = (
        "LATE_RESPONSE_UNCERTAIN_SETTLEMENT"  # Issuer network timeout (codes 68/91)
    )


@dataclass(slots=True)
class AuthMessage:
    """
    ISO 8583-flavoured payment message emitted to the streaming lane.
    slots=True optimizes memory allocation and accelerates property lookup.
    """

    # --- Core Identifiers ---
    auth_id: str  # Unique message identifier (UUID or hash)
    event_kind: str  # AUTH or REVERSAL
    mti: str  # Message Type Identifier: 0110 (Auth resp) or 0410 (Reversal resp)
    rrn: str  # 12-digit Retrieval Reference Number: primary key for recon
    stan: str  # 6-digit System Trace Audit Number per physical terminal
    original_auth_id: str | None  # Points to original auth_id if this is a reversal

    # --- Payment Instrument (Card) Details ---
    card_token: str  # Masked/tokenized representation (PCI-DSS compliant, never raw PAN)
    card_scheme: str  # Network: VISA, MASTERCARD, AMEX, DISCOVER
    issuer_bin: str  # First 6 digits identifying the card-issuing bank
    card_country: str  # ISO country code of the cardholder's bank

    # --- Merchant & Terminal Details ---
    merchant_id: str  # Unique merchant identifier with the acquirer
    merchant_name: str  # Display name of the business
    mcc: str  # 4-digit Merchant Category Code (e.g., 5461 = Bakery)
    acquiring_bank: str  # Financial institution processing the card for merchant
    terminal_id: str  # Hardware/POS terminal identifier
    merchant_country: str  # Operating country (drives local currency)
    merchant_city: str  # Operating city

    # --- Financial Data ---
    amount_minor: int  # Transaction value in minor units (e.g., 1050 = 10.50 EUR)
    currency: str  # ISO 4217 3-letter currency code (EUR, USD, GBP)
    pos_entry_mode: str  # Capture method: CHIP, CONTACTLESS, MAGSTRIPE, ECOM_3DS

    # --- Temporal Data ---
    merchant_local_time: (
        str  # Transaction time at merchant POS with timezone offset
    )
    timestamp_utc: str  # Normalised UTC timestamp assigned by the streaming pipeline

    # --- Decision & Status Codes ---
    response_code: (
        str  # ISO 8583 result code: 00=Approved, 51=No funds, 54=Expired, etc.
    )
    auth_code: (
        str | None
    )  # 6-character authorization approval code (None if declined)
    reversal_reason: (
        str | None
    )  # POS_TIMEOUT, CUSTOMER_CANCEL, or AMOUNT_CORRECTION

    def to_dict(self) -> dict:
        """Converts the dataclass instance into a native Python dict for JSON export."""
        return asdict(self)