import pytest

from tests.helpers import fn_url_event, sample_event
from webhook import app


def test_parse_single_object_becomes_list():
    assert app.parse_body(fn_url_event(sample_event())) == [sample_event()]


def test_parse_array():
    events = [sample_event(id="a"), sample_event(id="b")]
    assert app.parse_body(fn_url_event(events)) == events


def test_parse_base64_body():
    assert app.parse_body(fn_url_event(sample_event(), b64=True)) == [sample_event()]


@pytest.mark.parametrize(
    "body, message",
    [
        ("not json", "invalid JSON"),
        ('"a string"', "expected a JSON object"),
        ("42", "expected a JSON object"),
        ("[]", "array is empty"),
        ("", "body is empty"),
    ],
)
def test_parse_rejects_bad_bodies(body, message):
    with pytest.raises(app.BadRequest, match=message):
        app.parse_body(fn_url_event(body))


def test_parse_rejects_missing_body():
    event = fn_url_event("{}")
    del event["body"]
    with pytest.raises(app.BadRequest, match="empty"):
        app.parse_body(event)


def test_parse_rejects_bad_base64():
    event = fn_url_event("%%%not-base64%%%")
    event["isBase64Encoded"] = True
    with pytest.raises(app.BadRequest, match="base64"):
        app.parse_body(event)


def test_parse_rejects_too_many():
    with pytest.raises(app.BadRequest, match="too many"):
        app.parse_body(fn_url_event([sample_event()] * 3), max_events=2)


@pytest.mark.parametrize("kind", sorted(app.ALLOWED_TYPES))
def test_validate_happy_path_all_types(kind):
    assert app.validate_event(sample_event(type=kind)) == []


def test_validate_not_object():
    assert app.validate_event("nope", 3) == ["event[3]: expected a JSON object"]


@pytest.mark.parametrize("missing", ["id", "type", "timestamp", "data"])
def test_validate_missing_required_field(missing):
    evt = sample_event()
    del evt[missing]
    errors = app.validate_event(evt, 1)
    assert len(errors) == 1
    assert errors[0].startswith(f"event[1].{missing}")


@pytest.mark.parametrize(
    "overrides, field",
    [
        ({"id": ""}, "id"),
        ({"id": 123}, "id"),
        ({"id": "x" * 129}, "id"),
        ({"type": "order.deleted"}, "type"),
        ({"timestamp": "2026-09-26T14:47:03"}, "timestamp"),  # naive
        ({"timestamp": "yesterday"}, "timestamp"),
        ({"timestamp": 1700000000}, "timestamp"),
        ({"data": []}, "data"),
        ({"data": "x"}, "data"),
    ],
)
def test_validate_field_rules(overrides, field):
    errors = app.validate_event(sample_event(**overrides))
    assert len(errors) == 1
    assert f".{field}:" in errors[0]


def test_validate_accepts_offset_timestamp():
    assert app.validate_event(sample_event(timestamp="2026-09-26T15:47:03+01:00")) == []


def test_validate_rejects_oversized_event():
    evt = sample_event(data={"blob": "x" * (app.MAX_DETAIL_BYTES + 1)})
    errors = app.validate_event(evt)
    assert len(errors) == 1
    assert "exceeds" in errors[0]


def test_is_authorized_case_insensitive_and_constant_time():
    assert app.is_authorized({"X-Webhook-Secret": "s3cret"}, "s3cret")
    assert app.is_authorized({"x-webhook-secret": "s3cret"}, "s3cret")
    assert not app.is_authorized({"x-webhook-secret": "wrong"}, "s3cret")
    assert not app.is_authorized({}, "s3cret")
    assert not app.is_authorized(None, "s3cret")
    assert not app.is_authorized({"x-webhook-secret": ""}, "")
