PY          ?= python3
STACK       ?= aws-eda-simple
REGION      ?= eu-west-1
BUS_NAME    ?= simple-eda-bus
EVENT_SOURCE ?= com.example.shop
# Required for `make deploy`. Generate one with: export WEBHOOK_SECRET=$$(openssl rand -hex 24)
WEBHOOK_SECRET ?=
# Extra duckdb CLI flags, e.g. DUCKDB_FLAGS=-dark-mode if your terminal does not
# answer DuckDB's background-colour probe.
DUCKDB_FLAGS ?=
QUERY_FILE ?= queries/examples.sql
# LocalStack (make local-*). The auth token, if you have one, comes from the environment.
LOCAL_ENDPOINT ?= http://localhost:4566
LOCAL_SECRET   ?= local-dev-secret-0123456789
EXPECT         ?= 30

# DuckDB session set-up shared by every query target, so they cannot drift apart.
# Callers set the `archive` variable to the glob they want, then read the views.
DUCKDB_INSTALL = INSTALL httpfs; LOAD httpfs;
DUCKDB_AWS     = CREATE OR REPLACE SECRET (TYPE s3, PROVIDER credential_chain, REGION '$(REGION)');
DUCKDB_LOCAL   = CREATE OR REPLACE SECRET (TYPE s3, KEY_ID 'test', SECRET 'test', REGION '$(REGION)', \
	ENDPOINT 'localhost:4566', USE_SSL false, URL_STYLE 'path');
define DUCKDB_HINT
echo "Views: events, orders, order_items, payments (queries/views.sql). Leave with: .quit"; \
echo "Try: SELECT event_type, count(*) FROM events GROUP BY 1;"
endef

.PHONY: help install lint fmt test validate build deploy deploy-guided outputs url bucket generate logs logs-events logs-archiver errors duckdb query invoke-local invoke-local-archiver empty-bucket delete local-up local-down local-deploy local-url local-generate local-duckdb local-query local-verify

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

install: ## Create .venv and install dev dependencies
	$(PY) -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements-dev.txt

lint: ## Run ruff lint + format check
	ruff check .
	ruff format --check .

fmt: ## Auto-fix lint and format
	ruff check --fix .
	ruff format .

test: ## Run unit tests
	pytest

validate: ## Validate the SAM template
	sam validate --lint

build: ## Build the SAM application
	sam build

deploy: build ## Deploy the stack (requires WEBHOOK_SECRET in the environment)
	@test -n "$(WEBHOOK_SECRET)" || { echo "WEBHOOK_SECRET is not set. Try: export WEBHOOK_SECRET=\$$(openssl rand -hex 24)"; exit 1; }
	sam deploy \
		--stack-name $(STACK) \
		--region $(REGION) \
		--parameter-overrides \
			WebhookSecret=$(WEBHOOK_SECRET) \
			BusName=$(BUS_NAME) \
			EventSource=$(EVENT_SOURCE)

deploy-guided: build ## Interactive first-time deploy
	sam deploy --guided --stack-name $(STACK) --region $(REGION)

outputs: ## Show stack outputs
	aws cloudformation describe-stacks --stack-name $(STACK) --region $(REGION) \
		--query 'Stacks[0].Outputs' --output table

url: ## Print the webhook URL
	@aws cloudformation describe-stacks --stack-name $(STACK) --region $(REGION) \
		--query "Stacks[0].Outputs[?OutputKey=='WebhookUrl'].OutputValue" --output text

bucket: ## Print the lake bucket name
	@aws cloudformation describe-stacks --stack-name $(STACK) --region $(REGION) \
		--query "Stacks[0].Outputs[?OutputKey=='LakeBucketName'].OutputValue" --output text

generate: ## Post fake events to the deployed webhook (requires WEBHOOK_SECRET)
	@test -n "$(WEBHOOK_SECRET)" || { echo "WEBHOOK_SECRET is not set"; exit 1; }
	WEBHOOK_URL=$$($(MAKE) -s url) WEBHOOK_SECRET=$(WEBHOOK_SECRET) \
		$(PY) generator/generate.py --interval 2 --batch-size 3 --count 10

logs: ## Tail the webhook Lambda logs
	sam logs -n WebhookFunction --stack-name $(STACK) --region $(REGION) --tail

logs-events: ## Tail the events delivered to the bus (via the catch-all rule)
	aws logs tail /aws/events/$(BUS_NAME) --region $(REGION) --follow --format short

logs-archiver: ## Tail the archiver Lambda logs (one line per batch written)
	sam logs -n ArchiverFunction --stack-name $(STACK) --region $(REGION) --tail

errors: ## Show how many messages the archiver rejected (dead-letter queue depth, should be 0)
	@DLQ=$$(aws cloudformation describe-stacks --stack-name $(STACK) --region $(REGION) \
		--query "Stacks[0].Outputs[?OutputKey=='DeadLetterQueueUrl'].OutputValue" --output text); \
	N=$$(aws sqs get-queue-attributes --queue-url "$$DLQ" --region $(REGION) \
		--attribute-names ApproximateNumberOfMessages --query Attributes.ApproximateNumberOfMessages --output text); \
	if [ "$$N" = "0" ]; then echo "no delivery errors"; else echo "$$N message(s) in the dead-letter queue: $$DLQ"; exit 1; fi

