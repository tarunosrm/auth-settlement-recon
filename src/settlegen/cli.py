import argparse
import json
import random
import sys
from pathlib import Path

import yaml

from .generator import load_approved_auths, make_settlements, write_files


def main() -> None:
    ap = argparse.ArgumentParser(prog="settlegen")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--ledger", default="data/auth_ledger.jsonl")
    ap.add_argument("--out", default="data/settlements")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--max-auths", type=int, default=0, help="0 = all")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    cfg = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
    rng = random.Random(args.seed if args.seed is not None
                        else cfg.get("seed", 42) + 7)
    ledger_path = Path(args.ledger)
    if not ledger_path.exists():
        sys.exit(f"auth ledger not found: {ledger_path} — run `authgen run` first")

    auths = load_approved_auths(ledger_path)
    if args.max_auths:
        auths = auths[:args.max_auths]
    rows, entries = make_settlements(auths, cfg, rng)

    out_dir = Path(args.out)
    paths = write_files(rows, out_dir)

    # ground-truth ledger (never travels with the CSV — ADR-002 parallel)
    led_path = out_dir.parent / "settle_ledger.jsonl"
    with led_path.open("a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    print(f"auths considered: {len(auths)}")
    print(f"settlement rows written: {len(rows)}")
    print(f"files: {', '.join(p.name for p in paths) or '(none)'}")
    print(f"ground truth: {led_path}")


if __name__ == "__main__":
    main()
