"""The archiver as it runs: the real `bento` binary on src/archiver/archiver.yaml, consuming
from an SQS queue and writing to an S3 bucket served by a moto server. No AWS, no Docker.

`bento test` (archiver_bento_test.yaml) covers the mapping and the batching processors in
isolation; this covers what those tests cannot reach - the SQS input, the S3 output, the
ack/nack contract between them, and DuckDB reading what was written.

Needs `bento` on PATH (mise.toml installs it) and moto[server] (requirements-dev.txt).
"""

import gzip
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import boto3
import duckdb
import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "src" / "archiver" / "archiver.yaml"
VIEWS = ROOT / "queries" / "views.sql"
BENTO = os.environ.get("BENTO_BIN") or shutil.which("bento")

pytestmark = pytest.mark.skipif(BENTO is None, reason="bento binary not on PATH")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(predicate, timeout, what):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.25)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


@pytest.fixture(scope="module")
def moto():
    """A moto server: S3 and SQS on a local port, the way Floci is on 4566."""
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "moto.server", "-p", str(port), "-H", "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    endpoint = f"http://127.0.0.1:{port}"

    def up():
        try:
            return urllib.request.urlopen(f"{endpoint}/moto-api/", timeout=1).status == 200
        except OSError:
            return False

    try:
        wait_for(up, 30, "moto server")
        yield endpoint
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture
def aws(moto):
    kw = {
        "endpoint_url": moto,
        "region_name": "eu-west-1",
        "aws_access_key_id": "testing",
        "aws_secret_access_key": "testing",
    }
    return {"sqs": boto3.client("sqs", **kw), "s3": boto3.client("s3", **kw), "endpoint": moto}


@pytest.fixture
def queues(aws, request):
    """Archive queue plus a dead-letter queue, redriven after three receives, like template.yaml."""
    sqs = aws["sqs"]
    name = f"archive-{request.node.name}"[:70].replace("[", "-").replace("]", "")
    dlq = sqs.create_queue(QueueName=f"{name}-dlq")["QueueUrl"]
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]
    main = sqs.create_queue(
        QueueName=name,
        Attributes={
            "VisibilityTimeout": "3",
            "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": 3}),
        },
    )["QueueUrl"]
    yield main, dlq
    sqs.delete_queue(QueueUrl=main)
    sqs.delete_queue(QueueUrl=dlq)


