#!/usr/bin/env python3
"""Assert that the events log group holds exactly the events the generator sent.

The catch-all rule copies every event on the bus into ``/aws/events/<bus>``. This script
polls that log group until ``--expect`` events have arrived (delivery is asynchronous),
then checks each one is a well-formed EventBridge envelope from our source with the
inbound event passed through untouched, and that no event was duplicated.

Used by ``make local-verify`` (LocalStack, via ``--endpoint-url``) but works against AWS too.

Example:
    python scripts/verify_events.py --log-group /aws/events/simple-eda-bus --expect 30 \
        --source com.example.shop --endpoint-url http://localhost:4566
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import boto3

REQUIRED_DETAIL_KEYS = ("id", "type", "timestamp", "data")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log-group", required=True, help="Log group fed by the catch-all rule")
    parser.add_argument("--expect", type=int, required=True, help="Exact number of events expected")
    parser.add_argument("--source", required=True, help="Expected EventBridge source field")
    parser.add_argument(
        "--endpoint-url",
        default=os.environ.get("AWS_ENDPOINT_URL"),
        help="AWS endpoint override, e.g. http://localhost:4566 for LocalStack",
    )
    parser.add_argument(
        "--timeout", type=float, default=90.0, help="Seconds to wait for delivery (default 90)"
    )
    parser.add_argument("--poll", type=float, default=3.0, help="Seconds between polls")
    return parser.parse_args(argv)


def fetch_messages(client, log_group: str) -> list[str]:
    """Return every log message in the group, following pagination."""
    messages: list[str] = []
    kwargs = {"logGroupName": log_group}
    while True:
        page = client.filter_log_events(**kwargs)
        messages.extend(e["message"] for e in page.get("events", []))
        token = page.get("nextToken")
        if not token:
            return messages
        kwargs["nextToken"] = token


def check(messages: list[str], expect: int, source: str) -> list[str]:
    """Return a list of problems; empty means the delivered events are exactly as expected."""
    problems: list[str] = []
    if len(messages) != expect:
        problems.append(f"expected {expect} event(s) in the log group, found {len(messages)}")

    ids: list[str] = []
    for i, raw in enumerate(messages):
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError:
            problems.append(f"event {i}: message is not JSON: {raw[:80]!r}")
            continue
        if envelope.get("source") != source:
            problems.append(f"event {i}: source is {envelope.get('source')!r}, expected {source!r}")
        detail = envelope.get("detail")
        if not isinstance(detail, dict):
            problems.append(f"event {i}: detail is not an object")
            continue
        missing = [k for k in REQUIRED_DETAIL_KEYS if k not in detail]
        if missing:
            problems.append(f"event {i}: detail is missing {', '.join(missing)}")
            continue
        if envelope.get("detail-type") != detail["type"]:
            problems.append(
                f"event {i}: detail-type {envelope.get('detail-type')!r} != detail.type "
                f"{detail['type']!r}"
            )
        if envelope.get("time") != detail["timestamp"]:
            problems.append(
                f"event {i}: time {envelope.get('time')!r} != detail.timestamp "
                f"{detail['timestamp']!r}"
            )
        ids.append(detail["id"])

    duplicates = sorted({x for x in ids if ids.count(x) > 1})
    if duplicates:
        problems.append(f"duplicate event id(s): {', '.join(duplicates)}")
    return problems


def wait_for_events(client, log_group: str, expect: int, timeout: float, poll: float) -> list[str]:
    """Poll until at least ``expect`` messages are present or ``timeout`` seconds pass."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            messages = fetch_messages(client, log_group)
        except client.exceptions.ResourceNotFoundException:
            messages = []
        print(f"log group {log_group}: {len(messages)}/{expect} event(s)")
        if len(messages) >= expect or time.monotonic() >= deadline:
            return messages
        time.sleep(poll)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    client = boto3.client("logs", endpoint_url=args.endpoint_url)
    messages = wait_for_events(client, args.log_group, args.expect, args.timeout, args.poll)
    problems = check(messages, args.expect, args.source)
    if problems:
        print("FAILED:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"OK: {len(messages)} event(s) delivered, all well-formed, no duplicates")
    return 0


if __name__ == "__main__":
    sys.exit(main())