duckdb: ## Open the DuckDB prompt over the deployed archive, views loaded
	@command -v duckdb >/dev/null || { echo "duckdb not found. Install it: brew install duckdb"; exit 1; }
	@$(DUCKDB_HINT); \
	duckdb $(DUCKDB_FLAGS) \
		-cmd "$(DUCKDB_INSTALL) $(DUCKDB_AWS) SET VARIABLE archive = 's3://$$($(MAKE) -s bucket)/events/**/*.jsonl.gz';" \
		-cmd ".read queries/views.sql"

query: ## Run a SQL file over the deployed archive (default QUERY_FILE=queries/examples.sql)
	@duckdb $(DUCKDB_FLAGS) -box \
		-cmd "$(DUCKDB_INSTALL) $(DUCKDB_AWS) SET VARIABLE archive = 's3://$$($(MAKE) -s bucket)/events/**/*.jsonl.gz';" \
		-cmd ".read queries/views.sql" \
		-c ".read $(QUERY_FILE)"

invoke-local: ## Invoke the webhook locally with events/post.json (needs env.json, see README)
	sam local invoke WebhookFunction -e events/post.json --env-vars env.json

invoke-local-archiver: ## Invoke the archiver locally with events/sqs.json (needs env.json, writes to real S3)
	sam local invoke ArchiverFunction -e events/sqs.json --env-vars env.json

empty-bucket: ## Empty the lake bucket (CloudFormation cannot delete a bucket with objects)
	aws s3 rm "s3://$$($(MAKE) -s bucket)/" --recursive --region $(REGION)

delete: ## Delete the stack (run empty-bucket first)
	sam delete --stack-name $(STACK) --region $(REGION) --no-prompts

# ------------------------------------------------------------------ LocalStack
# The whole pipeline on your machine: no AWS account, no WEBHOOK_SECRET.
# Needs docker, the localstack CLI and samlocal (see requirements-dev.txt).

local-up: ## Start LocalStack in the background (export LOCALSTACK_AUTH_TOKEN first if you have one)
	LAMBDA_IGNORE_ARCHITECTURE=1 localstack start -d
	@echo "waiting for $(LOCAL_ENDPOINT) ..."; \
	for i in $$(seq 1 60); do \
		curl -sf $(LOCAL_ENDPOINT)/_localstack/health >/dev/null && { echo "LocalStack is up"; exit 0; }; \
		sleep 2; \
	done; echo "LocalStack did not come up in time"; exit 1

local-down: ## Stop LocalStack (deletes everything in it)
	localstack stop

local-deploy: ## Build and deploy the stack into LocalStack
	samlocal build
	samlocal deploy --config-env local

local-url: ## Print the LocalStack webhook URL
	@aws --endpoint-url $(LOCAL_ENDPOINT) cloudformation describe-stacks --stack-name $(STACK) --region $(REGION) \
		--query "Stacks[0].Outputs[?OutputKey=='WebhookUrl'].OutputValue" --output text

# Function URLs are routed by hostname. Posting to localhost with the URL's host in the
# Host header works even where *.localhost.localstack.cloud does not resolve.
local-generate: ## Post fake events to the LocalStack webhook
	@HOST=$$($(MAKE) -s local-url | sed -E 's#^https?://([^/:]+).*#\1#'); \
	echo "POSTing to $(LOCAL_ENDPOINT)/ as $$HOST"; \
	WEBHOOK_URL=$(LOCAL_ENDPOINT)/ WEBHOOK_HOST=$$HOST WEBHOOK_SECRET=$(LOCAL_SECRET) \
		$(PY) generator/generate.py --interval 1 --batch-size 3 --count 10

local-duckdb: ## Open the DuckDB prompt over the LocalStack archive, views loaded
	@$(DUCKDB_HINT); \
	duckdb $(DUCKDB_FLAGS) \
		-cmd "$(DUCKDB_INSTALL) $(DUCKDB_LOCAL) SET VARIABLE archive = 's3://$(STACK)-lake-000000000000-$(REGION)/events/**/*.jsonl.gz';" \
		-cmd ".read queries/views.sql"

local-query: ## Run a SQL file over the LocalStack archive (default QUERY_FILE=queries/examples.sql)
	@duckdb $(DUCKDB_FLAGS) -box \
		-cmd "$(DUCKDB_INSTALL) $(DUCKDB_LOCAL) SET VARIABLE archive = 's3://$(STACK)-lake-000000000000-$(REGION)/events/**/*.jsonl.gz';" \
		-cmd ".read queries/views.sql" \
		-c ".read $(QUERY_FILE)"

local-verify: ## Assert the LocalStack archive holds EXPECT events (default 30); used by CI
	@N=$$(duckdb -noheader -csv \
		-cmd "$(DUCKDB_INSTALL) $(DUCKDB_LOCAL) SET VARIABLE archive = 's3://$(STACK)-lake-000000000000-$(REGION)/events/**/*.jsonl.gz';" \
		-cmd ".read queries/views.sql" \
		-c "SELECT count(*) FROM events;" | tail -1); \
	echo "events archived: $$N (expected $(EXPECT))"; test "$$N" = "$(EXPECT)"
