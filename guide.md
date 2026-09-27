# Build this yourself: an event-driven archive on AWS, in nine tasks

This is the order I would build `aws.eda.simple` in if starting from an empty directory. Each task
ends with something you can run and see working. The **why** notes are the decisions and gotchas
that actually cost time along the way - read those even if you skip the commands.

You need: a laptop with Docker, [mise](https://mise.jdx.dev), an AWS account (only from task 7),
and a free [LocalStack](https://app.localstack.cloud) auth token (from task 6).

```
generator → Function URL → webhook Lambda → EventBridge bus ─┬─→ CloudWatch Logs
                                                             └─→ SQS → archiver Lambda → S3 (gzipped JSON Lines) → DuckDB
```

---

## Task 1 - Tooling first, so every later step has one way to run

**Goal:** `task ci` runs lint, tests and template validation, identically on your machine and in CI.

**Do**

1. `mise.toml` pins the tools: `python = "3.12"`, `task`, `duckdb`, `awscli`, `aws-sam-cli`, `"pypi:aws-sam-cli-local"`. Run `mise install`.
2. `Taskfile.yml` with `install` (venv + `pip install -r requirements-dev.txt`), `lint` (ruff check + format check), `fmt`, `test` (pytest), `validate` (`sam validate --lint`) and `ci` that calls the first three checks.
3. `requirements-dev.txt`: `boto3`, `botocore`, `pytest`, `ruff`, `Faker`, `duckdb`. `pyproject.toml`: ruff at 100 columns with `E F I B UP S SIM`, pytest `pythonpath = ["src", "."]`.
4. `.github/workflows/ci.yml`: checkout, `jdx/mise-action`, `task install`, `task ci`. That is the whole job.
5. `dotenv: [".env"]` at the top of the Taskfile, `.env` in `.gitignore`: secrets live there locally and come from the environment in CI. Commands, preconditions and called tasks all see them. One thing does *not* carry across: a task's `env:` block applies to its own commands only, not to tasks it calls - pass values to helpers explicitly.

**Check:** `task ci` passes with zero tests. Push; CI is green.

> **Why:** every command lives in the Taskfile and CI only calls tasks, so "works on my machine, fails in CI" has nowhere to hide. mise makes the tool versions part of the repo rather than of your laptop. Use the `pypi:` backend for `samlocal` - and do **not** put the `localstack` CLI in the same Python environment as SAM: their `click` pins conflict and the CLI dies with `Parameter.__init__() got an unexpected keyword argument 'deprecated'`. You will not need that CLI anyway (task 6).

---

## Task 2 - The event contract and a webhook that enforces it

**Goal:** a Lambda behind a Function URL that accepts JSON events with a shared-secret header, validates them, and publishes them to EventBridge - fully unit-tested without AWS.

**Do**

1. Write the contract down first (README "Event contract"): `id`, `type` from a fixed set, `timestamp` with a timezone, `data` as an object. One object or an array of up to 100.
2. `src/webhook/app.py` as small pure functions: `is_authorized` (constant-time compare, case-insensitive header), `parse_body`, `validate_event`, `to_entry`, `chunk` (PutEvents takes 10 at a time), `put_events` (collect partial failures), and a `lambda_handler` that strings them together. `detail-type` is the event `type`; `source` is fixed per deployment and never taken from the payload.
3. `template.yaml`: `AWS::Events::EventBus`, the function with `FunctionUrlConfig: AuthType: NONE` and an inline policy allowing `events:PutEvents` on that one bus.
4. Tests with botocore's `Stubber` wrapping the module-level client: 405 / 401 / 400 / 202 / 500, "one invalid event rejects the whole batch", "12 events use two PutEvents calls", partial failures reported with the original index.

**Check:** `task ci` green. `task deploy` is for task 7 - resist deploying yet.

> **Why pure functions:** the handler is one assembly line; everything else is testable in a millisecond. **Gotcha:** set test environment variables with assignment, not `setdefault`, in `conftest.py` - a real `WEBHOOK_SECRET` exported for deploying will otherwise leak into the tests and turn every authorised request into a 401.

---

## Task 3 - A bus you can watch

**Goal:** every event on the bus shows up as one line in a CloudWatch log group, so you can see the pipeline work before writing any consumer.

**Do**

1. `AWS::Logs::LogGroup` `/aws/events/<bus>` with retention.
2. `AWS::Logs::ResourcePolicy` allowing `events.amazonaws.com` to `CreateLogStream` / `PutLogEvents` on it.
3. `AWS::Events::Rule` on the bus with `DependsOn` the policy, pattern `source: [<your source>]`, target the log group. An empty pattern is rejected; matching on your fixed source is the catch-all.
4. Tasks: `logs`, `logs:events`, `url`, `outputs`.

**Check:** `task validate`. Deploying comes in task 7.

> **Why this is its own task:** EventBridge targets fail *silently* when unauthorised - the rule deploys fine and delivers nothing; only the `FailedInvocations` metric moves. The resource policy is the lesson, and you will meet it again with SQS in task 5.

---

## Task 4 - Fake data that cannot drift from the contract

**Goal:** a generator that produces realistic `order.created` / `order.updated` / `payment.received` events and POSTs them, with a test proving the webhook accepts everything it makes.

**Do**

1. `generator/generate.py`: Faker for names and emails, an `OrderMemory` so updates and payments reference orders that exist, `--interval`, `--count`, `--batch-size`, `--types`, `--seed`, `--dry-run`. `--url` / `--secret` fall back to `WEBHOOK_URL` / `WEBHOOK_SECRET`. Add `--host` to override the HTTP `Host` header - you will want it in task 6.
2. `tests/test_generator.py`: feed `make_event()` output through the webhook's own `validate_event()` for every type, and assert the generator's type list equals the webhook's allowed set.
3. Task `generate` that needs `WEBHOOK_SECRET` and reads the URL from the stack outputs.

**Check:** `python generator/generate.py --url x --secret x --dry-run --count 1` prints a valid payload; `task test` includes the contract test.

> **Why:** the two ends of the contract are tested against each other, not against a shared fixture that both could quietly disagree with.

---

## Task 5 - Keep the events: SQS, an archiver, gzipped JSON Lines on S3

**Goal:** a second target on the same rule sends every event to a queue; a Lambda drains it in batches and writes each batch as one file under `events/dt=YYYY-MM-DD/`. Bad messages are dead-lettered on their own.

**Do**

1. `AWS::S3::Bucket` with public access blocked and SSE-S3. Two `AWS::SQS::Queue`s: the archive queue (`VisibilityTimeout` six times the Lambda timeout, 14-day retention, `RedrivePolicy` with `maxReceiveCount: 3`) and its dead-letter queue.
2. `AWS::SQS::QueuePolicy` allowing `events.amazonaws.com` `sqs:SendMessage`, with `aws:SourceArn` pinned to the rule. Build the rule ARN with `!Sub` (`arn:…:events:…:rule/<bus>/<rule-name>`) instead of `!GetAtt`, so the rule can `DependsOn` the policy without a cycle. Add the queue as the rule's second target.
3. `src/archiver/app.py`: `parse_messages` (a body that is a JSON object becomes one compact line; anything else is a failure by `messageId`), `object_key` (`events/dt=…/<timestamp>Z-<batch>.jsonl.gz`, UTC, no colons), `pack` (gzip, trailing newline), `write_archive` (`put_object`, **no** `ContentEncoding` header), and a handler returning `{"batchItemFailures": [...]}`.
4. In the template: `Events: Archive: Type: SQS` with `BatchSize: 100`, `MaximumBatchingWindowInSeconds: 30`, `FunctionResponseTypes: [ReportBatchItemFailures]`; an inline policy for `s3:PutObject` on `events/*` only.
5. Tests: N good bodies → one `put_object` whose gzip body round-trips to N lines; a bad body → its id in `batchItemFailures` and the good ones still written; empty batch → no call; key format; S3 `ClientError` propagates.
6. Tasks: `logs:archiver`, `errors` (dead-letter queue depth), `bucket`, `empty-bucket`.

**Check:** `task ci` green.

> **Why no transform:** the archive is the bus envelope byte for byte. Files have no schema to match, so there is nothing to reshape and nothing that a new payload field can break. **Why at-least-once is fine:** if the S3 write fails the whole batch is redelivered, so an event can appear in two files - task 6 dedupes on the envelope `id` at read time. **Why `ReportBatchItemFailures`:** one unparseable message should not poison ninety-nine good ones. **Why no `ContentEncoding: gzip`:** DuckDB keys on the `.gz` extension; a transport-level encoding header invites double decompression.

---

## Task 6 - Query it with DuckDB, and test the SQL offline

**Goal:** `task duckdb` opens a prompt with `events`, `orders`, `order_items` and `payments` views over the archive; the same SQL is exercised by pytest against files the archiver wrote.

**Do**

1. `queries/views.sql`. The `events` view reads `read_json(getvariable('archive'), format='newline_delimited', hive_partitioning=true, filename=true, columns={...})` with explicit column types - `detail` as `JSON` - and `QUALIFY row_number() OVER (PARTITION BY id ORDER BY filename) = 1` to collapse duplicate deliveries. Then `orders`, `payments` (plain `json_extract_string`) and `order_items` (the `items` array unnested via `from_json(..., '["STRUCT(sku VARCHAR, qty INTEGER, unit_price DOUBLE)"]')`).
2. `queries/examples.sql`: a handful of worked queries, including a reconciliation of item totals against each order's own total.
3. Tasks: internal `_duckdb`, `_query`, `_verify` taking `SECRET_SQL` and `ARCHIVE` vars; public `duckdb`, `query` on top. The caller runs `SET VARIABLE archive = 's3://…/events/**/*.jsonl.gz'` then `.read queries/views.sql`.
4. `tests/test_archive_query.py`: write `.jsonl.gz` files into `tmp_path` with the archiver's own `pack()`, point the `archive` variable at them with the `duckdb` Python package, execute `views.sql`, and assert the mapping, the dedupe, the unnest, the cross-type join, and that an unknown payload field breaks nothing.

**Check:** `task test` runs the read-side tests with no AWS and no Docker.

> **Why a SQL variable:** the same `views.sql` then serves S3, LocalStack and a temp directory - which is what makes the SQL testable. **Gotcha:** `read_json(..., maximum_depth=1)` looks like the way to keep `detail` as JSON, but it makes *every* top-level value JSON-typed, so `event_type = 'order.created'` fails. Declare `columns=` explicitly instead. **Gotcha:** `iceberg_scan`-style catalog-free reads and DuckDB's `Success` rows both bit us once; when scripting a count, take the last line and refuse anything non-numeric.

---

## Task 7 - Run the whole thing in LocalStack, then in CI

**Goal:** `task local:e2e` starts LocalStack, deploys the stack, posts 30 events and asserts DuckDB counts 30 - with no AWS credentials. CI runs the same task.

**Do**

1. `samconfig.toml` gains a `[local]` environment: same stack name, `resolve_s3 = true`, and a fixed non-secret `WebhookSecret` (it only ever guards localhost).
2. Tasks: `local:up` (`docker run -d --name localstack-main -p 4566:4566 -e LAMBDA_IGNORE_ARCHITECTURE=1 -e LOCALSTACK_AUTH_TOKEN -v /var/run/docker.sock:/var/run/docker.sock localstack/localstack`, then poll `/_localstack/health`), `local:down`, `local:deploy` (`samlocal build && samlocal deploy --config-env local`), `local:url`, `local:generate`, `local:duckdb`, `local:query`, `local:verify`, and `local:e2e` chaining them with a deferred `local:down`. LocalStack tasks set the dummy `AWS_ACCESS_KEY_ID=test` credentials themselves.
3. `local:generate` posts to `http://localhost:4566/` with the Function URL's hostname in the `Host` header (the generator's `--host`), because that is how LocalStack routes Function URLs and it sidesteps resolvers that refuse the `*.localhost.localstack.cloud` wildcard.
4. DuckDB against LocalStack: `CREATE SECRET (TYPE s3, KEY_ID 'test', SECRET 'test', REGION '…', ENDPOINT 'localhost:4566', USE_SSL false, URL_STYLE 'path')`.
5. CI: a second job with the `LOCALSTACK_AUTH_TOKEN` repository secret, `task install`, `task local:e2e`.

**Check:** put `LOCALSTACK_AUTH_TOKEN=…` in a gitignored `.env` (the Taskfile's `dotenv:` loads it) and `task local:e2e` ends with `events archived: 30 (expected 30)`. Push; both CI jobs green. Then send a `not json` message to the queue by hand and watch it reach the dead-letter queue after three visibility timeouts while good events keep flowing.

> **Why LocalStack works here at all:** SQS, Lambda, S3, EventBridge and Logs are all emulated with high fidelity, including SQS batching and partial-batch responses. The earlier Firehose-to-Iceberg version of this pipeline could not run locally - Firehose has no Iceberg destination in LocalStack and Glue is a control-plane mock - which is a large part of why this design exists. **Gotcha:** LocalStack's image exits with "License activation failed" without an auth token, even on the free tier; make `local:up` fail fast with a clear message. **Gotcha:** the functions are `arm64`; `LAMBDA_IGNORE_ARCHITECTURE=1` lets an x86 runner execute them.

---

## Task 8 - Deploy to AWS

**Goal:** the same template live in your account, verified the same way.

**Do**

1. `export WEBHOOK_SECRET=$(openssl rand -hex 24)` and keep it.
2. `task deploy` → `task outputs`. Then `task generate`, wait 45 seconds, `task errors` (expect "no delivery errors"), `task logs:events`, `task query`.
3. Try the curl checks in the README: 405, 401, 400, 202.

**Check:** `task query` prints the same tables you saw locally, and reconciliation is N/N.

> **Why last:** by now every component has been proven locally; the AWS deploy only tests IAM, the queue policy and CloudFormation itself. If the archive stays empty, `task logs:archiver` and `task errors` tell you which of those it was.

---

## Task 9 - Operate and tear down

**Goal:** know what to look at when something is wrong, and leave nothing behind.

- Pipeline lag: `task query` → the lag query compares `archived_at` (from the file name) with `event_time`. Expect roughly the batching window.
- Something rejected: `task errors` shows the dead-letter depth; `task logs:archiver` shows `message_rejected` with the message id. Read the message from the DLQ, fix the producer, redrive.
- Nothing arriving: check the rule's `FailedInvocations` metric first - it is the silent-failure signal for both targets.
- Tear down: `task empty-bucket` (deliberately separate - it deletes the archive), then `task delete`. `task local:down` for LocalStack.

---

## What you could add next

- **Partitioning by hour** if the day prefixes get large: change `object_key`; DuckDB picks up the extra Hive key automatically.
- **An Iceberg table** built *from* the archive with DuckDB or PyIceberg, if you want snapshots, time travel or a catalog. Ingestion does not change.
- **A FIFO queue** if you ever need ordering per order id; you lose the 100-message batches.
- **More event types**: add them to the webhook's allowed set and the generator - the archive and the `events` view need nothing.