def start_archiver(aws, queue_url, bucket, tmp_path, **env_overrides):
    """Run the archiver exactly as the container does, configured by environment only.

    The queue URL's host is deliberately wrong: Bento must send every SQS call to
    AWS_ENDPOINT_URL and treat the URL as the queue's name, which is what makes the same
    config work against Floci, whose queue URLs name a host containers cannot resolve.
    """
    bogus_url = queue_url.replace(aws["endpoint"], "http://sqs.eu-west-1.example.invalid")
    env = {
        **os.environ,
        "ARCHIVE_QUEUE_URL": bogus_url,
        "ARCHIVE_BUCKET": bucket,
        "ARCHIVE_PREFIX": "events/",
        "AWS_ENDPOINT_URL": aws["endpoint"],
        "AWS_REGION": "eu-west-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "S3_FORCE_PATH_STYLE": "true",
        "BATCH_COUNT": "100",
        "BATCH_PERIOD": "1s",
        "LOG_LEVEL": "DEBUG",
        **env_overrides,
    }
    log = (tmp_path / "bento.log").open("w")
    proc = subprocess.Popen(
        [BENTO, "-c", str(CONFIG), "--set", f"http.address=127.0.0.1:{free_port()}"],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    return proc, log


def stop(proc, log):
    proc.terminate()
    try:
        proc.wait(timeout=30)
    finally:
        log.close()


def objects(s3, bucket):
    listing = s3.list_objects_v2(Bucket=bucket, Prefix="events/")
    return [o["Key"] for o in listing.get("Contents", [])]


def depth(sqs, url):
    """(visible, in flight) message counts of a queue."""
    attrs = sqs.get_queue_attributes(
        QueueUrl=url,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
    )["Attributes"]
    return (
        int(attrs["ApproximateNumberOfMessages"]),
        int(attrs["ApproximateNumberOfMessagesNotVisible"]),
    )


def envelope(i, event_type="order.created"):
    return {
        "version": "0",
        "id": f"bus-{i}",
        "detail-type": event_type,
        "source": "com.example.shop",
        "account": "123456789012",
        "time": "2026-09-26T14:47:03Z",
        "region": "eu-west-1",
        "resources": [],
        "detail": {
            "id": f"evt_{i}",
            "type": event_type,
            "timestamp": "2026-09-26T14:47:03Z",
            "data": {"order_id": f"ord_{i}", "total": 19.98, "currency": "GBP", "items": []},
        },
    }


def test_batch_is_archived_verbatim_and_bad_message_is_dead_lettered(aws, queues, tmp_path):
    sqs, s3 = aws["sqs"], aws["s3"]
    main, dlq = queues
    bucket = "lake-archived"
    s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})

    bodies = [json.dumps(envelope(1), separators=(",", ":")), json.dumps(envelope(2), indent=2)]
    for body in bodies:
        sqs.send_message(QueueUrl=main, MessageBody=body)
    sqs.send_message(QueueUrl=main, MessageBody="not json at all")

    proc, log = start_archiver(aws, main, bucket, tmp_path)
    try:
        keys = wait_for(lambda: objects(s3, bucket), 30, "an archive object")
        assert len(keys) == 1, keys
        key = keys[0]
        assert key.startswith("events/dt=")
        assert key.endswith(".jsonl.gz")

        obj = s3.get_object(Bucket=bucket, Key=key)
        assert obj["ContentType"] == "application/x-ndjson"
        assert "ContentEncoding" not in obj
        lines = gzip.decompress(obj["Body"].read()).decode().split("\n")
        assert lines[-1] == ""
        got = sorted(lines[:-1])
        # The compact body is archived byte for byte; the indented one lands on one line.
        assert bodies[0] in got
        assert all("\n" not in line for line in got)
        assert sorted(json.loads(line)["id"] for line in got) == ["bus-1", "bus-2"]

        # The bad message was nacked three times and moved to the dead-letter queue;
        # the good ones were deleted from the archive queue once the file was written.
        wait_for(lambda: depth(sqs, dlq)[0] == 1, 30, "the bad message in the dead-letter queue")
        dead = sqs.receive_message(QueueUrl=dlq, MaxNumberOfMessages=1)["Messages"][0]
        assert dead["Body"] == "not json at all"

        wait_for(lambda: depth(sqs, main) == (0, 0), 30, "the archive queue to drain")

        # And DuckDB reads what Bento wrote, through the same views as production.
        local = tmp_path / key
        local.parent.mkdir(parents=True)
        local.write_bytes(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        con = duckdb.connect()
        con.execute("SET VARIABLE archive = ?", [str(tmp_path / "events" / "**" / "*.jsonl.gz")])
        con.execute(VIEWS.read_text())
        rows = con.execute("SELECT event_id, order_id FROM orders ORDER BY 1").fetchall()
        assert rows == [("evt_1", "ord_1"), ("evt_2", "ord_2")]
    finally:
        stop(proc, log)


def test_a_failed_write_is_retried_and_nothing_is_lost(aws, queues, tmp_path):
    """The bucket does not exist when the batch is due: Bento keeps retrying the write and
    keeps the messages invisible in the meantime; once the bucket appears the batch lands
    and the queue drains. A restart in between would return the messages to the queue."""
    sqs, s3 = aws["sqs"], aws["s3"]
    main, _ = queues
    bucket = "lake-late"

    sqs.send_message(QueueUrl=main, MessageBody=json.dumps(envelope(7)))
    proc, log = start_archiver(aws, main, bucket, tmp_path)
    try:
        wait_for(lambda: depth(sqs, main) == (0, 1), 15, "the message to be received")
        time.sleep(3)  # well past the 1s batch period: the write has failed at least once
        assert depth(sqs, main) == (0, 1), "a failed write must not release or delete the message"

        s3.create_bucket(
            Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"}
        )
        keys = wait_for(lambda: objects(s3, bucket), 60, "the retried write to land")
        assert len(keys) == 1
        lines = gzip.decompress(s3.get_object(Bucket=bucket, Key=keys[0])["Body"].read())
        assert json.loads(lines.decode().strip())["id"] == "bus-7"

        wait_for(
            lambda: depth(sqs, main) == (0, 0), 30, "the message to be deleted after the write"
        )
    finally:
        stop(proc, log)
        text = (tmp_path / "bento.log").read_text()
        assert "NoSuchBucket" in text or "failed" in text.lower(), text[-2000:]
