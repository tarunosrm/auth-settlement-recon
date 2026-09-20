"""Fictional but plausible merchant + card catalog.

Names/IDs are invented. BINs are random draws in scheme ranges and may collide
with real BINs — BINs alone aren't sensitive, and we never emit PANs (tokens
only). Tokens are deliberately NOT PAN-shaped (no Luhn-valid 16 digits) so the
dataset can never be mistaken for real cardholder data.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class Merchant:
    merchant_id: str
    name: str
    mcc: str
    city: str
    country: str            # drives currency (DE/NL/FR->EUR, GB->GBP, US->USD)
    acquirer: str
    tz_offset_min: int      # fixed offset; DST realism unnecessary for synthetic
    median_minor: int       # log-normal median, local currency minor units
    sigma: float
    weekend_x: float        # Sat/Sun pick-weight multiplier
    terminals: int


MERCHANTS = [
    Merchant("MDE1000231", "Kranz & Sohn Bakery",        "5461", "Berlin",  "DE", "Nordbank AG",   60,   650, 0.55, 0.8, 2),
    Merchant("MDE1000417", "Wursthaus Lukas",            "5814", "Munich",  "DE", "Nordbank AG",   60,  1100, 0.50, 1.0, 3),
    Merchant("MDE1000902", "Trattoria Da Raffaele",      "5812", "Hamburg", "DE", "Suedwest Bank", 60,  3500, 0.70, 1.3, 2),  # tips -> +settlement
    Merchant("MDE1001156", "Sparmarkt Ohlrogge",         "5411", "Bremen",  "DE", "Nordbank AG",   60,  2900, 0.60, 0.6, 4),
    Merchant("MDE1001288", "Apotheke am Ring",           "5912", "Cologne", "DE", "Rhein Treasury",60,  1900, 0.65, 0.7, 2),
    Merchant("MDE1002013", "Elektro Hoffmann",           "5732", "Stuttgart","DE","Suedwest Bank", 60, 12000, 0.90, 1.1, 2),
    Merchant("MDE1002445", "SpielZeug Kollberg",         "5945", "Dresden", "DE", "Nordbank AG",   60,  4200, 0.75, 1.6, 2),
    Merchant("MDE1003071", "CityGalerie Kaufhaus",       "5311", "Leipzig", "DE", "Rhein Treasury",60,  6500, 0.80, 1.2, 5),
    Merchant("MDE1003520", "Taxi-Ruf Nuernberg",         "4121", "Nuernberg","DE","Nordbank AG",   60,  1600, 0.60, 1.0, 8),
    Merchant("MDE1004102", "Hotel Strandperle",          "7011", "Sylt",    "DE", "Nordbank AG",   60, 18000, 0.80, 1.7, 1),  # late/partial settle
    Merchant("MNL1000518", "Kanaal Cafe Amsterdam",      "5812", "Amsterdam","NL","Acme Bank EU",   0,  2400, 0.65, 1.1, 2),
    Merchant("MFR1000765", "Boulangerie Martin",         "5461", "Lyon",    "FR", "Acme Bank EU",  60,   750, 0.55, 0.9, 2),
    Merchant("MGB1000633", "Pixel Dept Store London",    "5311", "London",  "GB", "Thames Clearing", 0, 7200, 0.80, 1.2, 3),
    Merchant("MUS1000907", "FlyHigh Airways US",         "4511", "Chicago", "US", "Midwest Trust", -300, 26000, 1.00, 0.9, 1),  # late/partial
]

CURRENCY_BY_COUNTRY = {"DE": "EUR", "NL": "EUR", "FR": "EUR", "GB": "GBP", "US": "USD"}


@dataclass(frozen=True)
class Card:
    card_token: str
    scheme: str
    issuer_bin: str
    country: str
    expiry_yymm: str        # organic 54-declines: ~6% of pool is already expired


SCHEME_PREFIX = {"VISA": "4", "MASTERCARD": "5", "AMEX": "3", "DISCOVER": "6"}
SCHEME_WEIGHTS = [("VISA", 52), ("MASTERCARD", 34), ("AMEX", 10), ("DISCOVER", 4)]
FOREIGN_ISSUERS = ["DE", "DE", "DE", "FR", "NL", "GB", "US", "IN"]  # domestic skew


def build_card_pool(rng, n: int = 400) -> list[Card]:
    """~6% already-expired (expiry offsets -3..+48 months) so declines emerge
    from data rather than a random coin flip."""
    import string
    now = datetime.now(UTC)
    cards = []
    for i in range(n):
        scheme = rng.choices([s for s, _ in SCHEME_WEIGHTS],
                             weights=[w for _, w in SCHEME_WEIGHTS])[0]
        issuer = rng.choice(FOREIGN_ISSUERS)
        bin_ = SCHEME_PREFIX[scheme] + "".join(rng.choices(string.digits, k=5))
        off = rng.randint(-3, 48)
        y, m = now.year, now.month + off
        y += (m - 1) // 12
        m = (m - 1) % 12 + 1
        cards.append(Card(
            card_token=f"tkn_{rng.getrandbits(64):016x}",
            scheme=scheme, issuer_bin=bin_, country=issuer,
            expiry_yymm=f"{y % 100:02d}{m:02d}",
        ))
    return cards