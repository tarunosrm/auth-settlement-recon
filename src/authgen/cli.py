import argparse
import logging
import random
import sys
import time
from pathlib import Path

import yaml

from .clock import LiveClock, SimulatedClock
from .generator import AuthGenerator
from .ledger import Ledger
from .producer import EventHubSink, NullSink, StdOutSink


def load_config(path: str) -> dict:
    p = Path(path)
    return yaml.safe_load(p.read_text()) if p.exists() else {}


def cmd_run(args) -> None:
    cfg = load_config(args.config)
    ledger = Ledger(cfg.get("ledger_path", "data/auth_ledger.jsonl"))
    rng = random.Random(cfg.get("seed", 42))

    if args.dry_run:
        sink = StdOutSink(sample=args.sample)
    elif args.sink == "none":
        sink = NullSink()
    else:
        sink = EventHubSink()  # needs EVENTHUB_CONNECTION_STRING + EVENTHUB_NAME

    if args.sim_day_minutes:
        clock, pace = SimulatedClock(args.sim_day_minutes), args.sim_sleep
    else:
        clock, pace = LiveClock(), 1.0 / cfg.get("events_per_second", 10)

    gen = AuthGenerator(cfg, ledger, sink, rng=rng, clock=clock)
    n, t0 = 0, time.monotonic()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        while n < args.max_events:
            gen.step()
            n += 1
            if n % 100 == 0:
                rate = n / (time.monotonic() - t0)
                print(f"[{n} events, {rate:.1f}/s wall]", file=sys.stderr)
            if pace > 0:
                time.sleep(pace * random.uniform(0.5, 1.5))  # jittered pacing
    except KeyboardInterrupt:
        print(f"\nStopped at {n} events.")
    finally:
        if isinstance(sink, EventHubSink):
            sink.close()


def cmd_stats(_args) -> None:
    from collections import Counter
    ledger = Ledger("data/auth_ledger.jsonl")
    kinds, codes, tags = Counter(), Counter(), Counter()
    amounts, dups, revs = [], 0, 0
    for r in ledger.iter_records():
        kinds[r["event_kind"]] += 1
        if r["event_kind"] == "AUTH":
            codes[r["response_code"]] += 1
            if r["response_code"] == "00":
                amounts.append(r["amount_minor"])
        tags.update(r.get("ground_truth", []))
        dups += r.get("ground_truth", []).count("DUPLICATE_SEND")
        revs += 1 if r["event_kind"] == "REVERSAL" else 0
    total = sum(kinds.values())
    print(f"events={total}  auths={kinds['AUTH']}  reversals={revs}")
    print(f"response codes: {dict(codes)}")
    appr = codes.get("00", 0)
    denom = appr + codes.get("05", 0) + codes.get("51", 0) + codes.get("54", 0) \
        + codes.get("57", 0) + codes.get("65", 0) + codes.get("68", 0) + codes.get("91", 0)
    if denom:
        print(f"approval rate: {appr / denom:.1%}")
    if amounts:
        amounts.sort()
        print(f"amount_minor p50={amounts[len(amounts)//2]} p95={amounts[int(len(amounts)*0.95)]}")
    print(f"duplicate sends={dups}  ground truth: {dict(tags)}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="authgen")
    sub = ap.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="generate and (optionally) send events")
    run.add_argument("--config", default="config.yaml")
    run.add_argument("--dry-run", action="store_true", help="no Event Hub; print samples")
    run.add_argument("--sink", choices=["eventhub", "none"], default="eventhub")
    run.add_argument("--max-events", type=int, default=200)
    run.add_argument("--sim-day-minutes", type=float, default=0,
                     help="compress one 24h day into N minutes (0 = live clock)")
    run.add_argument("--sim-sleep", type=float, default=0.05)
    run.add_argument("--sample", type=int, default=3)
    run.set_defaults(func=cmd_run)

    st = sub.add_parser("stats", help="summarize the ledger")
    st.set_defaults(func=cmd_stats)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()