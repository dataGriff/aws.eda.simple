import json
import re

import pytest
from faker import Faker

from generator import generate
from webhook import app


@pytest.fixture
def fake():
    return Faker()


@pytest.mark.parametrize("kind", sorted(generate.EVENT_TYPES))
def test_generated_events_satisfy_webhook_contract(kind, fake):
    orders = generate.OrderMemory()
    evt = generate.make_event(kind, fake, orders)
    assert evt["type"] == kind
    assert app.validate_event(evt) == [], f"{kind} does not satisfy the webhook contract"
    json.dumps(evt)  # must be serializable


def test_generator_and_webhook_agree_on_event_types():
    assert set(generate.EVENT_TYPES) == set(app.ALLOWED_TYPES)


def test_updates_reference_known_orders(fake):
    orders = generate.OrderMemory()
    created = generate.make_event("order.created", fake, orders)
    updated = generate.make_event("order.updated", fake, orders)
    payment = generate.make_event("payment.received", fake, orders)
    assert updated["data"]["order_id"] == created["data"]["order_id"]
    assert payment["data"]["order_id"] == created["data"]["order_id"]


def test_make_batch_length_and_type_filter(fake):
    batch = generate.make_batch(5, fake, generate.OrderMemory(), types=["payment.received"])
    assert len(batch) == 5
    assert {e["type"] for e in batch} == {"payment.received"}


def test_make_batch_single_object_when_batch_size_one(fake):
    payload = generate.build_payload(1, fake, generate.OrderMemory(), types=None)
    assert isinstance(payload, dict)
    payload = generate.build_payload(2, fake, generate.OrderMemory(), types=None)
    assert isinstance(payload, list) and len(payload) == 2


def test_dry_run_prints_payload_and_exits_zero(capsys):
    rc = generate.main(
        [
            "--url",
            "http://example.invalid",
            "--secret",
            "x",
            "--dry-run",
            "--count",
            "2",
            "--batch-size",
            "3",
            "--interval",
            "0",
            "--seed",
            "42",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if re.match(r"^\[\d\d:\d\d:\d\d\]", line)]
    assert len(lines) == 2
    assert all("dry-run" in line for line in lines)


def test_main_requires_url_and_secret(capsys, monkeypatch):
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    with pytest.raises(SystemExit) as exc:
        generate.main(["--dry-run"])
    assert exc.value.code == 2
