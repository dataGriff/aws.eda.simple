"""Tests for scripts/verify_events.py, the end-to-end assertion used against LocalStack."""

import json
import sys
from pathlib import Path

import boto3
import pytest
from botocore.stub import Stubber

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import verify_events  # noqa: E402

SOURCE = "com.example.test"


def envelope(event_id: str, kind: str = "order.created", source: str = SOURCE) -> str:
    detail = {"id": event_id, "type": kind, "timestamp": "2026-09-26T14:47:03Z", "data": {"a": 1}}
    return json.dumps(
        {
            "version": "0",
            "id": "aws-id",
            "detail-type": kind,
            "source": source,
            "time": detail["timestamp"],
            "detail": detail,
        }
    )


def test_check_passes_for_exact_wellformed_events():
    msgs = [envelope("evt_1"), envelope("evt_2", "payment.received")]
    assert verify_events.check(msgs, expect=2, source=SOURCE) == []


def test_check_reports_wrong_count():
    problems = verify_events.check([envelope("evt_1")], expect=3, source=SOURCE)
    assert problems == ["expected 3 event(s) in the log group, found 1"]


def test_check_reports_wrong_source_and_bad_json():
    msgs = [envelope("evt_1", source="com.other"), "not json"]
    problems = verify_events.check(msgs, expect=2, source=SOURCE)
    assert any("source is 'com.other'" in p for p in problems)
    assert any("not JSON" in p for p in problems)


def test_check_reports_duplicates_and_mismatched_detail_type():
    dup = envelope("evt_1")
    bad = json.loads(envelope("evt_2"))
    bad["detail-type"] = "something.else"
    problems = verify_events.check([dup, dup, json.dumps(bad)], expect=3, source=SOURCE)
    assert "duplicate event id(s): evt_1" in problems
    assert any("detail-type 'something.else' != detail.type" in p for p in problems)


def test_check_reports_missing_detail_keys():
    problems = verify_events.check(
        [json.dumps({"source": SOURCE, "detail": {"id": "x"}})], expect=1, source=SOURCE
    )
    assert problems == ["event 0: detail is missing type, timestamp, data"]


@pytest.fixture
def logs_client():
    client = boto3.client("logs", region_name="eu-west-1")
    with Stubber(client) as stub:
        yield client, stub
        stub.assert_no_pending_responses()


def test_fetch_messages_follows_pagination(logs_client):
    client, stub = logs_client
    stub.add_response(
        "filter_log_events",
        {"events": [{"message": "a"}], "nextToken": "t1"},
        {"logGroupName": "/g"},
    )
    stub.add_response(
        "filter_log_events",
        {"events": [{"message": "b"}, {"message": "c"}]},
        {"logGroupName": "/g", "nextToken": "t1"},
    )
    assert verify_events.fetch_messages(client, "/g") == ["a", "b", "c"]


def test_wait_for_events_polls_until_expected_count(logs_client, monkeypatch):
    client, stub = logs_client
    monkeypatch.setattr(verify_events.time, "sleep", lambda _s: None)
    stub.add_client_error("filter_log_events", "ResourceNotFoundException")
    stub.add_response("filter_log_events", {"events": [{"message": "a"}]}, {"logGroupName": "/g"})
    stub.add_response(
        "filter_log_events",
        {"events": [{"message": "a"}, {"message": "b"}]},
        {"logGroupName": "/g"},
    )
    msgs = verify_events.wait_for_events(client, "/g", expect=2, timeout=60, poll=0)
    assert msgs == ["a", "b"]


def test_wait_for_events_gives_up_after_timeout(logs_client, monkeypatch):
    client, stub = logs_client
    monkeypatch.setattr(verify_events.time, "sleep", lambda _s: None)
    stub.add_response("filter_log_events", {"events": []}, {"logGroupName": "/g"})
    assert verify_events.wait_for_events(client, "/g", expect=1, timeout=0, poll=0) == []


def test_main_exit_codes(monkeypatch, capsys):
    monkeypatch.setattr(verify_events.boto3, "client", lambda *_a, **_k: object())
    monkeypatch.setattr(
        verify_events, "wait_for_events", lambda *_a, **_k: [envelope("evt_1"), envelope("evt_2")]
    )
    argv = ["--log-group", "/g", "--expect", "2", "--source", SOURCE]
    assert verify_events.main(argv) == 0
    assert "OK: 2 event(s)" in capsys.readouterr().out
    assert verify_events.main([*argv[:-3], "1", "--source", SOURCE]) == 1
    assert "FAILED" in capsys.readouterr().out
