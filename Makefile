PY           ?= python3
TF           ?= terraform
TF_DIR       ?= terraform
NAME         ?= aws-eda-simple
REGION       ?= eu-west-1
BUS_NAME     ?= simple-eda-bus
EVENT_SOURCE ?= com.example.shop
# Required for `make deploy`. Generate one with: export WEBHOOK_SECRET=$$(openssl rand -hex 24)
WEBHOOK_SECRET ?=

# Non-secret settings are passed as -var flags; the secret goes in via TF_VAR_webhook_secret
# so it never appears in `ps` output or shell history.
TF_VARS = -var name=$(NAME) -var aws_region=$(REGION) -var bus_name=$(BUS_NAME) -var event_source=$(EVENT_SOURCE)

# ---- LocalStack: the whole pipeline on your machine, no AWS account (make local-e2e).
# LocalStack needs an auth token even on its free tier: export LOCALSTACK_AUTH_TOKEN.
LOCAL_ENDPOINT ?= http://localhost:4566
LOCAL_IMAGE    ?= localstack/localstack
LOCAL_CONTAINER ?= localstack-main
# Deliberately not secret: it only ever guards a webhook on localhost.
LOCAL_SECRET   ?= local-dev-secret-0123456789
# 10 POSTs x 3 events; local-verify expects exactly this many in the events log group.
LOCAL_COUNT    ?= 10
LOCAL_BATCH    ?= 3
LOCAL_AWS_ENV   = AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=$(REGION)
# Separate Terraform workspace so LocalStack state never mixes with the AWS one.
LOCAL_TF        = TF_WORKSPACE=local TF_VAR_webhook_secret=$(LOCAL_SECRET) $(TF) -chdir=$(TF_DIR)
LOCAL_TF_VARS   = $(TF_VARS) -var aws_endpoint_url=$(LOCAL_ENDPOINT)

.PHONY: help install lint fmt test validate init plan deploy outputs url generate logs logs-events invoke-local delete \
        local-up local-down local-deploy local-outputs local-url local-generate local-verify local-logs local-logs-events local-destroy local-e2e

help: ## Show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Create .venv and install dev dependencies
	$(PY) -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements-dev.txt

lint: ## Run ruff lint + format check, and terraform fmt check
	ruff check .
	ruff format --check .
	$(TF) -chdir=$(TF_DIR) fmt -check -recursive

fmt: ## Auto-fix lint and format (Python and Terraform)
	ruff check --fix .
	ruff format .
	$(TF) -chdir=$(TF_DIR) fmt -recursive

test: ## Run unit tests
	pytest

init: ## Download Terraform providers (run once, and after changing versions.tf)
	$(TF) -chdir=$(TF_DIR) init

validate: init ## Validate the Terraform configuration
	$(TF) -chdir=$(TF_DIR) validate

plan: init ## Show what `make deploy` would change (requires WEBHOOK_SECRET in the environment)
	@test -n "$(WEBHOOK_SECRET)" || { echo "WEBHOOK_SECRET is not set. Try: export WEBHOOK_SECRET=\$$(openssl rand -hex 24)"; exit 1; }
	TF_VAR_webhook_secret=$(WEBHOOK_SECRET) $(TF) -chdir=$(TF_DIR) plan $(TF_VARS)

deploy: init ## Deploy the stack with terraform apply (requires WEBHOOK_SECRET in the environment)
	@test -n "$(WEBHOOK_SECRET)" || { echo "WEBHOOK_SECRET is not set. Try: export WEBHOOK_SECRET=\$$(openssl rand -hex 24)"; exit 1; }
	TF_VAR_webhook_secret=$(WEBHOOK_SECRET) $(TF) -chdir=$(TF_DIR) apply -auto-approve $(TF_VARS)

outputs: ## Show stack outputs
	$(TF) -chdir=$(TF_DIR) output

url: ## Print the webhook URL
	@$(TF) -chdir=$(TF_DIR) output -raw webhook_url

generate: ## Post fake events to the deployed webhook (requires WEBHOOK_SECRET)
	@test -n "$(WEBHOOK_SECRET)" || { echo "WEBHOOK_SECRET is not set"; exit 1; }
	WEBHOOK_URL=$$($(MAKE) -s url) WEBHOOK_SECRET=$(WEBHOOK_SECRET) \
		$(PY) generator/generate.py --interval 2 --batch-size 3 --count 10

logs: ## Tail the webhook Lambda logs
	aws logs tail /aws/lambda/$(NAME)-webhook --region $(REGION) --follow --format short

logs-events: ## Tail the events delivered to the bus (via the catch-all rule)
	aws logs tail /aws/events/$(BUS_NAME) --region $(REGION) --follow --format short

