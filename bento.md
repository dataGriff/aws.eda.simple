# Bento for the Lambda: the experiment, and whether it improved the solution

This branch replaces the archiver Lambda's Python with a [Bento](https://warpstreamlabs.github.io/bento/) stream,
running as a Lambda on Bento's own Lambda build. The webhook Lambda stays Python, for a reason that turned out to
be decisive (below). Everything else - template, bus, rule, queue, bucket, DuckDB views, Floci, CI - is unchanged.

**Verdict:** for this repo, no - it is not an improvement overall. The archiver gets a little more declarative and
gains built-in retries, but pays with a 230 MB binary that sits 20 MB under Lambda's hard limit, an
architecture-specific build, a second language, and offline tests that cannot reach the S3 write. And Bento cannot
take over the webhook at all, so it does not unify the pipeline. It would become an improvement if the archiver
grew into a real stream job (several sinks, Parquet, enrichment) or ran as a long-lived Bento service instead of a
Lambda. Details and numbers follow; the branch is kept as the working reference for either of those futures.

## What was built

```
SQS batch (Lambda event)  ─→  bootstrap (Bento Lambda build, provided.al2023, arm64)
                                 └─ archiver.yaml
                                     pipeline: mapping (lines + failed ids + key)  →  compress gzip  →  log
                                     output:   switch
                                                 nothing archivable → sync_response {batchItemFailures}
                                                 otherwise          → broker: aws_s3, then sync_response
                              ←  {"batchItemFailures": [...]}
```

| | Python (`feat/floci`) | Bento (this branch) |
|---|---|---|
| Archiver source | `src/archiver/app.py`, 108 lines (75 without comments/blank) | `src/archiver/archiver.yaml`, 95 lines (59 without comments/blank) |
| Tests | 14 pytest tests, botocore `Stubber` for `put_object` | 8 `bento test` cases; the S3 output is not reachable offline |
| Build | `sam build` zips one file | `sam build` runs a Makefile: download the pinned release, sha256, unzip |
| Deployment package | a few KB | 71 MB zipped, **229 MB unzipped** (arm64); Lambda's limit is 250 MB |
| Runtime | `python3.12`, boto3 from the runtime | `provided.al2023`, one static Go binary, every Bento component compiled in |
| Memory | 256 MB | 512 MB (more CPU for the cold start) |
| Cold start | tens of ms | 0.38 s init on an x86 sandbox under the Runtime Interface Emulator; **not measured on AWS** |
| Warm invocation | a few ms | 5-10 ms under the emulator |
| Archive line | envelope parsed and re-serialised (`json.dumps`, compact) | the SQS body bytes verbatim (re-serialised only if it spans lines) |
| File name | `<timestamp>Z-<lambda-request-id>.jsonl.gz` | `<timestamp>Z-<uuid>.jsonl.gz` - Bento does not see the Lambda context |
| S3 write fails | raises at once; SQS redelivers the batch | Bento retries with backoff until the 60 s timeout; then SQS redelivers |
| Bad message | id in `batchItemFailures`, rest archived | same |
| Local emulation (Floci) | nothing special | binary must match the host: `BENTO_ARCH`, own build dir and own cache dir |

