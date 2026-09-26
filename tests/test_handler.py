import json

from tests.helpers import fn_url_event, put_events_ok, sample_event
from webhook import app


def call(event, lambda_context):
    resp = app.lambda_handler(event, lambda_context)
    return resp["statusCode"], json.loads(resp["body"]), resp["headers"]


def test_non_post_is_405(stubber, lambda_context):
    status, body, headers = call(fn_url_event(sample_event(), method="GET"), lambda_context)
    assert status == 405
    assert body == {"error": "method_not_allowed"}
    assert headers["Allow"] == "POST"


def test_missing_secret_is_401(stubber, lambda_context):
    status, body, _ = call(fn_url_event(sample_event(), secret=None), lambda_context)
    assert (status, body) == (401, {"error": "unauthorized"})


def test_wrong_secret_is_401(stubber, lambda_context):
    status, _, _ = call(fn_url_event(sample_event(), secret="nope"), lambda_context)
    assert status == 401


def test_secret_header_name_is_case_insensitive(stubber, lambda_context):
    stubber.add_response("put_events", put_events_ok(1))
    event = fn_url_event(sample_event(), header_name="X-Webhook-Secret")
    status, body, _ = call(event, lambda_context)
    assert status == 202
    assert body["accepted"] == 1


def test_auth_checked_before_body(stubber, lambda_context):
    status, _, _ = call(fn_url_event("not json", secret=None), lambda_context)
    assert status == 401


def test_bad_json_is_400_and_nothing_sent(stubber, lambda_context):
    status, body, _ = call(fn_url_event("not json"), lambda_context)
    assert status == 400
    assert body["error"] == "invalid_request"
    assert any("invalid JSON" in d for d in body["details"])


def test_one_invalid_event_rejects_whole_batch(stubber, lambda_context):
    events = [sample_event(id="a"), sample_event(id="b", type="bogus"), sample_event(id="c")]
    status, body, _ = call(fn_url_event(events), lambda_context)
    assert status == 400
    assert body["details"] == [
        "event[1].type: must be one of order.created, order.updated, payment.received"
    ]


def test_valid_single_event_is_202(stubber, lambda_context):
    stubber.add_response("put_events", put_events_ok(1))
    status, body, headers = call(fn_url_event(sample_event()), lambda_context)
    assert status == 202
    assert body == {"accepted": 1, "failed": 0, "failures": []}
    assert headers["Content-Type"] == "application/json"
    assert headers["x-request-id"] == "test-request-id"


def test_valid_array_of_12_uses_two_batches(stubber, lambda_context):
    stubber.add_response("put_events", put_events_ok(10))
    stubber.add_response("put_events", put_events_ok(2))
    events = [sample_event(id=f"evt_{i}") for i in range(12)]
    status, body, _ = call(fn_url_event(events), lambda_context)
    assert status == 202
    assert body["accepted"] == 12


def test_base64_body_is_accepted(stubber, lambda_context):
    stubber.add_response("put_events", put_events_ok(1))
    status, _, _ = call(fn_url_event(sample_event(), b64=True), lambda_context)
    assert status == 202


def test_client_error_is_500(stubber, lambda_context):
    stubber.add_client_error("put_events", "InternalException", "boom", 500)
    status, body, _ = call(fn_url_event(sample_event()), lambda_context)
    assert (status, body) == (500, {"error": "internal_error"})
