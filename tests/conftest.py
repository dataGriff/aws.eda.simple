"""Shared fixtures. Environment variables must be set before ``webhook.app`` is imported."""

import os
from types import SimpleNamespace

import pytest
from botocore.stub import Stubber

from tests.helpers import SECRET

os.environ.setdefault("WEBHOOK_SECRET", SECRET)
os.environ.setdefault("EVENT_BUS_NAME", "test-bus")
os.environ.setdefault("EVENT_SOURCE", "com.example.test")
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-west-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

from webhook import app  # noqa: E402  (import after env setup on purpose)


@pytest.fixture
def stubber():
    with Stubber(app.events_client) as stub:
        yield stub
        stub.assert_no_pending_responses()


@pytest.fixture
def lambda_context():
    return SimpleNamespace(aws_request_id="test-request-id", function_name="webhook")
