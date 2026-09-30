"""Test helpers shared across modules (no AWS, no side effects on import)."""

import base64
import json

SECRET = "test-secret-0123456789abcdef"


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
