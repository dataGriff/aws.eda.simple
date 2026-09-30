"""Webhook Lambda: accepts JSON events over a Function URL and publishes them to EventBridge.

Request flow:
    POST <FunctionUrl>  (header X-Webhook-Secret)  ->  validate  ->  PutEvents (batches of 10)

The handler is split into small pure functions so they can be unit tested without AWS.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

EVENT_BUS_NAME = os.environ["EVENT_BUS_NAME"]
EVENT_SOURCE = os.environ.get("EVENT_SOURCE", "com.example.shop")
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
MAX_EVENTS_PER_REQUEST = int(os.environ.get("MAX_EVENTS_PER_REQUEST", "100"))

AUTH_HEADER = "x-webhook-secret"
PUT_EVENTS_BATCH_SIZE = 10  # hard limit of the EventBridge PutEvents API
MAX_DETAIL_BYTES = 256_000  # EventBridge entry limit is 256 KB
MAX_ID_LENGTH = 128
ALLOWED_TYPES = frozenset({"order.created", "order.updated", "payment.received"})

# Created once per container and reused across warm invocations.
events_client = boto3.client("events")


class BadRequest(Exception):
    """Raised when the inbound HTTP body cannot be turned into a list of events."""


@dataclass
class PutResult:
    accepted: int = 0
    failed: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------- helpers


def is_authorized(headers: dict[str, str] | None, secret: str) -> bool:
    """Constant-time comparison of the shared secret header (case-insensitive name)."""
    if not headers or not secret:
        return False
    provided = next((v for k, v in headers.items() if k.lower() == AUTH_HEADER), None)
    if provided is None:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), secret.encode("utf-8"))


def parse_body(event: dict[str, Any], max_events: int = MAX_EVENTS_PER_REQUEST) -> list[Any]:
    """Return the list of raw events in the request body.

    Accepts a single JSON object or a JSON array of objects. Raises BadRequest otherwise.
    """
    body = event.get("body")
    if body is None or body == "":
        raise BadRequest("body: request body is empty")

    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise BadRequest("body: could not base64-decode request body") from exc

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise BadRequest(f"body: invalid JSON ({exc.msg} at position {exc.pos})") from exc

    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        if not payload:
            raise BadRequest("body: event array is empty")
        if len(payload) > max_events:
            raise BadRequest(f"body: too many events ({len(payload)} > {max_events})")
        return payload
    raise BadRequest("body: expected a JSON object or an array of objects")


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def validate_event(evt: Any, index: int = 0) -> list[str]:
    """Return a list of human-readable validation errors (empty when valid)."""
    prefix = f"event[{index}]"
    if not isinstance(evt, dict):
        return [f"{prefix}: expected a JSON object"]

    errors: list[str] = []

    event_id = evt.get("id")
    if not isinstance(event_id, str) or not event_id:
        errors.append(f"{prefix}.id: required non-empty string")
    elif len(event_id) > MAX_ID_LENGTH:
        errors.append(f"{prefix}.id: longer than {MAX_ID_LENGTH} characters")

    event_type = evt.get("type")
    if event_type not in ALLOWED_TYPES:
        errors.append(f"{prefix}.type: must be one of {', '.join(sorted(ALLOWED_TYPES))}")

    if _parse_timestamp(evt.get("timestamp")) is None:
        errors.append(f"{prefix}.timestamp: required ISO-8601 timestamp with timezone")

    if not isinstance(evt.get("data"), dict):
        errors.append(f"{prefix}.data: required JSON object")

    if not errors:
        size = len(json.dumps(evt, separators=(",", ":")).encode("utf-8"))
        if size > MAX_DETAIL_BYTES:
            errors.append(f"{prefix}: serialized size {size} exceeds {MAX_DETAIL_BYTES} bytes")

    return errors


def to_entry(evt: dict[str, Any], *, source: str, bus_name: str) -> dict[str, Any]:
    """Map a validated inbound event to a PutEvents request entry."""
    timestamp = _parse_timestamp(evt["timestamp"])
    entry: dict[str, Any] = {
        "Source": source,
        "DetailType": evt["type"],
        "Detail": json.dumps(evt, separators=(",", ":")),
        "EventBusName": bus_name,
    }
    if timestamp is not None:
        entry["Time"] = timestamp
    return entry


def chunk(items: Iterable[Any], size: int = PUT_EVENTS_BATCH_SIZE) -> Iterator[list[Any]]:
    """Yield successive lists of at most ``size`` items."""
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def put_events(entries: list[dict[str, Any]], client: Any = None) -> PutResult:
    """Send entries to EventBridge in batches of 10 and collect per-entry failures.

    boto3 exceptions propagate to the caller; partial failures are reported in the result.
    """
    client = client or events_client
    result = PutResult()
    offset = 0
    for batch in chunk(entries):
        response = client.put_events(Entries=batch)
        returned = response.get("Entries", [])
        failed_in_batch = 0
        for i, item in enumerate(returned):
            if item.get("ErrorCode"):
                failed_in_batch += 1
                detail = json.loads(batch[i]["Detail"]) if i < len(batch) else {}
                result.failures.append(
                    {
                        "index": offset + i,
                        "id": detail.get("id"),
                        "code": item.get("ErrorCode"),
                        "message": item.get("ErrorMessage"),
                    }
                )
        # Trust FailedEntryCount if the API reports more failures than we could attribute.
        failed_in_batch = max(failed_in_batch, int(response.get("FailedEntryCount", 0)))
        result.failed += failed_in_batch
        result.accepted += len(batch) - failed_in_batch
        offset += len(batch)
    return result


def response(status: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> dict:
    out_headers = {"Content-Type": "application/json"}
    if headers:
        out_headers.update(headers)
    return {"statusCode": status, "headers": out_headers, "body": json.dumps(body)}


# --------------------------------------------------------------------------- handler


def lambda_handler(event: dict[str, Any], context: Any) -> dict:
    request_id = getattr(context, "aws_request_id", None)
    extra = {"x-request-id": request_id} if request_id else None

    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    if method != "POST":
        return response(405, {"error": "method_not_allowed"}, {"Allow": "POST", **(extra or {})})

    if not is_authorized(event.get("headers"), WEBHOOK_SECRET):
        logger.warning(json.dumps({"msg": "unauthorized", "request_id": request_id}))
        return response(401, {"error": "unauthorized"}, extra)

    try:
        raw_events = parse_body(event)
    except BadRequest as exc:
        return response(400, {"error": "invalid_request", "details": [str(exc)]}, extra)

    errors = [err for i, evt in enumerate(raw_events) for err in validate_event(evt, i)]
    if errors:
        return response(400, {"error": "invalid_request", "details": errors}, extra)

    entries = [to_entry(evt, source=EVENT_SOURCE, bus_name=EVENT_BUS_NAME) for evt in raw_events]

    try:
        result = put_events(entries)
    except Exception:
        logger.exception("put_events failed request_id=%s", request_id)
        return response(500, {"error": "internal_error"}, extra)

    logger.info(
        json.dumps(
            {
                "msg": "put_events",
                "received": len(entries),
                "accepted": result.accepted,
                "failed": result.failed,
                "request_id": request_id,
            }
        )
    )
    return response(202, asdict(result), extra)
