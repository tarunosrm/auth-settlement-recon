import argparse
import datetime
import json
import logging
import os

import apache_beam as beam
from apache_beam.coders import BooleanCoder
from apache_beam.options.pipeline_options import (
    GoogleCloudOptions,
    PipelineOptions,
    StandardOptions,
)
from apache_beam.transforms.userstate import ReadModifyWriteStateSpec

SCHEMA = {
    "auth_id": str, "event_kind": str, "rrn": str, "stan": str,
    "card_token": str, "card_scheme": str, "merchant_id": str,
    "merchant_name": str, "mcc": str, "amount_minor": int,
    "currency": str, "response_code": str, "timestamp_utc": str,
}

REQUIRED = ("auth_id", "rrn", "amount_minor", "currency", "response_code")

class ParseAndRoute(beam.DoFn):
    """One DoFn, two outputs: valid records -> 'ok', failures -> 'dead'."""
    def process(self, element):
        raw = element.decode("utf-8")
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as e:
            yield beam.pvalue.TaggedOutput("dead", {"raw_payload": raw[:1000], "error": f"JSONDecodeError: {e}"})
            return

        missing = [k for k in REQUIRED if k not in msg or msg.get(k) is None]
        if missing:
            yield beam.pvalue.TaggedOutput("dead", {"raw_payload": raw[:1000], "error": f"missing required fields: {missing}"})
            return

        rec = {k: msg.get(k) for k in SCHEMA}
        try:
            rec["amount_minor"] = int(rec["amount_minor"])
        except (ValueError, TypeError):
            yield beam.pvalue.TaggedOutput("dead", {"raw_payload": raw[:1000], "error": "amount_minor must be int"})
            return

        yield rec

class DedupeByAuthId(beam.DoFn):
    """Stateful dedupe: per-key state. Requires (key, value) input."""
    SEEN_STATE = beam.DoFn.StateParam(ReadModifyWriteStateSpec('seen', BooleanCoder()))

    def process(self, element, seen=SEEN_STATE):
        _, record = element
        if not seen.read():
            seen.write(True)
            yield record

def run(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--input_sub", required=True, help="Pub/Sub subscription")
    parser.add_argument("--bq_dataset", default="recon")
    parser.add_argument("--dead_letter_table", default=None)
    known, pipeline_args = parser.parse_known_args(argv)

    options = PipelineOptions(pipeline_args, save_main_session=True)
    options.view_as(StandardOptions).streaming = True  # Required for Pub/Sub

    project = options.view_as(GoogleCloudOptions).project
    if not project:
        project = os.environ.get("GOOGLE_CLOUD_PROJECT", "privacyai-501020")
        options.view_as(GoogleCloudOptions).project = project

    dlq_table = known.dead_letter_table or f"{project}:{known.bq_dataset}.auth_dead_letter"
    events_table = f"{project}:{known.bq_dataset}.auth_events"

    events_schema = {"fields": [{"name": k, "type": "STRING" if v == str else "INTEGER"} for k, v in SCHEMA.items()]}
    dlq_schema = {"fields": [
        {"name": "raw_payload", "type": "STRING"},
        {"name": "error", "type": "STRING"},
        {"name": "timestamp", "type": "TIMESTAMP"}
    ]}

    with beam.Pipeline(options=options) as p:
        messages = p | "ReadPubSub" >> beam.io.ReadFromPubSub(subscription=known.input_sub)
        routed = messages | "Parse" >> beam.ParDo(ParseAndRoute()).with_outputs("dead", main="ok")

        (routed["ok"]
         | "KeyByAuthId" >> beam.Map(lambda x: (x["auth_id"], x))
         | "Dedupe" >> beam.ParDo(DedupeByAuthId())
         | "WriteBQ" >> beam.io.WriteToBigQuery(
             events_table,
             schema=events_schema,
             create_disposition=beam.io.BigQueryDisposition.CREATE_IF_NEEDED,
             write_disposition=beam.io.BigQueryDisposition.WRITE_APPEND))

        (routed["dead"]
         | "AddTimestamp" >> beam.Map(lambda x: {**x, "timestamp": datetime.datetime.now(datetime.UTC).isoformat()})
         | "WriteDeadLetter" >> beam.io.WriteToBigQuery(
             dlq_table,
             schema=dlq_schema,
             create_disposition=beam.io.BigQueryDisposition.CREATE_IF_NEEDED,
             write_disposition=beam.io.BigQueryDisposition.WRITE_APPEND))

if __name__ == "__main__":
    logging.getLogger().setLevel(logging.INFO)
    run()
