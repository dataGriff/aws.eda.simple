import pytest
from botocore.exceptions import ClientError

from tests.helpers import put_events_ok, sample_event
from webhook import app


def entries(n):
    return [
        app.to_entry(sample_event(id=f"evt_{i}"), source="com.example.test", bus_name="test-bus")
        for i in range(n)
    ]


def test_put_events_batches_in_tens(stubber):
    batch = entries(25)
    for size in (10, 10, 5):
        stubber.add_response("put_events", put_events_ok(size))
    result = app.put_events(batch)
    assert (result.accepted, result.failed, result.failures) == (25, 0, [])


def test_put_events_sends_expected_params(stubber):
    batch = entries(2)
    stubber.add_response("put_events", put_events_ok(2), {"Entries": batch})
    result = app.put_events(batch)
    assert result.accepted == 2


def test_put_events_reports_partial_failure_with_global_index(stubber):
    batch = entries(12)
    stubber.add_response("put_events", put_events_ok(10))
    stubber.add_response(
        "put_events",
        {
            "FailedEntryCount": 1,
            "Entries": [
                {"EventId": "ok"},
                {"ErrorCode": "ThrottlingException", "ErrorMessage": "slow down"},
            ],
        },
    )
    result = app.put_events(batch)
    assert result.accepted == 11
    assert result.failed == 1
    assert result.failures == [
        {"index": 11, "id": "evt_11", "code": "ThrottlingException", "message": "slow down"}
    ]


def test_put_events_propagates_client_error(stubber):
    stubber.add_client_error("put_events", "AccessDeniedException", "nope", 403)
    with pytest.raises(ClientError):
        app.put_events(entries(1))


def test_put_events_empty_list_makes_no_calls(stubber):
    assert app.put_events([]) == app.PutResult()
