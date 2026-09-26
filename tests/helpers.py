"""Test helpers shared across modules (no AWS, no side effects on import)."""

import base64
import json

SECRET = "test-secret-0123456789abcdef"
ENVELOPE_ID = "e1f2a3b4-0000-1111-2222-333344445555"


def sample_event(**overrides):
    evt = {
        "id": "evt_0123456789ab",
        "type": "order.created",
        "timestamp": "2026-09-26T14:47:03Z",
        "data": {
            "order_id": "ord_abc123",
            "customer_name": "Ada Lovelace",
            "customer_email": "ada@example.com",
            "items": [{"sku": "SKU-1", "qty": 2, "unit_price": 9.99}],
            "total": 19.98,
            "currency": "GBP",
        },
    }
    evt.update(overrides)
    return evt


def fn_url_event(body, *, method="POST", secret=SECRET, b64=False, header_name="x-webhook-secret"):
    """Build a Lambda Function URL event (API Gateway HTTP payload format 2.0)."""
    if not isinstance(body, str):
        body = json.dumps(body)
    if b64:
        body = base64.b64encode(body.encode()).decode()
    headers = {"content-type": "application/json"}
    if secret is not None:
        headers[header_name] = secret
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": "/",
        "rawQueryString": "",
        "headers": headers,
        "requestContext": {"http": {"method": method, "path": "/"}},
        "body": body,
        "isBase64Encoded": b64,
    }


def put_events_ok(n):
    return {
        "FailedEntryCount": 0,
        "Entries": [{"EventId": f"evt-{i}"} for i in range(n)],
    }


def bus_envelope(evt, *, source="com.example.shop", envelope_id=ENVELOPE_ID):
    """Build the EventBridge envelope a rule target receives for an inbound event.

    Mirrors ``webhook.app.to_entry``: detail-type comes from the event type, time from
    the event timestamp, and detail is the whole inbound object.
    """
    return {
        "version": "0",
        "id": envelope_id,
        "detail-type": evt["type"],
        "source": source,
        "account": "123456789012",
        "time": evt["timestamp"],
        "region": "eu-west-1",
        "resources": [],
        "detail": evt,
    }


def bus_envelope_from_entry(entry, *, envelope_id=ENVELOPE_ID):
    """Build the envelope EventBridge would deliver for a given PutEvents entry."""
    time = entry["Time"]
    return {
        "version": "0",
        "id": envelope_id,
        "detail-type": entry["DetailType"],
        "source": entry["Source"],
        "account": "123456789012",
        "time": time.isoformat() if hasattr(time, "isoformat") else time,
        "region": "eu-west-1",
        "resources": [],
        "detail": json.loads(entry["Detail"]),
    }


def firehose_event(payloads, *, record_ids=None):
    """Build a Firehose transform invocation event from a list of record payloads.

    A payload that is not a str/bytes is JSON-encoded; pass a raw string to simulate
    a malformed record.
    """
    records = []
    for i, payload in enumerate(payloads):
        if isinstance(payload, bytes):
            raw = payload
        elif isinstance(payload, str):
            raw = payload.encode()
        else:
            raw = json.dumps(payload).encode()
        record_id = record_ids[i] if record_ids else f"rec-{i}"
        records.append({"recordId": record_id, "data": base64.b64encode(raw).decode()})
    return {"invocationId": "test-invocation", "records": records}


def decode_row(record):
    """Decode a transformed Firehose record back into a row dict."""
    return json.loads(base64.b64decode(record["data"]).decode())
