#!/usr/bin/env python3
"""Fake data generator: POSTs synthetic shop events to the webhook Lambda.

Examples:
    python generator/generate.py --url https://xyz.lambda-url.eu-west-1.on.aws/ --secret $SECRET
    WEBHOOK_URL=... WEBHOOK_SECRET=... python generator/generate.py --batch-size 5 --interval 1
    python generator/generate.py --dry-run --count 3        # print payloads, do not POST

Only the standard library plus Faker is used, so no extra HTTP client is needed.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import UTC, datetime
from uuid import uuid4

from faker import Faker

EVENT_TYPES = ("order.created", "order.updated", "payment.received")
ORDER_STATUSES = ("packed", "shipped", "delivered", "cancelled")
PAYMENT_METHODS = ("card", "paypal", "bank_transfer")
CURRENCIES = ("GBP", "EUR", "USD")
AUTH_HEADER = "X-Webhook-Secret"


class OrderMemory:
    """Remembers recently created orders so updates and payments reference real ids."""

    def __init__(self, size: int = 50) -> None:
        self._orders: deque[dict] = deque(maxlen=size)

    def remember(self, order: dict) -> None:
        self._orders.append(order)

    def pick(self) -> dict | None:
        return random.choice(self._orders) if self._orders else None  # noqa: S311 (not crypto)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _envelope(kind: str, data: dict) -> dict:
    return {"id": _new_id("evt"), "type": kind, "timestamp": _now(), "data": data}


def _new_order(fake: Faker) -> dict:
    items = [
        {
            "sku": f"SKU-{fake.bothify(text='????-####').upper()}",
            "qty": random.randint(1, 4),  # noqa: S311
            "unit_price": float(fake.pydecimal(left_digits=3, right_digits=2, positive=True)),
        }
        for _ in range(random.randint(1, 3))  # noqa: S311
    ]
    return {
        "order_id": _new_id("ord"),
        "customer_name": fake.name(),
        "customer_email": fake.safe_email(),
        "items": items,
        "total": round(sum(i["qty"] * i["unit_price"] for i in items), 2),
        "currency": random.choice(CURRENCIES),  # noqa: S311
    }


def make_event(kind: str, fake: Faker, orders: OrderMemory) -> dict:
    """Build one event of the given type.

    Updates and payments reference a previously created order; if none exists yet one is
    synthesised in memory so the requested event type is always honoured.
    """
    if kind == "order.created":
        order = _new_order(fake)
        orders.remember(order)
        return _envelope(kind, order)

    order = orders.pick()
    if order is None:
        order = _new_order(fake)
        orders.remember(order)

    if kind == "order.updated":
        status = random.choice(ORDER_STATUSES)  # noqa: S311
        return _envelope(
            kind,
            {
                "order_id": order["order_id"],
                "status": status,
                "updated_fields": ["status"] + (["tracking_number"] if status == "shipped" else []),
            },
        )

    if kind == "payment.received":
        return _envelope(
            kind,
            {
                "payment_id": _new_id("pay"),
                "order_id": order["order_id"],
                "amount": order["total"],
                "currency": order["currency"],
                "method": random.choice(PAYMENT_METHODS),  # noqa: S311
            },
        )

    raise ValueError(f"unknown event type: {kind}")


def make_batch(n: int, fake: Faker, orders: OrderMemory, types: list[str] | None) -> list[dict]:
    kinds = list(types) if types else list(EVENT_TYPES)
    # Weight creates a little higher so updates/payments have orders to reference.
    weights = [3 if k == "order.created" else 2 for k in kinds]
    return [make_event(random.choices(kinds, weights)[0], fake, orders) for _ in range(n)]  # noqa: S311


def build_payload(batch_size: int, fake: Faker, orders: OrderMemory, types: list[str] | None):
    """batch_size == 1 sends a bare object; larger sends an array (both accepted by the webhook)."""
    batch = make_batch(batch_size, fake, orders, types)
    return batch[0] if batch_size == 1 else batch


def post(url: str, secret: str, payload, timeout: float = 10.0) -> tuple[int, str]:
    """POST the payload and return (status_code, body).

    Raises urllib.error.URLError on connection failure.
    """
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310 (scheme is whatever the user passed)
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", AUTH_HEADER: secret},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https URL from user)
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def _summarise(status: int, body: str) -> str:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body[:120]
    if "accepted" in data:
        return f"accepted={data['accepted']} failed={data['failed']}"
    return json.dumps(data)[:200]


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="POST fake shop events to the webhook Lambda.")
    p.add_argument(
        "--url", default=os.environ.get("WEBHOOK_URL"), help="Webhook URL (env WEBHOOK_URL)"
    )
    p.add_argument(
        "--secret",
        default=os.environ.get("WEBHOOK_SECRET"),
        help="Shared secret (env WEBHOOK_SECRET)",
    )
    p.add_argument("--interval", type=float, default=2.0, help="Seconds between POSTs (default 2)")
    p.add_argument("--count", type=int, default=0, help="Number of POSTs; 0 = run until Ctrl-C")
    p.add_argument("--batch-size", type=int, default=1, help="Events per POST (1 = single object)")
    p.add_argument("--types", help=f"Comma-separated subset of {', '.join(EVENT_TYPES)}")
    p.add_argument("--seed", type=int, help="Seed for reproducible fake data")
    p.add_argument("--dry-run", action="store_true", help="Print payloads instead of POSTing")
    args = p.parse_args(argv)

    if not args.url or not args.secret:
        p.error("--url and --secret are required (or set WEBHOOK_URL / WEBHOOK_SECRET)")
    if args.batch_size < 1:
        p.error("--batch-size must be >= 1")
    if args.types:
        args.types = [t.strip() for t in args.types.split(",") if t.strip()]
        unknown = set(args.types) - set(EVENT_TYPES)
        if unknown:
            p.error(f"unknown event types: {', '.join(sorted(unknown))}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.seed is not None:
        random.seed(args.seed)
        Faker.seed(args.seed)
    fake = Faker()
    orders = OrderMemory()

    sent = failed = 0
    iteration = 0
    try:
        while args.count == 0 or iteration < args.count:
            iteration += 1
            payload = build_payload(args.batch_size, fake, orders, args.types)
            n = args.batch_size
            stamp = datetime.now(UTC).strftime("%H:%M:%S")
            if args.dry_run:
                print(f"[{stamp}] dry-run {n} event(s):")
                print(json.dumps(payload, indent=2))
            else:
                try:
                    status, body = post(args.url, args.secret, payload)
                except urllib.error.URLError as exc:
                    failed += 1
                    print(f"[{stamp}] POST {n} event(s) -> connection error: {exc.reason}")
                else:
                    ok = 200 <= status < 300
                    sent += n if ok else 0
                    failed += 0 if ok else 1
                    print(f"[{stamp}] POST {n} event(s) -> {status} {_summarise(status, body)}")
            if args.count == 0 or iteration < args.count:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print()

    print(f"done: {iteration} request(s), {sent} event(s) accepted, {failed} request(s) failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
