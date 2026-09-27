# Bento on Fargate: the experiment, and whether it improved the solution

This branch is the third archiver. The first was Python in a Lambda (`feat/floci`). The second put a
[Bento](https://warpstreamlabs.github.io/bento/) stream *inside* the Lambda (`claude/bento-streaming-lambda-y0lyf4`)
and concluded that Bento in a Lambda is Bento with its input and buffering taken away, and that the
Bento-shaped design would be a long-running Bento with an `aws_sqs` input on ECS/Fargate. This branch builds that.
The webhook stays a Python Lambda for the reason that branch found and that has not changed: Bento has no
EventBridge output and its `http_client` cannot sign requests, so it cannot `PutEvents` on the bus.

**Verdict:** the archiver itself is the best of the three, and the solution as a whole is not. Bento is used
as designed now - long polling, batching by count and period, retries, and a message deleted only once the file
holding it is on S3, all in 75 lines of YAML in a 40 MB image built from the official one, tested offline
end to end against a moto server with the real binary, which neither Lambda variant could do. But it runs as a
process, and a process has to live somewhere: 17 more CloudFormation resources (a VPC, ECS, two roles), about
$11 a month before a single event flows against effectively nothing for either Lambda, Docker and ECR in the
deploy path, and a local run that no longer comes out of the template alone. For a repo called *simple*, at a
volume of a few events a second, that is a worse trade than the Python Lambda - and it is the right shape the
moment the archiver becomes a real stream job (routing, validation per schema, several sinks) or the volume
justifies an always-on consumer. On multiple schemas and Parquet, both asked about: the archive as it stands
needs no change for a new event type, and direct Parquet writing was measured and does not help at this batch
size. Details follow.

## What was built

```
                         ┌─ ECS Fargate task (arm64, 0.25 vCPU, 0.5 GB), one per service ──────┐
SQS ArchiveQueue ──────► │ input: aws_sqs (long poll 20 s, visibility extended while batching)  │
                         │ pipeline: mapping - one line per envelope, or error                   │
                         │ output: reject_errored ─► retry (5 m) ─► aws_s3                       │
                         │           errored ─► nack ─► SQS redelivers, DLQ after 3 receives     │
                         │           batching: 100 events or 30 s ─► archive lines, gzip, log    │
                         └──────────────────────────────────────────────────────────────────────┘
                                                        │ PutObject events/dt=…/…Z-<uuid>.jsonl.gz
                                                        ▼
                                                  S3 LakeBucket ──► DuckDB (unchanged views)
```

| | Python Lambda | Bento in Lambda | Bento on Fargate (this branch) |
|---|---|---|---|
| Archiver source | `app.py`, 108 lines | `archiver.yaml`, 95 lines | `archiver.yaml`, 118 lines (75 without comments), plus a 13-line Dockerfile |
| Batching | SQS event source mapping: 100 / 30 s, 5 min ceiling | the same mapping; Bento cannot batch in Lambda | Bento: `BATCH_COUNT` / `BATCH_PERIOD`, no ceiling; SQS visibility extended while a message waits |
| Ack contract | return `batchItemFailures` | return `batchItemFailures` via `sync_response` | delete on write, nack on error - SQS's own contract, no response shape to get right |
| S3 write fails | raise; SQS redelivers | Bento retries until the 60 s timeout; SQS redelivers | Bento retries with backoff for 5 min; then nack and SQS redelivers |
| Bad message | id reported; rest archived | same | nacked; redelivered, then dead-lettered after 3 receives; rest archived |
| Package | a few KB zip | 71 MB zip, 229 MB unpacked, 20 MB under Lambda's limit; Makefile + curl + sha256 | ~40 MB image: `FROM ghcr.io/warpstreamlabs/bento:1.21.2` + `COPY archiver.yaml` |
| Architecture | any | per-arch binary, `BENTO_ARCH`, separate build cache | the official image is multi-arch; the Dockerfile has no `RUN`, so `--platform linux/arm64` builds anywhere |
| Offline tests | 14 pytest, S3 stubbed | 8 `bento test`, output unreachable | 6 `bento test` + 2 pytest running the **real binary** against moto SQS and S3: verbatim archive, dead-lettering, a retried write, DuckDB reading the result - 16 s |
| Cold start | tens of ms | 0.4 s init, measured under the emulator | none: the task is always up; a deploy is a rolling replacement (about 2 min) |
| Template resources | 14 | 14 | 12 + 17 conditional (`ArchiverDeployment=fargate`) + an ECR repository the Taskfile creates |
| Runs locally as | Lambda in Floci | Lambda in Floci, native binary per host | the same image, `docker run` beside Floci, configured by environment only |
| Idle cost | ~$0 | ~$0 | ≈ $7/month task + ≈ $3.65/month public IPv4, list price eu-west-1, before any event |

What stayed the same: the bus, the rule, the queue and its dead-letter queue, the bucket, the file format,
the object key layout, `queries/views.sql` and every test on the read side. DuckDB cannot tell the branches apart.

## What the Fargate shape fixed

- **Bento has its input back.** `aws_sqs` long-polls, and the `aws_s3` output batches by count *and* period
  with no 300-second ceiling. The two numbers are environment variables in the task definition; a 1,000-event,
  5-minute batch is an edit, not a redesign, and the queue keeps the messages invisible while they wait
  (`update_visibility`).
- **The delivery contract is SQS's, not Lambda's.** A message is deleted when its batch is on S3, and nacked
  when its body is not a JSON object - so it comes straight back, and after three receives lands in the
  dead-letter queue by itself. There is no `batchItemFailures` shape to return and no `switch` output to build
  it, which was half of the Lambda config.
- **Retries where they belong, and bounded.** A failed write is retried with backoff inside the process for
  up to five minutes and only then handed back to SQS. Found while testing: without the `retry` wrapper the
  `aws_s3` output nacks on the *first* failure, so a blip on S3 would cost one of the queue's three receives.
- **The package problem is gone.** No 229 MB zip against a 250 MB limit, no Makefile downloading a release, no
  per-architecture build cache. The image is the official one plus one file, and it is the same image on the
  laptop, in CI and on Fargate.
- **It is tested as it runs.** `tests/test_archiver_stream.py` starts a moto server, creates the queues with the
  template's redrive policy, launches `bento -c src/archiver/archiver.yaml` with the task definition's
  environment variables, and checks the S3 object byte for byte, the dead-letter queue, an in-flight message
  surviving a missing bucket until the bucket appears, and DuckDB reading the object through the production
  views. The queue URL in that test points at a host that does not exist, which proves the config sends every
  call to `AWS_ENDPOINT_URL` - the property the Floci run depends on.
- **Two Floci gaps disappeared.** There is no event source mapping to patch after deploy and no
  `ReportBatchItemFailures` for the emulator to ignore. The local dead-letter path works now, because it is
  plain SQS.
- **Deploys are guarded.** The ECS deployment circuit breaker rolls back a task that fails its health check
  (`/ready` on Bento's HTTP server), and the image tag is a hash of the Dockerfile and the config, so an
  unchanged archiver redeploys as a no-op.

## What it cost

- **A network.** Fargate needs a VPC. The cheapest one that works without a NAT gateway (≈ $35/month) is two
  public subnets, an internet gateway, a public IP on the task and a security group with no ingress; S3 goes
  through a free gateway endpoint. That is 11 resources whose only purpose is to let one container reach SQS,
  ECR and CloudWatch, and the template grew from 273 lines to 509.
- **Money for nothing.** The smallest arm64 task is about $7 a month at list price in eu-west-1 and its public
  IPv4 address about $3.65, whether or not events flow. Both Lambdas cost pennies at this volume. There is
  `task archiver:scale N=0` to pause it - the queue holds 14 days - but a pipeline you have to remember to turn
  off is not simpler than one that scales to zero.
- **Docker and ECR in the deploy path.** `task deploy` now builds an image, logs into ECR, creates the
  repository on first use (it cannot live in the stack: the service would try to pull before the push) and pushes
  before `sam deploy`. Docker is a prerequisite for deploying, not just for the local run. A config change is a
  rolling replacement measured in minutes rather than a `sam deploy` measured in seconds.
- **The template has an emulator concession.** `ArchiverDeployment=none` exists so the Floci deploy can skip
  ECS and the Taskfile can `docker run` the archiver instead. Floci lists ECS, ECR and EC2 VPC resources as
  emulated, so the real template might deploy there unchanged; that was not tried on this branch, and a plain
  container was chosen because it has fewer moving parts and runs the identical image.
- **A process to operate.** Health checks, restarts, rollbacks, a log group per container, a `/metrics`
  endpoint nobody scrapes (Bento's `aws_cloudwatch` metrics exporter would fix that for `cloudwatch:PutMetricData`
  and about $0.30 per metric per month).
- **Two roles instead of one**, and a task role that mirrors what the Lambda's had. Not hard, but more IAM.
- **One SDK edge to know about.** A nack resets the message's visibility to zero through
  `ChangeMessageVisibilityBatch`, and the Go SDK omits a zero-valued `VisibilityTimeout` from the request. AWS
  treats the missing field as zero; moto rejects it, so the test queues use a 3-second visibility timeout and
  wait for expiry instead. If Floci behaves like moto, a rejected message is dead-lettered after three visibility
  timeouts (about 5 minutes) rather than at once. Nothing is lost either way.
- **Not verified on this branch:** an AWS deploy (no account in the session that built it) and the Floci run
  (no Docker in that session). CI runs the Floci job on every push; the AWS-only resources - VPC, ECS service,
  roles, health check - pass `cfn-lint` and `sam validate` and follow the standard Fargate pattern, but have not
  been seen running.

## Multiple event schemas

The archive already handles this, and that is the strongest argument for leaving the archiver alone. Every
file line is the bus envelope; `detail-type` says which schema `detail` follows, and `detail` is stored as
JSON. A fourth event type tomorrow is one line in the webhook's allow-list, no change to the archiver, and one more
view in `queries/views.sql` for whoever wants a typed table.
The `events` view keeps working for every type, `orders` and `payments` keep working for theirs.

Where Bento earns its place if that future arrives is *in front of* the file, not in its format:

- **Validation per schema.** A `json_schema` processor per `detail-type`, chosen with a `switch`, and a
  `switch` output that sends failures to `quarantine/` in the same bucket instead of nacking them. Twenty lines
  of the same config, unit-testable with `bento test`, no code.
- **Partitioning by type.** Adding `type=<detail-type>/` before `dt=` in the key lets DuckDB prune a per-type
  view down to that type's files. The cost is one file per type per batch - at this volume, three times as many
  small files - so it is worth doing only when a type's view is scanning mostly other types' data.
- **Routing.** A second sink for one type (Kafka, another bucket, a Firehose) is a `switch` case.

None of those needs Parquet, and all of them keep the raw line as the record.

## Would direct Parquet writing help?

Measured, with Bento's own `parquet_encode` (zstd) on events from this repo's generator, against the gzipped
JSON Lines the archiver writes today. Two schemas were tried: one **envelope** schema that works for every
event type (`id`, `detail_type`, `source`, `time` typed, `detail` as a JSON string), and one **typed** schema
for `order.created` alone (payload fields as columns, `items` as a repeated group). The typed file holds only
that type's events, so its like-for-like comparison is those same events as gzipped JSON Lines.

| Events per file | gzip JSON Lines, all types | Parquet, envelope schema | `order.created` events | gzip JSON Lines, those only | Parquet, typed schema |
|---|---|---|---|---|---|
| 30 (one 30 s batch at the generator's rate) | 2.9 KB | 6.2 KB | 16 | - | 5.0 KB |
| 100 (a full batch) | 7.3 KB | 10.0 KB | 36 | 4.2 KB | 6.2 KB |
| 1,000 | 71 KB | 87 KB | 401 | 40 KB | 37 KB |
| 10,000 | 718 KB | 815 KB | 4,187 | 413 KB | 270 KB |

- **The envelope schema is bigger than gzip JSON Lines at every size** (+13% to +117%). The JSON string
  column *is* the file; Parquet adds column metadata and a footer on top and zstd compresses it about as well
  as gzip did. It also stops being the bus bytes. What it buys is typed envelope columns and min/max statistics
  on `time` and `detail_type`, which the `dt=` partition and a filter on a 5-column view already give cheaply.
- **The typed schema only pays with thousands of events per file.** At 100 events it is 50% *bigger* than the
  same events as gzip JSON Lines; parity at about 1,000; 35% smaller at 10,000, and columnar, so a query
  touching two of eight columns reads a fraction of the bytes. At this repo's volume a 10,000-event file is
  hours of batching - or a compaction job, and a compaction job is exactly where Parquet belongs.
- **Schema evolution is the real problem, and it is silent.** Checked on Bento 1.21.2: a field the schema does
  not list is dropped without an error, and a *missing* `INT64` field is written as `0`, also without an error
  (`optional: true` only changes the Parquet nullability). A producer adding a field loses it until someone
  edits the archiver; a producer renaming one writes zeros into the lake. The JSON archive has neither failure
  mode: a new field is in the file, a renamed one is visible in the file, and the view is where it is dealt with.
- **Multiple schemas multiply the cost.** One `parquet_encode` schema per type, a `switch` output per type, one
  more small file per type per batch, and every producer change becomes an archiver deploy. That is the coupling
  the README argues against: every consumer of a typed table pays for the schema up front.

So: no, not in the archiver. Keep the landing zone as it is - raw, verbatim, schema-free, cheap to write - and
make typed Parquet or Iceberg tables *from* it, per type, on whatever schedule the readers want, with the
schema living next to the query that defines it:

```sql
-- DuckDB, over the views in queries/views.sql; the same works as an Athena CTAS
COPY (SELECT * FROM orders WHERE event_time >= '2026-09-27')
  TO 's3://<bucket>/tables/orders/dt=2026-09-27/orders.parquet' (FORMAT parquet, COMPRESSION zstd);
```

That job can be Bento too (`aws_s3` input over the day's files, `parquet_encode`, `aws_s3` output) or a
scheduled Lambda running the SQL above; either way ingestion never learns a payload schema, and a bad day in the
table is rebuilt from the archive.

## If you keep it

- **Batch bigger.** `BATCH_COUNT=1000`, `BATCH_PERIOD=5m` in the task definition cuts the file count tenfold
  and costs at most five minutes of archive lag. The visibility extension covers it; the queue's 90 s timeout is
  for a task that dies, not for a batch.
- **Fargate Spot** is about 70% cheaper and an at-least-once consumer tolerates interruption, but arm64 Spot
  capacity is regional and it was not tried; switching means a capacity provider strategy instead of `LaunchType`.
- **Pause it when idle:** `task archiver:scale N=0`; a deploy resets the count to the template's parameter.
- **Scrape or export the metrics:** `metrics: aws_cloudwatch` in the config plus `cloudwatch:PutMetricData` on
  the task role, if you want batch counts and write latency on a dashboard.
- **Pin and bump together:** the image tag in `src/archiver/Dockerfile` and the CLI version in `mise.toml`,
  so `bento lint`, `bento test` and the stream test run the release that deploys.
- **Watch the queue, not the task.** `ApproximateAgeOfOldestMessage` on the archive queue is the alarm that
  matters: it rises whether the task is dead, wedged or merely paused.
