# Build this yourself: an event archive on AWS, for any events you like

This is the order I would build `aws.eda.simple` in from an empty directory - nine tasks, each ending
with something you can run and see working. The **why** notes are the decisions and gotchas that
actually cost time; read those even if you skip the commands.

The shop events in this repo (`order.created`, `order.updated`, `payment.received`) are an example,
not a requirement. The pipeline archives **any** JSON event that fits a small envelope, and the
domain-specific parts live in a handful of known places. The first section says exactly where, so you
can build this for your own events from the start rather than retrofit.

You need: a laptop with Docker, [mise](https://mise.jdx.dev), a free
[LocalStack](https://app.localstack.cloud) auth token (from task 7), and an AWS account (only from task 8).

```
producer → Function URL → webhook Lambda → EventBridge bus ─┬─→ CloudWatch Logs               (watch)
                                                            └─→ SQS → archiver Lambda → S3     (keep)
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
| 4 | **Sample payloads** for local invokes and tests | `events/post.json`, `events/sqs.json`, `sample_event()` in `tests/helpers.py` | an `order.created` |
| 5 | **Domain views** over the payload | `queries/views.sql` below the `events` view, `queries/examples.sql` | `orders`, `order_items`, `payments` |
| 6 | The **contract table** in the README | README "Event contract" | the `type` row |

Everything else - template, archiver, `events` view, LocalStack, CI, the inspection tasks - is
domain-free and stays as it is.

**Worked example.** Say your events are IoT sensor readings: `sensor.reading` with
`{ "sensor_id", "metric", "value", "unit" }` and `sensor.alert` with `{ "sensor_id", "severity", "message" }`.

1. `EventSource` default → `com.example.sensors`; bus name to taste.
2. `ALLOWED_TYPES = frozenset({"sensor.reading", "sensor.alert"})`.
3. Generator: `EVENT_TYPES = ("sensor.reading", "sensor.alert")`, a `make_reading()` and `make_alert()` returning the `data` dicts, and `make_event()` dispatching on type. Keep `_envelope()`; it builds the fixed part.
4. `sample_event()` returns a `sensor.reading`; regenerate `events/post.json` and `events/sqs.json` from it.
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

1. `mise.toml` pins the tools: `python = "3.12"`, `task`, `duckdb`, `awscli`, `aws-sam-cli`, `"pypi:aws-sam-cli-local"` (samlocal), `"pypi:harlequin"`. Run `mise install`.
2. `Taskfile.yml` with `install` (venv + `pip install -r requirements-dev.txt`), `lint` (ruff check + format check), `fmt`, `test` (pytest), `validate` (`sam validate --lint`) and `ci` calling the three checks.
3. `requirements-dev.txt`: `boto3`, `botocore`, `pytest`, `ruff`, `Faker`, `duckdb`. `pyproject.toml`: ruff at 100 columns with `E F I B UP S SIM`, pytest `pythonpath = ["src", "."]`.
4. `dotenv: [".env"]` at the top of the Taskfile and `.env` in `.gitignore`: secrets live there locally and come from the environment in CI. Commands, preconditions and called tasks all see them.
5. `.github/workflows/ci.yml`: checkout, `jdx/mise-action`, `task install`, `task ci`. That is the whole job.

**Check:** `task ci` passes with zero tests. Push; CI is green.

> **Why:** every command lives in the Taskfile and CI only calls tasks, so "works on my machine, fails in CI" has nowhere to hide; mise makes the tool versions part of the repo. **Gotcha:** a task's `env:` block applies to its own commands only, not to tasks it calls - pass values to helpers explicitly (you will hit this in task 7). **Gotcha:** do not install the `localstack` CLI into the same Python environment as SAM - their `click` pins conflict. You will not need that CLI.

---

## Task 2 - The envelope, and a webhook that enforces it

**Goal:** a Lambda behind a Function URL that accepts JSON events with a shared-secret header, validates the envelope, and publishes them to EventBridge - fully unit-tested without AWS.

**Do**

1. Write the envelope down first (the section above, and the README "Event contract"). Decide your `ALLOWED_TYPES` (yours #2) and your `EventSource` (yours #1).
2. `src/webhook/app.py` as small pure functions: `is_authorized` (constant-time compare, case-insensitive header), `parse_body` (one object or an array of up to 100), `validate_event` (the four envelope fields, `type` in `ALLOWED_TYPES`, tz-aware `timestamp`, `data` an object, size ≤ 256 KB), `to_entry` (`detail-type` = `type`, `detail` = the whole event, `source` fixed per deployment and never from the payload), `chunk` (PutEvents takes 10 at a time), `put_events` (collect partial failures), and a `lambda_handler` that strings them together.
3. `template.yaml`: `AWS::Events::EventBus`, the function with `FunctionUrlConfig: AuthType: NONE`, `WebhookSecret` as a `NoEcho` parameter, and an inline policy allowing `events:PutEvents` on that one bus.
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

**Goal:** a second target on the same rule sends every event to a queue; a Lambda drains it in batches and writes each batch as one file under `events/dt=YYYY-MM-DD/`. Bad messages are dead-lettered on their own. Nothing here knows what your events mean.

**Do**

1. `AWS::S3::Bucket` with public access blocked and SSE-S3. Two `AWS::SQS::Queue`s: the archive queue (`VisibilityTimeout` six times the Lambda timeout, 14-day retention, `RedrivePolicy` with `maxReceiveCount: 3`) and its dead-letter queue.
2. `AWS::SQS::QueuePolicy` allowing `events.amazonaws.com` `sqs:SendMessage`, with `aws:SourceArn` pinned to the rule. Build the rule ARN with `!Sub` (`arn:…:events:…:rule/<bus>/<rule-name>`) rather than `!GetAtt`, so the rule can `DependsOn` the policy without a cycle. Add the queue as the rule's second target.
3. `src/archiver/app.py`: `parse_messages` (a body that is a JSON object becomes one compact line; anything else is a failure by `messageId`), `object_key` (`events/dt=…/<timestamp>Z-<batch>.jsonl.gz`, UTC, no colons), `pack` (gzip, trailing newline), `write_archive` (`put_object`, **no** `ContentEncoding` header), and a handler returning `{"batchItemFailures": [...]}`.
4. In the template: `Events: Archive: Type: SQS` with `BatchSize: 100`, `MaximumBatchingWindowInSeconds: 30`, `FunctionResponseTypes: [ReportBatchItemFailures]`; an inline policy for `s3:PutObject` on `events/*` only.
5. Tests: N good bodies → one `put_object` whose gzip body round-trips to N lines; a bad body → its id in `batchItemFailures` and the good ones still written; empty batch → no call; key format; S3 `ClientError` propagates. `events/sqs.json` is a two-message batch for `task invoke:archiver` (yours #4).
6. Tasks: `logs:archiver`, `queues`, `errors`, `archive`, `bucket`, `empty-bucket`. Write each inspection command once as an internal task taking a `CLI` variable (`aws`, or `aws --endpoint-url …` with dummy credentials) and add thin public wrappers - the `local:` twins in task 7 then cost one line each.

**Check:** `task ci` green.

> **Why no transform:** the archive is the bus envelope byte for byte. Files have no schema to match, so there is nothing to reshape and nothing a new payload field can break - this is what makes the pipeline domain-free. **Why at-least-once is fine:** if the S3 write fails the whole batch is redelivered, so an event can appear in two files; task 6 dedupes on the envelope `id` at read time. **Why `ReportBatchItemFailures`:** one unparseable message should not poison ninety-nine good ones. **Why no `ContentEncoding: gzip`:** DuckDB keys on the `.gz` extension; a transport-level encoding header invites double decompression.

---

## Task 6 - Query it: DuckDB, Harlequin, and SQL tested offline

**Goal:** `task duckdb` (or `task harlequin`) opens over the archive with an `events` view and your domain views loaded; the same SQL is exercised by pytest against files the archiver wrote.

**Do**

1. `queries/views.sql`. The **`events` view is domain-free**: `read_json(getvariable('archive'), format='newline_delimited', hive_partitioning=true, filename=true, columns={'id':'VARCHAR','detail-type':'VARCHAR','source':'VARCHAR','time':'TIMESTAMP','detail':'JSON'})`, renaming envelope fields to `bus_event_id`, `event_id` (`detail ->> '$.id'`), `event_type`, `event_time`, deriving `archived_at` from the file name, and `QUALIFY row_number() OVER (PARTITION BY id ORDER BY filename) = 1` to collapse duplicate deliveries. Below it, **your domain views** (yours #5): one `json_extract_string(detail, '$.data.…')` view per type, and `unnest(from_json(…, '["STRUCT(…)"]'))` for any array in a payload.
2. `queries/examples.sql` (yours #5): the questions you actually want answered, plus a lag query (`archived_at - event_time`).
3. Tasks: internal `_duckdb`, `_query`, `_verify`, `_harlequin` taking `SECRET_SQL` and `ARCHIVE`; public `duckdb`, `query`, `harlequin`. The caller runs `SET VARIABLE archive = 's3://…/events/**/*.jsonl.gz'` then `.read queries/views.sql`. Harlequin has no `.read`, so its task writes the boot SQL and `views.sql` into one gitignored init script and starts Harlequin with `--init-path`.
4. `tests/test_archive_query.py`: write `.jsonl.gz` files into `tmp_path` with the archiver's own `pack()`, point the `archive` variable at them with the `duckdb` Python package, execute `views.sql`, and assert the envelope mapping, the dedupe, your domain views, and that an unknown payload field breaks nothing. Extend the contract test to the end: generator → `to_entry` → envelope → archiver → file → `events` view returns the original event.

**Check:** `task test` runs the read-side tests with no AWS and no Docker.

> **Why a SQL variable:** the same `views.sql` then serves S3, LocalStack and a temp directory - which is what makes the SQL testable. **Gotcha:** `read_json(..., maximum_depth=1)` looks like the way to keep `detail` as JSON, but it makes *every* top-level value JSON-typed and `event_type = 'x'` stops matching; declare `columns=` explicitly. **Gotcha:** when scripting a count with the DuckDB CLI, take the last output line and refuse anything non-numeric - `-cmd` statements print `Success` rows.

---

## Task 7 - Run the whole thing in LocalStack, then in CI

**Goal:** `task local:e2e` starts LocalStack, deploys the stack, posts events and asserts DuckDB counts them - no AWS credentials. CI runs the same task. Every inspection task gets a `local:` twin.

**Do**

1. `samconfig.toml` gains a `[local]` environment: same stack name, `resolve_s3 = true`, your `EventSource`, and a fixed non-secret `WebhookSecret` (it only ever guards localhost).
2. `local:up`: `docker run -d --name localstack-main -p 4566:4566 -e LAMBDA_IGNORE_ARCHITECTURE=1 -e LOCALSTACK_AUTH_TOKEN -v /var/run/docker.sock:/var/run/docker.sock localstack/localstack`, then poll `/_localstack/health`. Preconditions: the token is set, and no `localstack-main` container already exists. `local:down`: `docker stop` then `docker rm -f`, then remove any `localstack-main-lambda-*` containers.
3. `local:deploy`: `samlocal build --build-dir .aws-sam/local` and `samlocal deploy --config-env local --config-file <repo>/samconfig.toml --template-file .aws-sam/local/template.yaml` - a separate build dir so local and AWS builds never overwrite each other, and `--config-file` because SAM looks for `samconfig.toml` next to the template.
4. `local:generate`: posts to `http://localhost:4566/` with the Function URL's hostname in the `Host` header (the generator's `--host`), because that is how LocalStack routes Function URLs and it sidesteps resolvers that refuse the `*.localhost.localstack.cloud` wildcard.
5. `local:outputs`, `local:resources`, `local:logs`, `local:logs:events`, `local:logs:archiver`, `local:queues`, `local:errors`, `local:archive`, `local:duckdb`, `local:query`, `local:harlequin`, `local:health`: the same internal helpers with `CLI: aws --endpoint-url http://localhost:4566` and dummy credentials, and the DuckDB secret `KEY_ID 'test', SECRET 'test', ENDPOINT 'localhost:4566', USE_SSL false, URL_STYLE 'path'`.
6. `local:verify` asserts the DuckDB count equals `EXPECT`; `local:e2e` chains up → deploy → generate → wait 45s → verify, with a deferred `local:down`.
7. CI: a second job with the `LOCALSTACK_AUTH_TOKEN` repository secret, `task install`, `task local:e2e`.

**Check:** put `LOCALSTACK_AUTH_TOKEN=…` in `.env`; `task local:e2e` ends with `events archived: 30 (expected 30)`. Push; both CI jobs green. Then leave LocalStack up, `task local:resources`, `task local:queues`, `task local:archive`, and send a `not json` message to the queue by hand: `task local:logs:archiver FOLLOW=` shows `message_rejected` while good events keep flowing, and after three visibility timeouts `task local:errors` reports it in the DLQ.

> **Why LocalStack works here at all:** SQS, Lambda, S3, EventBridge and Logs are all emulated with high fidelity, including SQS batching and partial-batch responses. The Firehose-to-Iceberg version this replaced could not run locally at all. **Gotcha:** LocalStack's image exits with "License activation failed" without an auth token, even on the free tier. **Gotcha:** the functions are `arm64`; `LAMBDA_IGNORE_ARCHITECTURE=1` lets an x86 runner execute them. **Gotcha:** tasks that call other tasks do not pass their `env:` along - the CI job found this as `NoCredentials` where the local run had inherited credentials from the shell.

---

## Task 8 - Deploy to AWS

**Goal:** the same template live in your account, verified the same way.

**Do**

1. `WEBHOOK_SECRET=$(openssl rand -hex 24)` - export it or put it in `.env`, and keep it.
2. `task deploy` → `task outputs` → `task resources`. Then `task generate`, wait 45 seconds, `task errors` (expect "no delivery errors"), `task archive`, `task logs:events FOLLOW=`, `task query`.
3. Try the curl checks in the README: 405, 401, 400, 202.

**Check:** `task query` prints the same tables you saw locally.

> **Why last:** by now every component has been proven locally; the AWS deploy only tests IAM, the queue policy and CloudFormation itself. If the archive stays empty, `task queues` tells you whether events reach SQS and `task logs:archiver` tells you what the Lambda thought of them.

---

## Task 9 - Operate and tear down

**Goal:** know what to look at when something is wrong, and leave nothing behind. Every command below has a `local:` twin.

- Pipeline lag: `task query` → the lag query compares `archived_at` (from the file name) with `event_time`. Expect roughly the batching window.
- Something rejected: `task errors` shows the dead-letter depth; `task logs:archiver` shows `message_rejected` with the message id. Read the message from the DLQ, fix the producer, redrive.
- Nothing arriving: `task resources` (everything `CREATE_COMPLETE`?), `task queues` (are events reaching SQS at all?), then the rule's `FailedInvocations` metric - the silent-failure signal for both targets.
- Tear down: `task empty-bucket` (deliberately separate - it deletes the archive), then `task delete`. `task local:down` for LocalStack.

---

## Swapping the domain later - the checklist

If you built the shop version first and now want your own events, change these and nothing else:

1. `template.yaml` → `EventSource` default; `samconfig.toml` → the two `EventSource=` overrides.
2. `src/webhook/app.py` → `ALLOWED_TYPES`.
3. `generator/generate.py` → `EVENT_TYPES`, one `make_*` per type, the dispatch in `make_event`.
4. `tests/helpers.py` → `sample_event()`; regenerate `events/post.json` and `events/sqs.json`.
5. `queries/views.sql` → the views below `events`; `queries/examples.sql`.
6. README → the `type` row of the contract table, and any example SQL.

Then `task ci`. The generator/webhook contract test and the read-side tests are the two places a missed
step surfaces - before anything is deployed.

---

## What you could add next

- **Partitioning by hour** if the day prefixes get large: change `object_key`; DuckDB picks up the extra Hive key automatically.
- **An Iceberg table** built *from* the archive with DuckDB or PyIceberg, if you want snapshots, time travel or a catalog. Ingestion does not change.
- **A FIFO queue** if you ever need ordering per entity; you lose the 100-message batches.
- **Authentication beyond a shared secret**: `AuthType: AWS_IAM` on the Function URL, or an API Gateway in front for WAF and throttling. The webhook's validation does not change.
