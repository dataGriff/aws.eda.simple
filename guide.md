# Build this yourself: an event archive on AWS, for any events you like

This is the order I would build `aws.eda.simple` in from an empty directory - nine tasks, each ending
with something you can run and see working. The **why** notes are the decisions and gotchas that
actually cost time; read those even if you skip the commands.

The shop events in this repo (`order.created`, `order.updated`, `payment.received`) are an example,
not a requirement. The pipeline archives **any** JSON event that fits a small envelope, and the
domain-specific parts live in a handful of known places. The first section says exactly where, so you
can build this for your own events from the start rather than retrofit.

You need: a laptop with Docker and [mise](https://mise.jdx.dev), and an AWS account (only from task 8). The
local emulator, [Floci](https://floci.io), needs no account and no token.

```
producer → Function URL → webhook Lambda → EventBridge bus ─┬─→ CloudWatch Logs                       (watch)
                                                            └─→ SQS → archiver (Bento on Fargate) → S3   (keep)
                                                                       gzipped JSON Lines ─→ DuckDB / Harlequin
```

---

## Bring your own events: what is fixed and what is yours

**Fixed - the envelope.** Every event the webhook accepts is:

```json
{ "id": "<unique string>", "type": "<one of your types>", "timestamp": "<ISO-8601 with timezone>", "data": { ...anything... } }
```

The webhook validates only those four fields. `data` is passed through untouched, archived
byte-for-byte, and never given a schema. That is the property that makes the pipeline generic: a
new type or a new field inside `data` cannot break ingestion, archiving or the base `events` view.

**Yours - six places, and nothing else:**

| # | What | Where | Example here |
|---|---|---|---|
| 1 | The event **source** name | `EventSource` parameter in `template.yaml` (default), `samconfig.toml` overrides | `com.example.shop` |
| 2 | The **allowed types** | `ALLOWED_TYPES` in `src/webhook/app.py` | `order.created`, `order.updated`, `payment.received` |
| 3 | What each type's **`data` looks like** | `generator/generate.py`: `EVENT_TYPES` and one `make_*` function per type | orders with `items`, payments with `amount` |
| 4 | **Sample payloads** for local invokes and tests | `events/post.json`, `sample_event()` in `tests/helpers.py` | an `order.created` |
| 5 | **Domain views** over the payload | `queries/views.sql` below the `events` view, `queries/examples.sql` | `orders`, `order_items`, `payments` |
| 6 | The **contract table** in the README | README "Event contract" | the `type` row |

Everything else - template, archiver, `events` view, the local emulator, CI, the inspection tasks - is
domain-free and stays as it is.

**Worked example.** Say your events are IoT sensor readings: `sensor.reading` with
`{ "sensor_id", "metric", "value", "unit" }` and `sensor.alert` with `{ "sensor_id", "severity", "message" }`.

1. `EventSource` default → `com.example.sensors`; bus name to taste.
2. `ALLOWED_TYPES = frozenset({"sensor.reading", "sensor.alert"})`.
3. Generator: `EVENT_TYPES = ("sensor.reading", "sensor.alert")`, a `make_reading()` and `make_alert()` returning the `data` dicts, and `make_event()` dispatching on type. Keep `_envelope()`; it builds the fixed part.
4. `sample_event()` returns a `sensor.reading`; regenerate `events/post.json` from it.
5. Views: replace `orders`/`order_items`/`payments` with, say,
   ```sql
   CREATE OR REPLACE VIEW readings AS
   SELECT event_id, event_time,
          json_extract_string(detail, '$.data.sensor_id') AS sensor_id,
          json_extract_string(detail, '$.data.metric')    AS metric,
          CAST(json_extract_string(detail, '$.data.value') AS DOUBLE) AS value
   FROM   events WHERE event_type = 'sensor.reading';
   ```
   and rewrite `examples.sql` for the questions you actually have.
6. Update the README's contract row.

`task ci` tells you when you have missed one: `tests/test_generator.py` asserts the generator's types
equal the webhook's allowed set, and `tests/test_archive_query.py` runs the views over files the
archiver wrote. If a domain view references a payload field your generator no longer produces, the
read-side tests fail before anything is deployed.

---

## Task 1 - Tooling first, so every later step has one way to run

**Goal:** `task ci` runs lint, tests and template validation, identically on your machine and in CI.

**Do**

1. `mise.toml` pins the tools: `python = "3.12"`, `task`, `duckdb`, `awscli`, `aws-sam-cli`, `"pypi:aws-sam-cli-local"` (samlocal - despite the name it just points SAM at `localhost:4566`, so it works with any emulator on that port), `"pypi:harlequin"`. Run `mise install`.
2. `Taskfile.yml` with `install` (venv + `pip install -r requirements-dev.txt`), `lint` (ruff check + format check), `fmt`, `test` (pytest), `validate` (`sam validate --lint`) and `ci` calling the three checks.
3. `requirements-dev.txt`: `boto3`, `botocore`, `pytest`, `ruff`, `Faker`, `duckdb`. `pyproject.toml`: ruff at 100 columns with `E F I B UP S SIM`, pytest `pythonpath = ["src", "."]`.
4. `dotenv: [".env"]` at the top of the Taskfile and `.env` in `.gitignore`: secrets live there locally and come from the environment in CI. Commands, preconditions and called tasks all see them.
5. `.github/workflows/ci.yml`: checkout, `jdx/mise-action`, `task install`, `task ci`. That is the whole job.

**Check:** `task ci` passes with zero tests. Push; CI is green.

> **Why:** every command lives in the Taskfile and CI only calls tasks, so "works on my machine, fails in CI" has nowhere to hide; mise makes the tool versions part of the repo. **Gotcha:** a task's `env:` block applies to its own commands only, not to tasks it calls - pass values to helpers explicitly (you will hit this in task 7). **Gotcha:** keep emulator CLIs out of SAM's Python environment - `click` pins conflict. You will not need one: the emulator is a plain `docker run`.

---

## Task 2 - The envelope, and a webhook that enforces it

**Goal:** a Lambda behind a Function URL that accepts JSON events with a shared-secret header, validates the envelope, and publishes them to EventBridge - fully unit-tested without AWS.

**Do**

1. Write the envelope down first (the section above, and the README "Event contract"). Decide your `ALLOWED_TYPES` (yours #2) and your `EventSource` (yours #1).
2. `src/webhook/app.py` as small pure functions: `is_authorized` (constant-time compare, case-insensitive header), `parse_body` (one object or an array of up to 100), `validate_event` (the four envelope fields, `type` in `ALLOWED_TYPES`, tz-aware `timestamp`, `data` an object, size ≤ 256 KB), `to_entry` (`detail-type` = `type`, `detail` = the whole event, `source` fixed per deployment and never from the payload), `chunk` (PutEvents takes 10 at a time), `put_events` (collect partial failures), and a `lambda_handler` that strings them together.
3. `template.yaml`: `AWS::Events::EventBus`, the function with `WebhookSecret` as a `NoEcho` parameter and an inline policy allowing `events:PutEvents` on that one bus, plus an explicit `AWS::Lambda::Url` (`AuthType: NONE`) and the `AWS::Lambda::Permission` for `lambda:InvokeFunctionUrl`. SAM's `FunctionUrlConfig` sugar expands to exactly those two on AWS, but Floci's SAM transform does not expand it - declaring them yourself works in both places.
4. Tests with botocore's `Stubber` on the module-level client: 405 / 401 / 400 / 202 / 500, "one invalid event rejects the whole batch", "12 events use two PutEvents calls", partial failures reported with their original index. `sample_event()` in `tests/helpers.py` is your first payload (yours #4).

**Check:** `task ci` green. Resist deploying until task 8.

> **Why pure functions:** the handler is one assembly line; everything else is testable in a millisecond. **Gotcha:** set test environment variables with assignment, not `setdefault`, in `conftest.py` - a real `WEBHOOK_SECRET` exported for deploying will otherwise leak into the tests and turn every authorised request into a 401.

---

## Task 3 - A bus you can watch

**Goal:** every event on the bus shows up as one line in a CloudWatch log group, so you can see the pipeline work before writing any consumer.

**Do**

1. `AWS::Logs::LogGroup` `/aws/events/<bus>` with retention.
2. `AWS::Logs::ResourcePolicy` allowing `events.amazonaws.com` to `CreateLogStream` / `PutLogEvents` on it.
3. `AWS::Events::Rule` on the bus with `DependsOn` the policy, pattern `source: [<your EventSource>]`, target the log group. An empty pattern is rejected; matching on your fixed source is the catch-all.
4. Tasks: `outputs`, `url`, `logs`, `logs:events`.

**Check:** `task validate`.

> **Why this is its own task:** EventBridge targets fail *silently* when unauthorised - the rule deploys fine and delivers nothing; only the `FailedInvocations` metric moves. The resource policy is the lesson, and you meet it again with SQS in task 5.

---

## Task 4 - A producer that cannot drift from the contract

**Goal:** a generator that makes realistic events of every type you allow and POSTs them, with a test proving the webhook accepts everything it makes.

**Do**

1. `generator/generate.py` (yours #3): `EVENT_TYPES`, one function per type returning that type's `data`, `_envelope(type, data)` for the fixed part, `make_event`, `make_batch`. Use Faker for realistic values; if later events reference earlier ones (here, payments reference orders), keep a small memory of recent ids. Flags: `--url`/`--secret` (falling back to `WEBHOOK_URL`/`WEBHOOK_SECRET`), `--interval`, `--count`, `--batch-size`, `--types`, `--seed`, `--dry-run`, and `--host` to override the HTTP `Host` header - you will want it in task 7.
2. `tests/test_generator.py`: run `make_event()` for every type through the webhook's own `validate_event()`, and assert `set(EVENT_TYPES) == set(ALLOWED_TYPES)`.
3. Task `generate`: needs `WEBHOOK_SECRET`, reads the URL from the stack outputs.

**Check:** `python generator/generate.py --url x --secret x --dry-run --count 1` prints a valid event; `task test` includes the contract test.

> **Why:** the two ends of the contract are tested against each other, not against a fixture both could quietly disagree with. If you have a real producer, keep this generator anyway - it is what `task local:e2e` and CI use to prove the pipeline without your real system.

---

## Task 5 - Keep the events: SQS, an archiver, gzipped JSON Lines on S3

**Goal:** a second target on the same rule sends every event to a queue; a Bento stream in a container drains it and writes each batch as one file under `events/dt=YYYY-MM-DD/`. Bad messages are dead-lettered on their own. Nothing here knows what your events mean. The container runs on ECS Fargate in task 8 and next to Floci in task 7; this task builds and tests it without either.

**Do**

1. `AWS::S3::Bucket` with public access blocked and SSE-S3. Two `AWS::SQS::Queue`s: the archive queue (`VisibilityTimeout: 90` - long enough for a dead consumer's messages to come back, not a batch period; the consumer extends it while batching - 14-day retention, `RedrivePolicy` with `maxReceiveCount: 3`) and its dead-letter queue.
2. `AWS::SQS::QueuePolicy` allowing `events.amazonaws.com` `sqs:SendMessage`, with `aws:SourceArn` pinned to the rule. Build the rule ARN with `!Sub` (`arn:…:events:…:rule/<bus>/<rule-name>`) rather than `!GetAtt`, so the rule can `DependsOn` the policy without a cycle. Add the queue as the rule's second target.
3. `src/archiver/archiver.yaml`, a [Bento](https://warpstreamlabs.github.io/bento/) stream, configured by environment variables only. `input: aws_sqs` (`url: ${ARCHIVE_QUEUE_URL}`, `wait_time_seconds: 20`; leave `delete_message`, `update_visibility` and `reset_visibility` on - they are the delivery contract). One `mapping` processor: a body that is a JSON object becomes one line (the SQS bytes verbatim, re-serialised only if it spans lines); anything else `throw`s, which flags the message as errored. The `output` is `reject_errored` (errored messages are nacked back to SQS) around `retry` (`max_elapsed_time: 5m`) around `aws_s3` (`content_type: application/x-ndjson`, **no** `content_encoding`, `force_path_style_urls: ${S3_FORCE_PATH_STYLE:false}` for emulators) with `batching: count: ${BATCH_COUNT:100}, period: ${BATCH_PERIOD:30s}` and batch processors: `mapping: meta archived = batch_size()`, `archive: lines`, a `mapping` that appends the trailing newline and puts the object key `events/dt=…/<timestamp>Z-<uuid>.jsonl.gz` (UTC, no colons) in metadata, `compress: gzip`, `log`. Add an `http` section so `/ready` exists for the health check.
4. `src/archiver/Dockerfile`: `FROM ghcr.io/warpstreamlabs/bento:<version>`, `COPY archiver.yaml /archiver.yaml`, `CMD ["-c", "/archiver.yaml"]`. No `RUN`, so `docker build --platform linux/arm64` works on any host. Add `bento` to `mise.toml` (`github:warpstreamlabs/bento`, same version), and `moto[server]` to the dev requirements.
5. Tests. `src/archiver/archiver_bento_test.yaml` (`bento test ./src/...`) on the mapping: an envelope is kept byte for byte and not errored; a multi-line body is re-serialised onto one line; a bad body is errored and its neighbours are not. On the batching processors (`target_processors: /output/reject_errored/retry/output/aws_s3/batching/processors`): N lines in → gzip that round-trips to N lines plus a trailing newline, `archived` metadata, key format. Then `tests/test_archiver_stream.py`: start `python -m moto.server` on a free port, create the two queues with the template's redrive policy and a bucket, launch the real `bento -c src/archiver/archiver.yaml` with the same environment variables the task definition will have plus `AWS_ENDPOINT_URL`, `S3_FORCE_PATH_STYLE=true` and `BATCH_PERIOD=1s`, and assert: the object, its content type, its lines, the bad message in the dead-letter queue, the archive queue empty, and DuckDB reading the object through `views.sql`. A second test starts with a missing bucket and asserts the message stays in flight until the bucket appears and the write lands.
6. Tasks: `image:build` (`ARCH` defaults to `arm64`; the tag is a hash of the Dockerfile and the config), `logs:archiver`, `queues`, `errors`, `archive`, `bucket`, `empty-bucket`. Write each inspection command once as an internal task taking a `CLI` variable (`aws`, or `aws --endpoint-url …` with dummy credentials) and add thin public wrappers - the `local:` twins in task 7 then cost one line each.

**Check:** `task ci` green - the stream test takes about 16 seconds and needs neither AWS nor Docker.

> **Why Bento, and why a service:** the archiver is a stream job - poll, batch, compress, write, ack - and Bento says that in seventy lines of YAML with the SQS client, the S3 client, gzip and retries built in. In a Lambda it lost its input and its batching to the event source mapping (the previous branch); as a service it keeps them, and it is tested offline exactly as it runs. See `bento.md` for what that costs - a VPC, ECS and about $11 a month - and for why it is still not an improvement for a pipeline this small. **Why only the archiver:** Bento has no EventBridge output, so the webhook (`PutEvents`) stays Python. **Why no transform:** the archive is the bus envelope byte for byte. Files have no schema to match, so there is nothing to reshape and nothing a new payload field can break - this is what makes the pipeline domain-free; `bento.md` measures why writing Parquet here would not help. **Why at-least-once is fine:** a message is deleted only after its file is on S3, so a crash in between redelivers the batch and an event can appear in two files; task 6 dedupes on the envelope `id` at read time. **Why `retry` around `aws_s3`:** found by the stream test - without it the output nacks on the first failed write and a blip on S3 costs one of the queue's three receives. **Why `reject_errored`:** one unparseable message should not poison ninety-nine good ones, and SQS already knows how to retry and dead-letter one message. **Why no `ContentEncoding: gzip`:** DuckDB keys on the `.gz` extension; a transport-level encoding header invites double decompression. **Gotcha:** a nack resets visibility to zero and the Go SDK omits a zero `VisibilityTimeout` from the batch request; AWS reads the missing field as zero, moto rejects it - hence the test queues' 3-second visibility timeout.

---

## Task 6 - Query it: DuckDB, Harlequin, and SQL tested offline

**Goal:** `task duckdb` (or `task harlequin`) opens over the archive with an `events` view and your domain views loaded; the same SQL is exercised by pytest against files the archiver wrote.

**Do**

1. `queries/views.sql`. The **`events` view is domain-free**: `read_json(getvariable('archive'), format='newline_delimited', hive_partitioning=true, filename=true, columns={'id':'VARCHAR','detail-type':'VARCHAR','source':'VARCHAR','time':'TIMESTAMP','detail':'JSON'})`, renaming envelope fields to `bus_event_id`, `event_id` (`detail ->> '$.id'`), `event_type`, `event_time`, deriving `archived_at` from the file name, and `QUALIFY row_number() OVER (PARTITION BY id ORDER BY filename) = 1` to collapse duplicate deliveries. Below it, **your domain views** (yours #5): one `json_extract_string(detail, '$.data.…')` view per type, and `unnest(from_json(…, '["STRUCT(…)"]'))` for any array in a payload.
2. `queries/examples.sql` (yours #5): the questions you actually want answered, plus a lag query (`archived_at - event_time`).
3. Tasks: internal `_duckdb`, `_query`, `_verify`, `_harlequin` taking `SECRET_SQL` and `ARCHIVE`; public `duckdb`, `query`, `harlequin`. The caller runs `SET VARIABLE archive = 's3://…/events/**/*.jsonl.gz'` then `.read queries/views.sql`. Harlequin has no `.read`, so its task writes the boot SQL and `views.sql` into one gitignored init script and starts Harlequin with `--init-path`.
4. `tests/test_archive_query.py`: write `.jsonl.gz` files into `tmp_path` with the archiver's own `pack()`, point the `archive` variable at them with the `duckdb` Python package, execute `views.sql`, and assert the envelope mapping, the dedupe, your domain views, and that an unknown payload field breaks nothing. Extend the contract test to the end: generator → `to_entry` → envelope → archiver → file → `events` view returns the original event.

**Check:** `task test` runs the read-side tests with no AWS and no Docker.

> **Why a SQL variable:** the same `views.sql` then serves S3, the local emulator and a temp directory - which is what makes the SQL testable. **Gotcha:** `read_json(..., maximum_depth=1)` looks like the way to keep `detail` as JSON, but it makes *every* top-level value JSON-typed and `event_type = 'x'` stops matching; declare `columns=` explicitly. **Gotcha:** when scripting a count with the DuckDB CLI, take the last output line and refuse anything non-numeric - `-cmd` statements print `Success` rows.

---

## Task 7 - Run the whole thing in Floci, then in CI

**Goal:** `task local:e2e` starts Floci, deploys the stack, posts events and asserts DuckDB counts them - no AWS credentials, no tokens. CI runs the same task. Every inspection task gets a `local:` twin.

**Do**

1. `samconfig.toml` gains a `[local]` environment: same stack name, `resolve_s3 = true`, your `EventSource`, and a fixed non-secret `WebhookSecret` (it only ever guards localhost).
2. `local:up`: `docker run -d --name floci-main -p 4566:4566 -e FLOCI_DEFAULT_REGION=<region> -v /var/run/docker.sock:/var/run/docker.sock floci/floci:latest`, then poll `/_floci/health` (up in a few seconds). Precondition: no `floci-main` container already exists. `local:down`: remove the archiver container, `docker stop`, `docker rm -f`, then remove the `floci-<stack>-*` Lambda containers Floci leaves behind.
3. `local:deploy`: `samlocal build --build-dir .aws-sam/local` and `samlocal deploy --config-env local --config-file <repo>/samconfig.toml --template-file .aws-sam/local/template.yaml` - a separate build dir so local and AWS builds never overwrite each other, and `--config-file` because SAM looks for `samconfig.toml` next to the template. The `[local]` overrides include `ArchiverDeployment=none` (task 8 adds that parameter), so the stack has the queue and the bucket and no ECS. Then `local:archiver:up`: `task image:build ARCH=<host arch>` and `docker run -d --name floci-archiver --add-host host.docker.internal:host-gateway` with the queue URL from the stack outputs, the bucket, `AWS_ENDPOINT_URL=http://host.docker.internal:4566`, `S3_FORCE_PATH_STYLE=true`, the region and dummy credentials; wait for `Output type aws_s3 is now active` in its logs.
4. `local:generate`: posts to `http://localhost:4566/` with the Function URL's hostname in the `Host` header (the generator's `--host`). Floci's URLs are `<id>.lambda-url.<region>.localhost:4566`, which resolves without DNS on most systems, so the plain URL works too; the header route is kept because it is emulator-independent.
5. `local:outputs`, `local:resources`, `local:logs`, `local:logs:events`, `local:queues`, `local:errors`, `local:archive`, `local:duckdb`, `local:query`, `local:harlequin`, `local:health`: the same internal helpers with `CLI: aws --endpoint-url http://localhost:4566` and dummy credentials, and the DuckDB secret `KEY_ID 'test', SECRET 'test', ENDPOINT 'localhost:4566', USE_SSL false, URL_STYLE 'path'`. `local:logs:archiver` and `local:archiver` are `docker logs` and `docker ps` on the container.
6. `local:verify` asserts the DuckDB count equals `EXPECT`; `local:e2e` chains up → deploy → generate → wait 40s → verify, with a deferred `local:down`.
7. CI: a second job with no secrets: `task install`, `task local:e2e`.

**Check:** `task local:e2e` ends with `events archived: 30 (expected 30)`. Push; both CI jobs green. Then leave Floci up and try `task local:resources`, `task local:queues`, `task local:archive`, `task local:logs:archiver FOLLOW=`.

> **Why Floci works here:** CloudFormation with the SAM transform, Lambda in real Docker containers (on the host's architecture, so the `arm64` webhook runs on x86 runners), Function URLs, EventBridge → SQS, SQS with visibility timeouts and dead-letter queues, S3 - all present, starting in about three seconds with no account. **Why the archiver runs beside Floci rather than in it:** Floci lists ECS, ECR and VPC resources as emulated, but a plain `docker run` of the identical image, pointed at Floci by `AWS_ENDPOINT_URL`, has fewer moving parts and proves the thing that matters - that nothing in the config is emulator-specific; the stream test in task 5 pins that with a queue URL whose host does not resolve. **Parity gaps in 2.1.0, characterised by running this pipeline:** (1) `FunctionUrlConfig` is not expanded - hence the explicit resources in task 2; (2) the EventBridge → CloudWatch Logs target is unsupported, so `task local:logs:events` is empty. The Lambda archiver's two gaps (event source mapping properties dropped, `ReportBatchItemFailures` ignored) went away with the Lambda. None affect AWS.

---

## Task 8 - Deploy to AWS: the archiver on Fargate

**Goal:** the same template live in your account, with the archiver as one Fargate task, verified the same way.

**Do**

1. In `template.yaml`, behind a parameter `ArchiverDeployment` (`fargate` | `none`) and a condition: the smallest network Fargate can use without a NAT gateway - a VPC, two public subnets in two zones, an internet gateway, a route table with a default route, an S3 gateway endpoint, a security group with no ingress; an `AWS::ECS::Cluster`; a log group; an execution role with `AmazonECSTaskExecutionRolePolicy` and a task role allowing `sqs:ReceiveMessage/DeleteMessage/ChangeMessageVisibility/GetQueueAttributes` on the queue and `s3:PutObject` on `events/*`; an `AWS::ECS::TaskDefinition` (`FARGATE`, `awsvpc`, `256`/`512`, `ARM64`, `Image: !Ref ArchiverImage`, the environment variables from task 5, `awslogs`, a `HealthCheck` of `wget -q -O /dev/null http://127.0.0.1:4195/ready`, `StopTimeout: 30`); and an `AWS::ECS::Service` (`DesiredCount: !Ref ArchiverDesiredCount`, `AssignPublicIp: ENABLED`, the circuit breaker with rollback, `DependsOn` the route and the endpoint). Outputs for the cluster, the service and the log group, conditional too.
2. Tasks: `image:push` (`aws ecr describe-repositories || create-repository`, `get-login-password | docker login`, tag, push, print the URI); `deploy` depends on `build` and `image:push` and passes `ArchiverImage=<uri>`; `archiver` (`describe-services` → desired/running/pending, rollout state, last event); `archiver:scale N=…`; `delete` also deletes the ECR repository.
3. `WEBHOOK_SECRET=$(openssl rand -hex 24)` - export it or put it in `.env`, and keep it.
4. `task deploy` → `task outputs` → `task archiver` (expect 1 running, `COMPLETED`). Then `task generate`, wait 45 seconds, `task errors` (expect "no delivery errors"), `task archive`, `task logs:archiver FOLLOW=`, `task query`.
5. Try the curl checks in the README: 405, 401, 400, 202.

**Check:** `task query` prints the same tables you saw locally.

> **Why last:** by now every component has been proven locally; the AWS deploy tests IAM, the queue policy, the network and CloudFormation itself. If the archive stays empty, `task queues` tells you whether events reach SQS, `task archiver` whether the task is running, and `task logs:archiver` what it thought of them. **Why the repository is created by the Taskfile, not the stack:** the service needs the image at creation time, and the image cannot be pushed before the repository exists - a stack that owns both never converges on first deploy. **Why a content-hashed tag:** an unchanged archiver redeploys as a no-op; `latest` would redeploy the task every time and hide what changed. **Why `DependsOn` the route:** without it CloudFormation starts the service before the task can reach ECR, the pull fails, the circuit breaker trips, and the stack rolls back for a reason that looks like a permissions problem. **Why a public IP and no NAT:** a NAT gateway costs more per month than the task; the S3 endpoint keeps the archive traffic private and the security group keeps everything else out.

---

## Task 9 - Operate and tear down

**Goal:** know what to look at when something is wrong, and leave nothing behind. Every command below has a `local:` twin.

- Pipeline lag: `task query` → the lag query compares `archived_at` (from the file name) with `event_time`. Expect roughly the batching window.
- Something rejected: `task errors` shows the dead-letter depth; `task logs:archiver` shows `body is not a JSON object` from the mapping. Read the message from the DLQ, fix the producer, redrive.
- Nothing arriving: `task resources` (everything `CREATE_COMPLETE`?), `task archiver` (a task running, rollout `COMPLETED`?), `task queues` (are events reaching SQS at all, are they stuck in flight?), then the rule's `FailedInvocations` metric - the silent-failure signal for both targets. The alarm worth having is `ApproximateAgeOfOldestMessage` on the archive queue: it rises whether the archiver is dead, wedged or paused.
- Paying for nothing: `task archiver:scale N=0` stops the task; the queue holds two weeks of events. `N=1` or the next deploy starts it again.
- Tear down: `task empty-bucket` (deliberately separate - it deletes the archive), then `task delete` (stack and ECR repository). `task local:down` for Floci and the local archiver.

---

## Swapping the domain later - the checklist

If you built the shop version first and now want your own events, change these and nothing else:

1. `template.yaml` → `EventSource` default; `samconfig.toml` → the two `EventSource=` overrides.
2. `src/webhook/app.py` → `ALLOWED_TYPES`.
3. `generator/generate.py` → `EVENT_TYPES`, one `make_*` per type, the dispatch in `make_event`.
4. `tests/helpers.py` → `sample_event()`; regenerate `events/post.json`.
5. `queries/views.sql` → the views below `events`; `queries/examples.sql`.
6. README → the `type` row of the contract table, and any example SQL.

Then `task ci`. The generator/webhook contract test and the read-side tests are the two places a missed
step surfaces - before anything is deployed.

---

## What you could add next

- **Bigger, fewer files**: `BATCH_COUNT=1000`, `BATCH_PERIOD=5m` in the task definition. The queue extends visibility while a batch waits, so nothing else changes.
- **Partitioning by hour or by type** if the day prefixes get large: change the key in the `name_object` mapping; DuckDB picks up the extra Hive key automatically. Per-type partitions mean one file per type per batch - see `bento.md`.
- **Validation per event type**: a `json_schema` processor per `detail-type` in the same config, with failures routed to a `quarantine/` prefix instead of nacked. Still no code.
- **Typed Parquet or Iceberg tables** built *from* the archive with DuckDB, PyIceberg or Athena, per type, if you want columns, snapshots or a catalog. Ingestion does not change - `bento.md` measures why writing Parquet in the archiver would not help.
- **A FIFO queue** if you ever need ordering per entity; you lose the 100-message batches.
- **Authentication beyond a shared secret**: `AuthType: AWS_IAM` on the Function URL, or an API Gateway in front for WAF and throttling. The webhook's validation does not change.
