"""Archiver Lambda: writes each SQS batch of EventBridge envelopes to S3 as gzipped JSON Lines.

Record flow:
    SQS batch (each body is one bus envelope)  ->  one s3://<bucket>/events/dt=YYYY-MM-DD/<ts>-<batch>.jsonl.gz

The envelope is archived exactly as delivered - nothing added, renamed or flattened. The
archive is the bus, byte for byte; DuckDB does the interpreting at query time.

Delivery is at-least-once: if the S3 write fails the whole batch is redelivered, so an event
can appear in two files. The `events` view in queries/views.sql dedupes on the envelope id.

The handler is split into small pure functions so they can be unit tested without AWS.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ARCHIVE_BUCKET = os.environ["ARCHIVE_BUCKET"]
ARCHIVE_PREFIX = os.environ.get("ARCHIVE_PREFIX", "events/")

# Created once per container and reused across warm invocations.
s3_client = boto3.client("s3")


# --------------------------------------------------------------------------- helpers


def parse_messages(records: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Split a batch into archive lines and the ids of messages that cannot be archived.

    A body that is a JSON object is re-serialised compactly (one line, no reformatting of
    its content). Anything else is reported back so SQS retries just that message.
    """
    lines: list[str] = []
    failed: list[str] = []
    for record in records:
        message_id = record.get("messageId")
        try:
            envelope = json.loads(record["body"])
        except (KeyError, TypeError, json.JSONDecodeError):
            envelope = None
        if not isinstance(envelope, dict):
            logger.warning(json.dumps({"msg": "message_rejected", "id": message_id}))
            failed.append(message_id)
            continue
        lines.append(json.dumps(envelope, separators=(",", ":")))
    return lines, failed


def object_key(now: datetime, batch_id: str, prefix: str = ARCHIVE_PREFIX) -> str:
    """events/dt=2026-09-27/2026-09-27T20-51-46Z-<batch>.jsonl.gz

    dt= is a Hive-style partition DuckDB prunes on; the timestamp in the name is what the
    lag query reads. Colons are avoided because they are awkward in S3 keys and URLs.
    """
    stamp = now.astimezone(UTC)
    return f"{prefix}dt={stamp:%Y-%m-%d}/{stamp:%Y-%m-%dT%H-%M-%S}Z-{batch_id}.jsonl.gz"


def pack(lines: list[str]) -> bytes:
    """Gzip newline-delimited JSON, one envelope per line, trailing newline."""
    return gzip.compress(("\n".join(lines) + "\n").encode("utf-8"))


def write_archive(key: str, body: bytes, client: Any = None, bucket: str = ARCHIVE_BUCKET) -> None:
    # No ContentEncoding header on purpose: DuckDB keys on the .gz extension, and a
    # transport-level encoding invites double decompression in some clients.
    (client or s3_client).put_object(
        Bucket=bucket, Key=key, Body=body, ContentType="application/x-ndjson"
    )


# --------------------------------------------------------------------------- handler


def lambda_handler(event: dict[str, Any], context: Any) -> dict:
    request_id = getattr(context, "aws_request_id", None) or "local"
    lines, failed = parse_messages(event.get("Records", []))

    key = None
    if lines:
        key = object_key(datetime.now(UTC), request_id)
        write_archive(key, pack(lines))

    logger.info(
        json.dumps(
            {
                "msg": "archive",
                "received": len(lines) + len(failed),
                "archived": len(lines),
                "rejected": len(failed),
                "key": key,
                "request_id": request_id,
            }
        )
    )
    return {"batchItemFailures": [{"itemIdentifier": i} for i in failed]}
