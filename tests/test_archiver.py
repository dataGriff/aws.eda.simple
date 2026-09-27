"""Archiver tests: batch parsing, S3 write (botocore Stubber), partial-batch failure reporting."""

import gzip
import json
from datetime import UTC, datetime

import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY

from archiver import app
from tests.helpers import bus_envelope, sample_event, sqs_event

FIXED_NOW = datetime(2026, 9, 27, 20, 51, 46, tzinfo=UTC)


def put_object_ok():
    return {"ETag": '"abc"'}


def expect_put(s3_stubber):
    """Expect one put_object to the test bucket. The key is time-based; another test checks it."""
    s3_stubber.add_response(
        "put_object",
        put_object_ok(),
        {"Bucket": "test-lake", "Key": ANY, "Body": ANY, "ContentType": "application/x-ndjson"},
    )


def unpack(body):
    return gzip.decompress(body).decode().splitlines()


# --------------------------------------------------------------------------- pure functions


def test_parse_messages_keeps_envelopes_verbatim():
    envelopes = [bus_envelope(sample_event(id=f"evt_{i}")) for i in range(3)]
    lines, failed = app.parse_messages(sqs_event(envelopes)["Records"])

    assert failed == []
    assert [json.loads(line) for line in lines] == envelopes
    assert all("\n" not in line for line in lines)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("{not json", id="not-json"),
        pytest.param('"a bare string"', id="not-an-object"),
        pytest.param("[1, 2, 3]", id="an-array"),
    ],
)
def test_parse_messages_reports_unusable_bodies_by_message_id(body):
    lines, failed = app.parse_messages(sqs_event([body], message_ids=["bad-1"])["Records"])
    assert lines == []
    assert failed == ["bad-1"]


def test_parse_messages_isolates_bad_records_from_good_ones():
    records = sqs_event(
        [
            bus_envelope(sample_event(id="evt_a")),
            "{not json",
            bus_envelope(sample_event(id="evt_b")),
        ],
        message_ids=["good-a", "bad", "good-b"],
    )["Records"]
    lines, failed = app.parse_messages(records)
    assert [json.loads(line)["detail"]["id"] for line in lines] == ["evt_a", "evt_b"]
    assert failed == ["bad"]


def test_object_key_partitions_by_day_and_stamps_the_batch():
    key = app.object_key(FIXED_NOW, "batch-123", prefix="events/")
    assert key == "events/dt=2026-09-27/2026-09-27T20-51-46Z-batch-123.jsonl.gz"


def test_object_key_normalises_to_utc():
    from datetime import timedelta, timezone

    plus_two = FIXED_NOW.astimezone(timezone(timedelta(hours=2)))
    assert app.object_key(plus_two, "b", prefix="events/") == app.object_key(
        FIXED_NOW, "b", prefix="events/"
    )


def test_pack_round_trips_one_json_object_per_line():
    lines = ['{"a":1}', '{"b":2}']
    packed = app.pack(lines)
    assert gzip.decompress(packed).decode() == '{"a":1}\n{"b":2}\n'


# --------------------------------------------------------------------------- handler


def test_handler_writes_one_object_per_batch(s3_stubber, lambda_context):
    envelopes = [bus_envelope(sample_event(id=f"evt_{i}")) for i in range(5)]
    expect_put(s3_stubber)

    result = app.lambda_handler(sqs_event(envelopes), lambda_context)

    assert result == {"batchItemFailures": []}


def test_handler_body_is_gzipped_ndjson_of_every_envelope(monkeypatch, lambda_context):
    envelopes = [bus_envelope(sample_event(id=f"evt_{i}")) for i in range(3)]
    written = {}
    monkeypatch.setattr(app, "write_archive", lambda key, body: written.update(key=key, body=body))

    app.lambda_handler(sqs_event(envelopes), lambda_context)

    assert written["key"].startswith("events/dt=")
    assert written["key"].endswith(f"-{lambda_context.aws_request_id}.jsonl.gz")
    assert [json.loads(line) for line in unpack(written["body"])] == envelopes


def test_handler_reports_bad_messages_and_still_archives_the_rest(s3_stubber, lambda_context):
    records = sqs_event([bus_envelope(sample_event()), "{not json"], message_ids=["good", "bad"])
    expect_put(s3_stubber)

    result = app.lambda_handler(records, lambda_context)

    assert result == {"batchItemFailures": [{"itemIdentifier": "bad"}]}


def test_handler_with_nothing_archivable_writes_nothing(s3_stubber, lambda_context):
    result = app.lambda_handler(sqs_event(["nope"], message_ids=["m1"]), lambda_context)
    assert result == {"batchItemFailures": [{"itemIdentifier": "m1"}]}


def test_handler_with_empty_batch_writes_nothing(s3_stubber, lambda_context):
    assert app.lambda_handler({"Records": []}, lambda_context) == {"batchItemFailures": []}


def test_s3_error_propagates_so_the_batch_is_redelivered(s3_stubber, lambda_context):
    s3_stubber.add_client_error("put_object", "InternalError", "boom", 500)
    with pytest.raises(ClientError):
        app.lambda_handler(sqs_event([bus_envelope(sample_event())]), lambda_context)
