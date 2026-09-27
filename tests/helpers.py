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


def sqs_event(bodies, *, message_ids=None):
    """Build an SQS Lambda event. A body that is not a str is JSON-encoded."""
    records = []
    for i, body in enumerate(bodies):
        records.append(
            {
                "messageId": message_ids[i] if message_ids else f"msg-{i}",
                "receiptHandle": f"rh-{i}",
                "body": body if isinstance(body, str) else json.dumps(body),
                "attributes": {"ApproximateReceiveCount": "1"},
                "eventSource": "aws:sqs",
                "awsRegion": "eu-west-1",
            }
        )
    return {"Records": records}
