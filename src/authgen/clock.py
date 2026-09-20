"""Two clocks: live (real time) and simulated (a full day compressed into
N wall-clock minutes — makes Power BI show diurnal waves in a demo)."""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta


class LiveClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SimulatedClock:
    def __init__(self, day_minutes: float, start_utc: datetime | None = None):
        self._t0_wall = time.monotonic()
        self._t0_sim = start_utc or datetime.now(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0)
        self._speed = 86400.0 / (day_minutes * 60.0)  # sim-seconds per wall-second

    def now(self) -> datetime:
        elapsed = (time.monotonic() - self._t0_wall) * self._speed
        return self._t0_sim + timedelta(seconds=elapsed)