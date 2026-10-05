"""Debug utility: read events from the auth hub, summarize, quarantine
malformed payloads to data/rejects.jsonl (app-level DLQ).

SDK contracts honored:
1. client.receive() BLOCKS until the client is closed. A watchdog thread
   closes it at the event target or on timeout; try/finally guarantees
   a summary on every exit path.
2. With max_wait_time set, on_event(pc, None) fires each idle wait - a
   heartbeat, not an error.

Usage: python scripts/consume_check.py [max_events] [timeout_seconds]
"""
import json
import os
import sys
import threading
import time
from collections import Counter
from pathlib import Path

from azure.eventhub import EventHubConsumerClient

REJECTS = Path("data") / "rejects.jsonl"
REJECTS.parent.mkdir(exist_ok=True)
MAX = int(sys.argv[1]) if len(sys.argv) > 1 else 200
TIMEOUT = int(sys.argv[2]) if len(sys.argv) > 2 else 60

lock = threading.Lock()          # callbacks run on per-partition threads
kinds, codes = Counter(), Counter()
stats = {"read": 0, "rejects": 0, "idle_waits": 0}


def reached_target() -> bool:
    return stats["read"] + stats["rejects"] >= MAX


def on_event(pc, event):
    if event is None:                     # heartbeat: nothing arrived
        with lock:
            stats["idle_waits"] += 1
        print(".", end="", flush=True)    # liveness dot
        return
    raw = event.body_as_str()
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        with lock:
            stats["rejects"] += 1
            with REJECTS.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"partition": pc.partition_id,
                                    "offset": event.offset,
                                    "raw": raw[:500]}) + "\n")
        return
    with lock:
        stats["read"] += 1
        kinds[msg.get("event_kind", "?")] += 1
        codes[msg.get("response_code", "?")] += 1


def on_error(pc, error):
    part = pc.partition_id if pc else "?"
    print(f"\nCONSUMER ERROR (partition {part}): {error!r}", file=sys.stderr)


def summarize() -> None:
    print(f"\nread={stats['read']}  rejects={stats['rejects']}  "
          f"idle_waits={stats['idle_waits']}")
    print(f"kinds={dict(kinds)}")
    print(f"codes={dict(codes)}")
    if not stats["read"] and not stats["rejects"]:
        print("\nNothing received within the timeout. Produce first:")
        print("  authgen run --sink eventhub --max-events 200")


client = EventHubConsumerClient.from_connection_string(
    os.environ["EVENTHUB_CONNECTION_STRING"],
    eventhub_name=os.environ["EVENTHUB_NAME"],
    consumer_group="cg-recon",
)

print(f"Listening for up to {MAX} events (timeout {TIMEOUT}s). dots = idle\n")


def watchdog() -> None:
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline and not reached_target():
        time.sleep(0.5)
    client.close()                        # unblocks the receive() below


threading.Thread(target=watchdog, daemon=True).start()

try:
    with client:
        client.receive(on_event=on_event, on_error=on_error,
                       starting_position="-1", max_wait_time=2)
finally:
    summarize()