The behaviour SQS, S3 and DuckDB see is the same: one gzipped JSON Lines object per batch, `dt=` partition,
timestamp in the name, `batchItemFailures` for the messages that could not be archived, `queries/views.sql`
untouched. Verified by running the real `bootstrap` binary under the
[Lambda Runtime Interface Emulator](https://github.com/aws/aws-lambda-runtime-interface-emulator) with moto as
S3: two envelopes → one object whose lines equal the SQS bodies, `Content-Type: application/x-ndjson`, no
`Content-Encoding`; one good + one bad → `{"batchItemFailures":[{"itemIdentifier":"bad"}]}` and the good one
archived; nothing archivable → no object, every id reported; empty batch → `{"batchItemFailures":[]}`; missing
bucket → retried until the timeout. CI's Floci job is the end-to-end check, and it passed on the first run:
30 events posted, 30 counted by DuckDB from the archive, on an x86 runner with the `amd64` build Floci needs,
with no Floci-specific configuration - the injected `AWS_ENDPOINT_URL` was enough.

## Is the architecture right for Bento?

Yes, with these points settled deliberately rather than by accident:

- **Bento's Lambda build has no `input`.** The Lambda event is the message, and `input`, `buffer` and `http`
  sections are ignored. So Bento's own batching, buffers and metrics endpoint are unavailable here; **batching
  stays in the SQS event source mapping** (`BatchSize: 100`, `MaximumBatchingWindowInSeconds: 30`), exactly as
  before. Bento only turns one batch into one file.
- **The response is whatever reaches `sync_response`.** `ReportBatchItemFailures` needs `{"batchItemFailures":
  [...]}` back, every time, including when nothing was written - hence the `switch` output with a
  `sync_response`-only case. Without an output Bento would return the processed message itself, which is the gzip
  bytes, and SQS would treat that as "all succeeded".
- **`fan_out_sequential`, S3 first.** The answer is produced only after the object exists. With plain `fan_out`
  the response would be stored before the write finished; the handler still waits for every output to ack, so the
  outcome is the same, but sequential says what is meant.
- **Retries live in Bento now.** The broker retries a failed `aws_s3` write with backoff until the Lambda times
  out (60 s), then SQS redelivers. Transient S3 errors no longer cost a redelivery; a persistent one costs a full
  timeout per receive, so a broken bucket takes about three minutes plus visibility timeouts to reach the
  dead-letter queue instead of seconds. Acceptable for an archiver; the `events` view still dedupes on envelope id.
- **Credentials and endpoint come from the environment.** Empty `region`/`credentials` mean the SDK default
  chain, so the execution role works on AWS and Floci's injected `AWS_ENDPOINT_URL` and credentials work locally,
  with no Floci-specific config. Confirmed under the emulator with `AWS_ENDPOINT_URL` pointed at moto.
- **The artifact is native.** `provided.al2023` + `Architectures: [arm64]` on AWS. Floci runs functions on the
  host's architecture, so `task local:deploy` builds with `BENTO_ARCH=<host>` into its own build *and* cache
  directory - SAM's build cache keys on the source hash and would otherwise reuse an arm64 binary on x86.
- **IAM is unchanged.** `s3:PutObject` on `events/*` plus the SQS permissions SAM grants; Bento needs nothing more.
- **Config discovery is explicit.** `BENTO_CONFIG_PATH=/var/task/archiver.yaml` rather than relying on the default
  `./bento.yaml` search, so the file keeps its name and `bento test` finds `archiver_bento_test.yaml` next to it.

What would be *more* Bento-shaped is not a Lambda at all: a long-running Bento with an `aws_sqs` input (its own
long polling and batching by count *and* period, no 30 s ceiling) and the same `aws_s3` output, on ECS/Fargate or
a small VM. That removes the Lambda and the event source mapping and unlocks Bento's buffers, metrics and
`parquet_encode`, at the price of running a service. For this volume Lambda is the right shape, and Bento inside
Lambda is Bento with its input and buffering taken away.

## Why the webhook is still Python

The webhook's job is `PutEvents` on the custom bus. **Bento has no EventBridge output**: its AWS outputs are
`aws_s3`, `aws_sqs`, `aws_sns`, `aws_kinesis`, `aws_kinesis_firehose` and `aws_dynamodb`, and the generic
`http_client` output cannot sign requests with SigV4. There is no way for a Bento webhook to publish to the bus
directly. The workable designs all change the architecture:

1. **Bento webhook → SQS → EventBridge Pipe → bus.** The Pipe's event bus target can take `detail-type`,
   `source` and `time` from the message with dynamic path parameters, so the envelope on the bus would be the same.
   Costs: a queue, a pipe and its role; `202` would mean "queued", not "on the bus"; `PutEvents`' per-entry
   partial-failure report disappears; the secret check loses `hmac.compare_digest` (Bloblang has no constant-time
   compare - comparing SHA-256 digests is the usual workaround); every 4xx path needs a `switch` output that
   answers without sending. And **Floci does not emulate an event-bus target for Pipes** (its Pipes targets are
   Lambda, SQS, SNS, Kinesis and Step Functions), so `task local:e2e` and CI could not prove it. That last point
   is why it was not built on this branch: a change the repo cannot test locally is against its own rules.
2. **API Gateway → EventBridge direct integration, no Lambda.** Validation moves to a JSON schema request
   validator; the Function URL, the secret header and the Lambda disappear. A good design, but it is "remove the
   Lambda", not "use Bento for the Lambda".

So Bento cannot be the single runtime for this pipeline. That matters for the verdict: the promise of one
declarative tool for both edges does not hold here, and a second language for one 100-line function is a cost.

## What Bento improved

- **Less to write and own.** The S3 client, gzip, JSON logging, backoff retries and the batch-to-response
  plumbing are configuration. The mapping is the only logic, and it reads like the README's description of it.
- **Retries in the right place.** A blip on S3 is retried inside the invocation instead of round-tripping through
  SQS's visibility timeout.
- **A verbatim archive.** Keeping the SQS body bytes was the natural thing to write in Bloblang; the Python version
  re-serialised. (Go's `json` sorts object keys, so re-serialising in Bento would have reordered them - the
  verbatim path avoids that too.)
- **Room to grow without code.** A second sink (Kafka, another bucket, a Parquet copy via `parquet_encode`), a
  filter, or an enrichment is a few lines in the same file; the same config lints and unit-tests offline.
- **Upgrades are a version bump** in `src/archiver/Makefile` and `mise.toml`, plus two checksums.

## What it cost

- **Weight.** 229 MB unzipped on arm64 (245 MB on amd64) against Lambda's 250 MB limit; a future release that
  grows by 10% no longer deploys without a custom, component-trimmed Bento build. 71 MB uploaded on every `sam
  deploy`. Cold start is measurably slower (0.38 s init on a fast x86 box; AWS at 512 MB will be slower) - harmless
  behind a 30 s batching window, but it is there, and memory was doubled to help it.
- **A build that needs the network.** `sam build` downloads a GitHub release and needs `curl`, `unzip`, `make`.
  It is cached and checksummed, but it is no longer hermetic.
- **Architecture-specific artifacts.** The Python zip ran anywhere; the Bento zip is per-arch, which is what forced
  `BENTO_ARCH`, the separate local cache dir, and the note that `task invoke:archiver` needs an arm64 host or QEMU.
- **Weaker offline tests.** `bento test` exercises processors, not outputs. The old suite stubbed `put_object` and
  checked bucket, key, body and content type; now the write is only checked by an emulator run (by hand) and by
  Floci in CI.
- **Lost context.** No Lambda request id (file names use a uuid); log lines are Bento's JSON, not ours.
- **Another language.** Bloblang is small but it is one more thing to know, with error messages that point at
  config paths (`root.pipeline.processors.0`) rather than lines of code, and no debugger - the loop is `bento
  test`, then the emulator, then Floci.
- **Simplicity.** The repo is called *simple* and had one runtime. It now has two, and the one that arrived cannot
  do the other job.

## If you keep it

- Pin and verify: the Makefile pins the version and both sha256s. Bump all three together.
- Watch the size: `unzip -l` the release; the 250 MB line is close. A trimmed build (`go build` with only the
  components used: `aws_s3`, `sync_response`, `switch`, `broker`, `mapping`, `compress`, `log`) would be a few MB
  and remove the concern, at the cost of owning a Go build.
- Measure the cold start on AWS once deployed: `task logs:archiver FOLLOW=` shows `Init Duration` in the
  `REPORT` lines. If it matters, SnapStart is not available for `provided.*` runtimes; provisioned concurrency is.