invoke-local: ## Invoke the handler in-process with events/post.json (needs env.json, see README)
	$(PY) scripts/invoke_local.py --event events/post.json --env-file env.json

delete: ## Destroy the stack (a placeholder secret is used if WEBHOOK_SECRET is unset)
	TF_VAR_webhook_secret=$(if $(WEBHOOK_SECRET),$(WEBHOOK_SECRET),placeholder-secret-for-destroy) \
		$(TF) -chdir=$(TF_DIR) destroy -auto-approve $(TF_VARS)

# ------------------------------------------------------------------ LocalStack

local-up: ## Start LocalStack in Docker and wait until it is healthy (needs LOCALSTACK_AUTH_TOKEN)
	@test -n "$$LOCALSTACK_AUTH_TOKEN" || { echo "LOCALSTACK_AUTH_TOKEN is not set. LocalStack needs one even on its free tier: https://app.localstack.cloud"; exit 1; }
	@test -z "$$(docker ps -aq --filter name=^$(LOCAL_CONTAINER)$$)" || { echo "a $(LOCAL_CONTAINER) container already exists - run 'make local-down' first"; exit 1; }
	docker run -d --name $(LOCAL_CONTAINER) -p 4566:4566 \
		-e LAMBDA_IGNORE_ARCHITECTURE=1 -e LOCALSTACK_AUTH_TOKEN \
		-v /var/run/docker.sock:/var/run/docker.sock $(LOCAL_IMAGE)
	@echo "waiting for $(LOCAL_ENDPOINT) ..."
	@for i in $$(seq 1 60); do \
		curl -sf $(LOCAL_ENDPOINT)/_localstack/health >/dev/null && { echo "LocalStack is up"; exit 0; }; \
		sleep 2; \
	done; echo "LocalStack did not come up in time"; docker logs $(LOCAL_CONTAINER) | tail -20; exit 1

local-down: ## Stop and remove LocalStack (deletes everything in it)
	-docker stop $(LOCAL_CONTAINER)
	-docker rm -f $(LOCAL_CONTAINER)
	-docker ps -aq --filter name=$(LOCAL_CONTAINER)-lambda | xargs -r docker rm -f

local-deploy: init ## terraform apply into LocalStack (workspace "local")
	$(LOCAL_TF) apply -auto-approve $(LOCAL_TF_VARS)

local-outputs: ## Stack outputs in LocalStack
	$(LOCAL_TF) output

local-url: ## Print the LocalStack webhook URL
	@$(LOCAL_TF) output -raw webhook_url

local-generate: ## Post fake events to the LocalStack webhook
	@# Function URLs are routed by hostname. Posting to localhost with the URL's host in the
	@# Host header works even where *.localhost.localstack.cloud does not resolve.
	HOST=$$($(MAKE) -s local-url | sed -E 's#^https?://([^/:]+).*#\1#'); \
	echo "POSTing to $(LOCAL_ENDPOINT)/ as $$HOST"; \
	WEBHOOK_URL=$(LOCAL_ENDPOINT)/ WEBHOOK_HOST=$$HOST WEBHOOK_SECRET=$(LOCAL_SECRET) \
		$(PY) generator/generate.py --interval 1 --batch-size $(LOCAL_BATCH) --count $(LOCAL_COUNT)

local-verify: ## Assert the LocalStack events log group holds exactly LOCAL_COUNT x LOCAL_BATCH well-formed events
	$(LOCAL_AWS_ENV) $(PY) scripts/verify_events.py --endpoint-url $(LOCAL_ENDPOINT) \
		--log-group /aws/events/$(BUS_NAME) --source $(EVENT_SOURCE) --expect $$(( $(LOCAL_COUNT) * $(LOCAL_BATCH) ))

local-logs: ## Print the webhook Lambda logs from LocalStack
	$(LOCAL_AWS_ENV) aws --endpoint-url $(LOCAL_ENDPOINT) logs tail /aws/lambda/$(NAME)-webhook --region $(REGION) --format short

local-logs-events: ## Print every event on the LocalStack bus
	$(LOCAL_AWS_ENV) aws --endpoint-url $(LOCAL_ENDPOINT) logs tail /aws/events/$(BUS_NAME) --region $(REGION) --format short

local-destroy: ## terraform destroy in LocalStack (local-down removes everything anyway)
	$(LOCAL_TF) destroy -auto-approve $(LOCAL_TF_VARS)

local-e2e: ## The whole pipeline in LocalStack - up, deploy, generate, verify, down; what CI runs
	@# LocalStack is torn down whatever happens; the recipe's exit status is that of the pipeline.
	trap '$(MAKE) local-down' EXIT; \
	$(MAKE) local-up && $(MAKE) local-deploy && $(MAKE) local-generate && $(MAKE) local-verify
