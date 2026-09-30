import json
from datetime import UTC, datetime

import pytest

from tests.helpers import sample_event
from webhook import app


def test_to_entry_maps_fields():
    evt = sample_event()
    entry = app.to_entry(evt, source="com.example.test", bus_name="test-bus")
    assert entry["Source"] == "com.example.test"
    assert entry["DetailType"] == "order.created"
    assert entry["EventBusName"] == "test-bus"
    assert json.loads(entry["Detail"]) == evt
    assert entry["Time"] == datetime(2026, 9, 26, 14, 47, 3, tzinfo=UTC)
    assert entry["Time"].tzinfo is not None


def test_to_entry_source_not_taken_from_payload():
    evt = sample_event(source="evil.source")
    entry = app.to_entry(evt, source="com.example.test", bus_name="test-bus")
    assert entry["Source"] == "com.example.test"


@pytest.mark.parametrize(
    "n, expected",
    [(0, []), (1, [1]), (10, [10]), (11, [10, 1]), (25, [10, 10, 5])],
)
def test_chunk_sizes(n, expected):
    assert [len(b) for b in app.chunk(list(range(n)))] == expected


def test_chunk_custom_size_preserves_order():
    assert list(app.chunk([1, 2, 3, 4, 5], size=2)) == [[1, 2], [3, 4], [5]]
