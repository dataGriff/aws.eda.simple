"""Read-side tests: the archiver's own files, queried through queries/views.sql with DuckDB.

No AWS. The same views.sql runs against S3, LocalStack and this tmp_path.
"""

import json
from pathlib import Path

import duckdb
import pytest
from faker import Faker

from archiver import app as archiver
from generator import generate
from tests.helpers import bus_envelope, bus_envelope_from_entry, sample_event
from webhook import app as webhook

VIEWS = Path(__file__).resolve().parents[1] / "queries" / "views.sql"


def write_archive(root: Path, day: str, stamp: str, batch: str, envelopes) -> Path:
    path = root / "events" / f"dt={day}" / f"{stamp}Z-{batch}.jsonl.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(e, separators=(",", ":")) for e in envelopes]
    path.write_bytes(archiver.pack(lines))
    return path


@pytest.fixture
def con(tmp_path):
    con = duckdb.connect()
    con.execute("SET VARIABLE archive = ?", [str(tmp_path / "events" / "**" / "*.jsonl.gz")])
    yield con, tmp_path
    con.close()


def load_views(con):
    con.execute(VIEWS.read_text())


def test_events_view_maps_the_envelope(con):
    con, root = con
    evt = sample_event()
    write_archive(
        root, "2026-09-27", "2026-09-27T10-00-30", "aaa", [bus_envelope(evt, envelope_id="bus-1")]
    )
    load_views(con)

    row = con.execute(
        "SELECT bus_event_id, event_id, event_type, source, event_time, dt, archived_at FROM events"
    ).fetchone()

    assert row[0] == "bus-1"
    assert row[1] == evt["id"]
    assert row[2] == "order.created"
    assert row[3] == "com.example.shop"
    assert str(row[4]) == "2026-09-26 14:47:03"
    assert str(row[5]) == "2026-09-27"
    assert str(row[6]) == "2026-09-27 10:00:30"


def test_duplicate_deliveries_collapse_to_one_row(con):
    con, root = con
    dup = bus_envelope(sample_event(id="evt_dup"), envelope_id="bus-dup")
    other = bus_envelope(sample_event(id="evt_other"), envelope_id="bus-other")
    write_archive(root, "2026-09-27", "2026-09-27T10-00-30", "aaa", [dup])
    write_archive(root, "2026-09-27", "2026-09-27T10-01-00", "bbb", [dup, other])
    load_views(con)

    assert con.execute("SELECT count(*) FROM events").fetchone()[0] == 2
    assert (
        con.execute("SELECT count(*) FROM events WHERE bus_event_id = 'bus-dup'").fetchone()[0] == 1
    )


def test_order_items_unnests_every_line_item(con):
    con, root = con
    evt = sample_event()
    evt["data"]["items"] = [
        {"sku": "SKU-1", "qty": 2, "unit_price": 9.99},
        {"sku": "SKU-2", "qty": 1, "unit_price": 0.02},
    ]
    evt["data"]["total"] = 20.0
    write_archive(root, "2026-09-27", "2026-09-27T10-00-30", "aaa", [bus_envelope(evt)])
    load_views(con)

    rows = con.execute(
        "SELECT sku, qty, unit_price, line_total FROM order_items ORDER BY sku"
    ).fetchall()
    assert rows == [("SKU-1", 2, 9.99, 19.98), ("SKU-2", 1, 0.02, 0.02)]
    assert con.execute("SELECT round(sum(line_total), 2) FROM order_items").fetchone()[0] == 20.0


def test_orders_join_payments_through_the_payload(con):
    con, root = con
    order = sample_event(id="evt_o", data={**sample_event()["data"], "order_id": "ord_1"})
    payment = sample_event(
        id="evt_p",
        type="payment.received",
        data={
            "payment_id": "pay_1",
            "order_id": "ord_1",
            "amount": 19.98,
            "currency": "GBP",
            "method": "card",
        },
    )
    write_archive(
        root,
        "2026-09-27",
        "2026-09-27T10-00-30",
        "aaa",
        [bus_envelope(order, envelope_id="bus-o"), bus_envelope(payment, envelope_id="bus-p")],
    )
    load_views(con)

    row = con.execute(
        "SELECT o.order_id, o.total, p.method, p.amount "
        "FROM orders o JOIN payments p USING (order_id)"
    ).fetchone()
    assert row == ("ord_1", 19.98, "card", 19.98)


def test_unknown_payload_fields_do_not_break_the_views(con):
    con, root = con
    evt = sample_event()
    evt["data"]["brand_new_field"] = {"nested": [1, 2, 3]}
    write_archive(root, "2026-09-27", "2026-09-27T10-00-30", "aaa", [bus_envelope(evt)])
    load_views(con)

    assert con.execute("SELECT count(*) FROM order_items").fetchone()[0] == 1
    assert (
        con.execute(
            "SELECT json_extract_string(detail, '$.data.brand_new_field.nested[2]') FROM events"
        ).fetchone()[0]
        == "3"
    )


@pytest.mark.parametrize("kind", sorted(generate.EVENT_TYPES))
def test_generator_through_webhook_and_archiver_to_the_view(con, kind):
    """Generator output must survive the whole chain into the events view."""
    con, root = con
    evt = generate.make_event(kind, Faker(), generate.OrderMemory())
    assert webhook.validate_event(evt) == []
    entry = webhook.to_entry(evt, source="com.example.shop", bus_name="test-bus")
    envelope = bus_envelope_from_entry(entry)

    lines, failed = archiver.parse_messages([{"messageId": "m", "body": json.dumps(envelope)}])
    assert failed == []
    path = root / "events" / "dt=2026-09-27" / "2026-09-27T10-00-30Z-aaa.jsonl.gz"
    path.parent.mkdir(parents=True)
    path.write_bytes(archiver.pack(lines))
    load_views(con)

    row = con.execute("SELECT event_id, event_type, detail FROM events").fetchone()
    assert row[0] == evt["id"]
    assert row[1] == kind
    assert json.loads(row[2]) == evt
