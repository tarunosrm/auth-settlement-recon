"""Append-only JSONL ledger: every emitted message + ground-truth tags.

The event stream does NOT carry anomaly flags — downstream recon must detect
problems from data alone. The ledger is the oracle you later measure the
recon engine against (precision / recall on breaks).
"""
from __future__ import annotations

import json
from pathlib import Path


class Ledger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = 0

    def append(self, record: dict) -> int:
        self._seq += 1
        record["seq"] = self._seq
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
        return self._seq

    def iter_records(self):
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)