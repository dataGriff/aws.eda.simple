#!/usr/bin/env python3
"""Invoke the webhook Lambda handler in-process with a sample Function URL event.

Replaces ``sam local invoke``: it loads environment variables from a JSON file
(copy ``env.example.json`` to ``env.json``), imports ``webhook.app`` and calls
``lambda_handler`` with the event from ``events/post.json``.

The handler still calls the real EventBridge with your local AWS credentials, so
the bus must already exist (``make deploy``) or you will get a 500.

Example:
    python scripts/invoke_local.py --event events/post.json --env-file env.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--event", default="events/post.json", help="Path to the Function URL event JSON"
    )
    parser.add_argument("--env-file", default="env.json", help="JSON file of environment variables")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    env_path = Path(args.env_file)
    if not env_path.exists():
        print(f"{env_path} not found. Copy env.example.json to {env_path} first.", file=sys.stderr)
        return 2
    os.environ.update({k: str(v) for k, v in json.loads(env_path.read_text()).items()})

    event = json.loads(Path(args.event).read_text())

    # Import only after the environment is set: the module reads its config at import time.
    sys.path.insert(0, str(ROOT / "src"))
    from webhook import app

    context = SimpleNamespace(aws_request_id=str(uuid4()), function_name="webhook-local")
    result = app.lambda_handler(event, context)
    print(json.dumps(result, indent=2))
    return 0 if result.get("statusCode", 500) < 400 else 1


if __name__ == "__main__":
    sys.exit(main())
