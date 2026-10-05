"""Event Hubs -> Delta. Idempotent by construction: MERGE on auth_id means
replays, restarts, and in-stream duplicates never duplicate rows."""
from pyspark.sql import functions as F
from pyspark.sql.types import IntegerType, StringType, StructField, StructType

try:
    dbutils  # noqa: F821  (notebooks/jobs inject it)
except NameError:
    from databricks.sdk.runtime import dbutils

STRING_FIELDS = ["auth_id", "event_kind", "mti", "rrn", "stan", "original_auth_id",
                 "card_token", "card_scheme", "issuer_bin", "card_country",
                 "merchant_id", "merchant_name", "mcc", "acquiring_bank",
                 "terminal_id", "merchant_country", "merchant_city", "currency",
                 "pos_entry_mode", "merchant_local_time", "timestamp_utc",
                 "response_code", "auth_code", "reversal_reason"]
SCHEMA = StructType([StructField(f, StringType(), True) for f in STRING_FIELDS]
                    + [StructField("amount_minor", IntegerType(), True)])
DB, TABLE = "recon", "silver_auth_events"
CKPT = "dbfs:/recon/_checkpoints/auth_stream"

def _ddl() -> str:
    return ", ".join([f"`{f}` STRING" for f in STRING_FIELDS]
                     + ["`amount_minor` INT", "`eh_enqueued_at` TIMESTAMP"])

def source():
    conn = dbutils.secrets.get("eventhub", "eventhub-listen-conn")
    cfg = {
        "eventhubs.connectionString":
            sc._jvm.org.apache.spark.eventhubs.EventHubsUtils.encrypt(conn),  # noqa: F821
        "eventhubs.name": dbutils.secrets.get("eventhub", "eventhub-name"),
        "eventhubs.consumerGroup": "cg-stream",
        # first run only: read the retention backlog, not just live tail
        "eventhubs.startingPosition": '{"offset": "-1", "seqNo": -1}',
    }
    raw = spark.readStream.format("eventhubs").options(**cfg).load()  # noqa: F821
    return (raw.select(F.from_json(F.col("body").cast("string"), SCHEMA).alias("m"),
                       F.col("enqueuedTime").alias("eh_enqueued_at"))
               .select("m.*", "eh_enqueued_at")
               .filter(F.col("auth_id").isNotNull()))

def upsert(batch_df, _batch_id):
    batch_df.dropDuplicates(["auth_id"]).createOrReplaceTempView("auth_updates")
    spark.sql(f"MERGE INTO {DB}.{TABLE} t USING auth_updates s "
              f"ON t.auth_id = s.auth_id WHEN NOT MATCHED THEN INSERT *")  # noqa: F821

spark.sql(f"CREATE DATABASE IF NOT EXISTS {DB}")        # noqa: F821
spark.sql(f"CREATE TABLE IF NOT EXISTS {DB}.{TABLE} ({_ddl()}) USING DELTA")  # noqa: F821

(source().writeStream
   .foreachBatch(upsert)
   .option("checkpointLocation", CKPT)
   .trigger(processingTime="30 seconds")
   .start()
   .awaitTermination())