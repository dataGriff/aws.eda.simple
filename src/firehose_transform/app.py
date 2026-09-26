"""Firehose transform Lambda: reshapes EventBridge envelopes into Iceberg table rows.

Record flow:
    EventBridge envelope (JSON)  ->  to_row  ->  one flat JSON object per record

Firehose maps only the *first level* of a record's JSON to table columns, and the
names and types must match the table exactly. The bus envelope nests the inbound
event under ``detail`` and names its type ``detail-type``, so the envelope cannot
be delivered as-is. Flattening here keeps ``detail`` as a JSON string, which means
a new event type or a new field inside ``data`` never breaks ingestion.

This module calls no AWS APIs, so every function is unit testable and the handler
runs offline under ``sam local invoke``.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Must stay in step with the Glue table schema in template.yaml.
COLUMNS = ("event_id", "event_type", "source", "event_time", "ingest_time", "detail")

# Columns Firehose cannot deliver a row without.
REQUIRED_COLUMNS = ("event_id", "event_type", "source", "event_time")


class UnusableRecord(Exception):
    """Raised when a Firehose record cannot be turned into a table row."""


# --------------------------------------------------------------------------- helpers


def to_iceberg_timestamp(value: Any) -> str | None:
    """Format an ISO-8601 instant as Iceberg expects it: UTC, millisecond, no offset.

    Returns None when the value is not a timestamp Firehose could parse.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def to_row(envelope: Any, *, ingested_at: datetime | None = None) -> dict[str, Any]:
    """Map an EventBridge envelope to one Iceberg table row.

    Raises UnusableRecord when a required column cannot be populated.
    """
    if not isinstance(envelope, dict):
        raise UnusableRecord("expected a JSON object")

    detail = envelope.get("detail")
    if not isinstance(detail, dict):
        raise UnusableRecord("detail: expected a JSON object")

    now = ingested_at or datetime.now(UTC)
    row = {
        "event_id": detail.get("id"),
        "event_type": envelope.get("detail-type"),
        "source": envelope.get("source"),
        "event_time": to_iceberg_timestamp(envelope.get("time")),
        "ingest_time": to_iceberg_timestamp(now.isoformat()),
        "detail": json.dumps(detail, separators=(",", ":")),
    }

    missing = [c for c in REQUIRED_COLUMNS if not row[c]]
    if missing:
        raise UnusableRecord(f"missing or unusable: {', '.join(missing)}")
    return row


def decode_record(record: dict[str, Any]) -> Any:
    """Base64-decode a Firehose record and parse it as JSON."""
    try:
        raw = base64.b64decode(record["data"], validate=True)
    except (KeyError, binascii.Error, ValueError) as exc:
        raise UnusableRecord("data: could not base64-decode record") from exc
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise UnusableRecord("data: record is not valid JSON") from exc


def encode_row(row: dict[str, Any]) -> str:
    """Serialise a row as the single JSON object Firehose expects per record."""
    payload = json.dumps(row, separators=(",", ":")) + "\n"
    return base64.b64encode(payload.encode("utf-8")).decode("utf-8")


def transform_records(
    records: list[dict[str, Any]], *, ingested_at: datetime | None = None
) -> list[dict[str, Any]]:
    """Transform a batch of Firehose records, isolating failures to single records.

    A record that cannot be reshaped is marked ProcessingFailed so Firehose routes
    it to the S3 error prefix instead of failing the whole batch.
    """
    out: list[dict[str, Any]] = []
    for record in records:
        record_id = record.get("recordId")
        try:
            row = to_row(decode_record(record), ingested_at=ingested_at)
        except UnusableRecord as exc:
            logger.warning(json.dumps({"msg": "record_failed", "id": record_id, "why": str(exc)}))
            out.append({"recordId": record_id, "result": "ProcessingFailed"})
            continue
        out.append({"recordId": record_id, "result": "Ok", "data": encode_row(row)})
    return out


# --------------------------------------------------------------------------- handler


def lambda_handler(event: dict[str, Any], context: Any) -> dict:
    records = event.get("records", [])
    results = transform_records(records)
    failed = sum(1 for r in results if r["result"] != "Ok")
    logger.info(
        json.dumps(
            {
                "msg": "transform",
                "received": len(records),
                "ok": len(results) - failed,
                "failed": failed,
                "request_id": getattr(context, "aws_request_id", None),
            }
        )
    )
    return {"records": results}
