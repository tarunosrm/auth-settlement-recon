"""Sinks: Azure Event Hub (env-configured) or dry-run stdout / null (tests)."""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("authgen")


class NullSink:
    def send(self, messages: list[dict]) -> None:
        pass


class StdOutSink:
    def __init__(self, sample: int = 3):
        self.sample = sample
        self.sent = 0

    def send(self, messages: list[dict]) -> None:
        for m in messages:
            if self.sent < self.sample:
                print(json.dumps(m, indent=2))
            self.sent += 1


class EventHubSink:
    def __init__(self):
        from azure.eventhub import EventHubProducerClient
        conn = os.environ["EVENTHUB_CONNECTION_STRING"]
        name = os.environ["EVENTHUB_NAME"]
        self._producer = EventHubProducerClient.from_connection_string(
            conn, eventhub_name=name)

    def send(self, messages: list[dict]) -> None:
        from azure.eventhub import EventData
        try:
            batch = self._producer.create_batch()
            for m in messages:
                batch.add(EventData(json.dumps(m)))
            self._producer.send_batch(batch)
        except Exception:
            # Producer-side failure: log and drop this tick. Mirrors a real
            # producer's behaviour; consumer-side gaps show up in recon later.
            log.exception("Event Hub send failed; dropping %d message(s)", len(messages))

    def close(self):
        self._producer.close()