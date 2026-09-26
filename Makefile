PY          ?= python3
STACK       ?= aws-eda-simple
REGION      ?= eu-west-1
BUS_NAME    ?= simple-eda-bus
EVENT_SOURCE ?= com.example.shop
GLUE_DATABASE ?= shop_events
GLUE_TABLE   ?= events
# Extra duckdb CLI flags, e.g. DUCKDB_FLAGS=-dark-mode if your terminal does not
# answer DuckDB's background-colour probe.
DUCKDB_FLAGS ?=
# Required for `make deploy`. Generate one with: export WEBHOOK_SECRET=$$(openssl rand -hex 24)
WEBHOOK_SECRET ?=

.PHONY: help install lint fmt test validate build deploy deploy-guided outputs url bucket table-location generate logs logs-events logs-firehose errors duckdb invoke-local invoke-local-transform empty-bucket delete

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

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
			EventSource=$(EVENT_SOURCE) \
			GlueDatabaseName=$(GLUE_DATABASE) \
			GlueTableName=$(GLUE_TABLE)

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

table-location: ## Print the S3 URI of the Iceberg table
	@aws cloudformation describe-stacks --stack-name $(STACK) --region $(REGION) \
		--query "Stacks[0].Outputs[?OutputKey=='TableLocation'].OutputValue" --output text

generate: ## Post fake events to the deployed webhook (requires WEBHOOK_SECRET)
	@test -n "$(WEBHOOK_SECRET)" || { echo "WEBHOOK_SECRET is not set"; exit 1; }
	WEBHOOK_URL=$$($(MAKE) -s url) WEBHOOK_SECRET=$(WEBHOOK_SECRET) \
		$(PY) generator/generate.py --interval 2 --batch-size 3 --count 10

logs: ## Tail the webhook Lambda logs
	sam logs -n WebhookFunction --stack-name $(STACK) --region $(REGION) --tail

logs-events: ## Tail the events delivered to the bus (via the catch-all rule)
	aws logs tail /aws/events/$(BUS_NAME) --region $(REGION) --follow --format short

logs-firehose: ## Tail the Firehose delivery logs (errors show up here first)
	aws logs tail /aws/kinesisfirehose/$(STACK)-events-to-iceberg --region $(REGION) --follow --format short

errors: ## List records Firehose could not deliver (should report none)
	@aws s3 ls "s3://$$($(MAKE) -s bucket)/errors/" --recursive --region $(REGION) \
		|| echo "no delivery errors"

duckdb: ## Query the Iceberg table with DuckDB (view: events)
	@command -v duckdb >/dev/null || { echo "duckdb not found. Install it: brew install duckdb"; exit 1; }
	@ACCOUNT=$$(aws sts get-caller-identity --query Account --output text 2>/dev/null) \
		&& test -n "$$ACCOUNT" \
		|| { echo "no AWS credentials in this shell - log in first, then retry"; exit 1; }; \
	echo "Attaching $(GLUE_DATABASE).$(GLUE_TABLE)... then you are at the DuckDB prompt."; \
	echo "Try: SELECT count(*) FROM events;   Leave with: .quit"; \
	duckdb $(DUCKDB_FLAGS) -cmd "INSTALL aws; INSTALL httpfs; INSTALL iceberg; LOAD aws; LOAD iceberg; \
		CREATE OR REPLACE SECRET (TYPE s3, PROVIDER credential_chain, REGION '$(REGION)'); \
		ATTACH '$$ACCOUNT' AS lake (TYPE iceberg, ENDPOINT_TYPE glue); \
		CREATE OR REPLACE VIEW events AS SELECT * FROM lake.$(GLUE_DATABASE).$(GLUE_TABLE);"

invoke-local: ## Invoke the webhook locally with events/post.json (needs env.json, see README)
	sam local invoke WebhookFunction -e events/post.json --env-vars env.json

invoke-local-transform: ## Invoke the Firehose transform locally (offline, needs env.json)
	sam local invoke FirehoseTransformFunction -e events/firehose.json --env-vars env.json

empty-bucket: ## Empty the lake bucket (CloudFormation cannot delete a bucket with objects)
	aws s3 rm "s3://$$($(MAKE) -s bucket)/" --recursive --region $(REGION)

delete: ## Delete the stack (run empty-bucket first)
	sam delete --stack-name $(STACK) --region $(REGION) --no-prompts
