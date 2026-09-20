"""Core loop: realistic auths + three auth-side operational anomalies.

Auth-side anomalies (settlement-side ones come in Phase 2):
  1. DUPLICATE_SEND  — identical message retransmitted (terminal retry while
                       awaiting the issuer). No self-flagging: recon must spot
                       repeated auth_id/RRN.
  2. LATE_RESPONSE   — response codes 68/91: issuer never answered in time.
                       Whether the authorization actually happened is UNKNOWN
                       -> settlement may or may not arrive. Classic breaks cause.
  3. REVERSAL        — full reversal after approval (POS timeout, customer
                       cancel, amount correction). Must NEVER settle.
"""
from __future__ import annotations

import random
import string
from datetime import timedelta, timezone

from . import distributions as dist
from .clock import LiveClock
from .ledger import Ledger
from .merchants import CURRENCY_BY_COUNTRY, MERCHANTS, build_card_pool
from .models import AuthMessage, EventKind, GroundTruth

REVERSAL_REASONS = ["POS_TIMEOUT", "CUSTOMER_CANCEL", "AMOUNT_CORRECTION"]


class AuthGenerator:
    def __init__(self, cfg: dict, ledger: Ledger, sink, rng: random.Random | None = None,
                 clock=None):
        self.cfg = cfg
        self.rng = rng or random.Random(cfg.get("seed", 42))
        self.clock = clock or LiveClock()
        self.ledger = ledger
        self.sink = sink
        self.cards = build_card_pool(self.rng)
        self._card_rank_weights = [1 / (i + 8) ** 0.9 for i in range(len(self.cards))]
        self._stan_counters: dict[str, int] = {}
        a = cfg.get("anomalies", {})
        self.p_dup = a.get("duplicate_send_rate", 0.005)
        self.p_late = a.get("late_response_rate", 0.006)
        self.p_rev = a.get("reversal_rate", 0.025)

    # -- helpers ---------------------------------------------------------
    def _rrn(self, utc) -> str:
        t = utc.timetuple()
        return f"{t.tm_year % 100:02d}{t.tm_yday:03d}{t.tm_hour:02d}" \
               f"{self.rng.randrange(100000):05d}"

    def _auth_code(self) -> str:
        pool = string.ascii_uppercase + string.digits
        return "".join(self.rng.choices(pool, k=6))

    def _local_ts(self, utc, offset_min: int) -> str:
        tz = timezone(timedelta(minutes=offset_min))
        return utc.astimezone(tz).isoformat()

    def _emit(self, msg: AuthMessage, tags: list[str], **links) -> int:
    # Ledger: full-fidelity record with ground truth (oracle only — never leaves this machine)
    	seq = self.ledger.append({**msg.to_dict(), "ground_truth": tags, **links})

    # Stream: clean domain message only
    	self.sink.send([msg.to_dict()])

    	return seq

    # -- main step -------------------------------------------------------
    def step(self) -> None:
        rng, utc_now = self.rng, self.clock.now()
        # weekend-aware merchant pick
        weights = [m.weekend_x if utc_now.weekday() >= 5 else 1.0 for m in MERCHANTS]
        m = rng.choices(MERCHANTS, weights=weights)[0]
        terminal = f"T{rng.randrange(1, m.terminals + 1):04d}"
        stan = self._stan_counters.get(terminal, 0) + 1
        self._stan_counters[terminal] = stan % 1_000_000
        card = rng.choices(self.cards, weights=self._card_rank_weights)[0]  # repeat customers
        amount = dist.draw_amount(rng, m.median_minor, m.sigma)
        entry = rng.choices(
            ["CONTACTLESS", "CHIP", "ECOM_3DS", "ECOM_NO3DS", "MAGSTRIPE"],
            weights=[38, 30, 18, 9, 5])[0]

        # expiry drives the 54 decline organically
        expired = card.expiry_yymm < utc_now.strftime("%y%m")
        approved = (not expired) and rng.random() < dist.approval_probability(entry, amount)
        if expired:
            code, tags = "54", [GroundTruth.DECLINED.value, GroundTruth.EXPIRED_CARD.value]
        elif approved:
            code, tags = "00", [GroundTruth.APPROVED.value]
        else:
            code, tags = dist.decline_code(rng), [GroundTruth.DECLINED.value]

        base = dict(
            auth_id=f"a{rng.getrandbits(96):024x}",
            event_kind=EventKind.AUTH.value, mti="0110",
            rrn=self._rrn(utc_now), stan=f"{stan:06d}", original_auth_id=None,
            card_token=card.card_token, card_scheme=card.scheme,
            issuer_bin=card.issuer_bin, card_country=card.country,
            merchant_id=m.merchant_id, merchant_name=m.name, mcc=m.mcc,
            acquiring_bank=m.acquirer, terminal_id=terminal,
            merchant_country=m.country, merchant_city=m.city,
            amount_minor=amount, currency=CURRENCY_BY_COUNTRY[m.country],
            pos_entry_mode=entry,
            merchant_local_time=self._local_ts(utc_now, m.tz_offset_min),
            timestamp_utc=utc_now.isoformat(),
            response_code=code,
            auth_code=self._auth_code() if approved else None,
            reversal_reason=None,
        )
        msg = AuthMessage(**base)

        # late-response anomaly overrides the outcome: settlement uncertain
        settlement_possible = False
        if approved and rng.random() < self.p_late:
            msg.response_code = rng.choice(["68", "91"])
            msg.auth_code = None
            tags = [GroundTruth.DECLINED.value, GroundTruth.LATE_RESPONSE.value]
            settlement_possible = True  # oracle says: may still settle (~50% in Phase 2)

        seq = self._emit(msg, tags)

        # duplicate send: IDENTICAL message retransmitted (same auth_id/RRN)
        if approved and rng.random() < self.p_dup:
            self._emit(msg, tags + [GroundTruth.DUPLICATE_SEND.value],
                       duplicate_of_seq=seq)

        # full reversal after approval — must never be settled
        if approved and not settlement_possible and rng.random() < self.p_rev:
            rev = AuthMessage(**{**base,
                "auth_id": f"a{rng.getrandbits(96):024x}",
                "event_kind": EventKind.REVERSAL.value, "mti": "0410",
                "original_auth_id": msg.auth_id,
                "reversal_reason": rng.choice(REVERSAL_REASONS),
                "merchant_local_time": self._local_ts(
                    utc_now + timedelta(seconds=rng.randrange(5, 300)), m.tz_offset_min),
            })
            self._emit(rev, [GroundTruth.REVERSAL.value], reversal_of_seq=seq)