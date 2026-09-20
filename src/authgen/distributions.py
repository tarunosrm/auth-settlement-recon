"""Realistic statistical shape: log-normal ticket sizes, diurnal traffic."""
from __future__ import annotations

import math

# 24 hourly weights (merchant-local time): overnight lull, lunch bump,
# evening peak. A live stream shaped by these shows real daily waves.
HOUR_WEIGHTS = [2, 1.5, 1, 1, 1, 1.5, 3, 6, 10, 12, 11, 12,
                14, 12, 10, 10, 12, 15, 18, 19, 16, 12, 7, 4]


def draw_hour(rng) -> int:
    return rng.choices(range(24), weights=HOUR_WEIGHTS)[0]


def draw_amount(rng, median_minor: int, sigma: float) -> int:
    """Log-normal: many small tickets, few large ones — the real retail shape."""
    amt = rng.lognormvariate(math.log(median_minor), sigma)
    return int(min(max(amt, 100), median_minor * 40))  # clamp outliers


DECLINE_WEIGHTS = [("05", 45), ("51", 28), ("91", 12), ("57", 8), ("65", 7)]
# 05 do-not-honor, 51 insufficient funds, 91 issuer unavailable,
# 57 txn not permitted, 65 activity-limit exceeded


def decline_code(rng) -> str:
    return rng.choices([c for c, _ in DECLINE_WEIGHTS],
                       weights=[w for _, w in DECLINE_WEIGHTS])[0]


def approval_probability(entry_mode: str, amount_minor: int) -> float:
    """Base ~0.955 conditional on a valid card; risky context lowers it."""
    p = 0.955
    if entry_mode == "ECOM_NO3DS":
        p -= 0.10                     # unauthenticated ecom declines more
    if amount_minor > 100_000:
        p -= 0.10                     # big-ticket friction
    elif amount_minor > 20_000:
        p -= 0.04
    return p