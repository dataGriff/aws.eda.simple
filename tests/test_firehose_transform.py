"""Firehose transform tests: envelope -> row mapping, batch isolation, contract with the webhook."""

import base64
import json
import re
from datetime import UTC, datetime

import pytest
from faker import Faker

from firehose_transform import app as transform
from generator import generate
from tests.helpers import (
    bus_envelope,
    bus_envelope_from_entry,
    decode_row,
    firehose_event,
    sample_event,
)
from webhook import app as webhook

ISO_MILLIS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}$")
FIXED_NOW = datetime(2026, 9, 26, 15, 0, 0, tzinfo=UTC)


@pytest.fixture
def fake():
    return Faker()


# --------------------------------------------------------------------------- to_row


def test_to_row_maps_every_column():
    evt = sample_event()
    row = transform.to_row(bus_envelope(evt), ingested_at=FIXED_NOW)

    assert set(row) == set(transform.COLUMNS)
    assert row["event_id"] == evt["id"]
    assert row["event_type"] == "order.created"
    assert row["source"] == "com.example.shop"
    assert row["event_time"] == "2026-09-26T14:47:03.000"
    assert row["ingest_time"] == "2026-09-26T15:00:00.000"


def test_detail_round_trips_unchanged():
    evt = sample_event()
    row = transform.to_row(bus_envelope(evt))
    assert json.loads(row["detail"]) == evt


@pytest.mark.parametrize("kind", sorted(generate.EVENT_TYPES))
def test_to_row_handles_every_event_type(kind, fake):
    evt = generate.make_event(kind, fake, generate.OrderMemory())
    row = transform.to_row(bus_envelope(evt))
    assert row["event_type"] == kind
    assert json.loads(row["detail"])["data"] == evt["data"]


def test_event_time_is_normalised_to_utc():
    evt = sample_event(timestamp="2026-09-26T15:47:03+01:00")
    row = transform.to_row(bus_envelope(evt))
    assert row["event_time"] == "2026-09-26T14:47:03.000"


def test_timestamps_use_millisecond_precision():
    row = transform.to_row(bus_envelope(sample_event()))
    assert ISO_MILLIS.match(row["event_time"])
    assert ISO_MILLIS.match(row["ingest_time"])


@pytest.mark.parametrize(
    "envelope",
    [
        pytest.param("not an object", id="not-an-object"),
        pytest.param({"detail-type": "order.created"}, id="no-detail"),
        pytest.param({"detail": [], "detail-type": "order.created"}, id="detail-not-an-object"),
    ],
)
def test_to_row_rejects_unusable_envelopes(envelope):
    with pytest.raises(transform.UnusableRecord):
        transform.to_row(envelope)


@pytest.mark.parametrize("missing", ["id"])
def test_to_row_rejects_detail_without_required_field(missing):
    evt = sample_event()
    del evt[missing]
    with pytest.raises(transform.UnusableRecord, match="event_id"):
        transform.to_row(bus_envelope(evt))


@pytest.mark.parametrize("bad_time", ["", "not-a-time", "2026-09-26T14:47:03"])
def test_to_row_rejects_unparseable_or_naive_time(bad_time):
    envelope = bus_envelope(sample_event())
    envelope["time"] = bad_time
    with pytest.raises(transform.UnusableRecord, match="event_time"):
        transform.to_row(envelope)


def test_to_row_rejects_envelope_without_source():
    envelope = bus_envelope(sample_event())
    del envelope["source"]
    with pytest.raises(transform.UnusableRecord, match="source"):
        transform.to_row(envelope)


# --------------------------------------------------------------------------- batches


def test_transform_records_returns_one_result_per_record_in_order():
    events = [sample_event(id=f"evt_{i}") for i in range(3)]
    event = firehose_event([bus_envelope(e) for e in events])

    results = transform.transform_records(event["records"])

    assert [r["recordId"] for r in results] == ["rec-0", "rec-1", "rec-2"]
    assert all(r["result"] == "Ok" for r in results)
    assert [decode_row(r)["event_id"] for r in results] == ["evt_0", "evt_1", "evt_2"]


def test_transformed_record_is_one_json_object_per_line():
    results = transform.transform_records(firehose_event([bus_envelope(sample_event())])["records"])
    payload = base64.b64decode(results[0]["data"]).decode()
    assert payload.endswith("\n")
    assert len(payload.strip().splitlines()) == 1


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("{not json", id="not-json"),
        pytest.param('"a bare string"', id="not-an-object"),
        pytest.param("{}", id="empty-object"),
    ],
)
def test_unusable_records_are_marked_processing_failed(payload):
    results = transform.transform_records(firehose_event([payload])["records"])
    assert results == [{"recordId": "rec-0", "result": "ProcessingFailed"}]


def test_undecodable_record_is_marked_processing_failed():
    results = transform.transform_records([{"recordId": "rec-0", "data": "not base64!!"}])
    assert results == [{"recordId": "rec-0", "result": "ProcessingFailed"}]


def test_one_bad_record_does_not_fail_its_neighbours():
    records = firehose_event(
        [
            bus_envelope(sample_event(id="evt_good_1")),
            "{not json",
            bus_envelope(sample_event(id="evt_good_2")),
        ]
    )["records"]

    results = transform.transform_records(records)

    assert [r["result"] for r in results] == ["Ok", "ProcessingFailed", "Ok"]
    assert decode_row(results[0])["event_id"] == "evt_good_1"
    assert decode_row(results[2])["event_id"] == "evt_good_2"


def test_empty_batch_returns_no_records():
    assert transform.transform_records([]) == []


# --------------------------------------------------------------------------- handler


def test_handler_returns_records_envelope(lambda_context):
    event = firehose_event([bus_envelope(sample_event())])
    result = transform.lambda_handler(event, lambda_context)
    assert list(result) == ["records"]
    assert result["records"][0]["result"] == "Ok"


def test_handler_tolerates_event_without_records(lambda_context):
    assert transform.lambda_handler({}, lambda_context) == {"records": []}


# --------------------------------------------------------------------------- contract


@pytest.mark.parametrize("kind", sorted(generate.EVENT_TYPES))
def test_generator_through_webhook_to_table_row(kind, fake):
    """Generator output must survive the whole chain into a row matching the table schema."""
    evt = generate.make_event(kind, fake, generate.OrderMemory())
    assert webhook.validate_event(evt) == []

    entry = webhook.to_entry(evt, source="com.example.shop", bus_name="test-bus")
    row = transform.to_row(bus_envelope_from_entry(entry))

    assert set(row) == set(transform.COLUMNS)
    assert row["event_id"] == evt["id"]
    assert row["event_type"] == evt["type"]
    assert json.loads(row["detail"]) == evt


def test_required_columns_are_a_subset_of_columns():
    assert set(transform.REQUIRED_COLUMNS) <= set(transform.COLUMNS)
