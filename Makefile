PY          ?= python3
STACK       ?= aws-eda-simple
REGION      ?= eu-west-1
BUS_NAME    ?= simple-eda-bus
EVENT_SOURCE ?= com.example.shop
# Required for `make deploy`. Generate one with: export WEBHOOK_SECRET=$$(openssl rand -hex 24)
WEBHOOK_SECRET ?=

.PHONY: help install lint fmt test validate build deploy deploy-guided outputs url generate logs logs-events invoke-local delete

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
			EventSource=$(EVENT_SOURCE)

deploy-guided: build ## Interactive first-time deploy
	sam deploy --guided --stack-name $(STACK) --region $(REGION)

outputs: ## Show stack outputs
	aws cloudformation describe-stacks --stack-name $(STACK) --region $(REGION) \
		--query 'Stacks[0].Outputs' --output table

url: ## Print the webhook URL
	@aws cloudformation describe-stacks --stack-name $(STACK) --region $(REGION) \
		--query "Stacks[0].Outputs[?OutputKey=='WebhookUrl'].OutputValue" --output text

generate: ## Post fake events to the deployed webhook (requires WEBHOOK_SECRET)
	@test -n "$(WEBHOOK_SECRET)" || { echo "WEBHOOK_SECRET is not set"; exit 1; }
	WEBHOOK_URL=$$($(MAKE) -s url) WEBHOOK_SECRET=$(WEBHOOK_SECRET) \
		$(PY) generator/generate.py --interval 2 --batch-size 3 --count 10

logs: ## Tail the webhook Lambda logs
	sam logs -n WebhookFunction --stack-name $(STACK) --region $(REGION) --tail

logs-events: ## Tail the events delivered to the bus (via the catch-all rule)
	aws logs tail /aws/events/$(BUS_NAME) --region $(REGION) --follow --format short

invoke-local: ## Invoke the function locally with events/post.json (needs env.json, see README)
	sam local invoke WebhookFunction -e events/post.json --env-vars env.json

delete: ## Delete the stack
	sam delete --stack-name $(STACK) --region $(REGION) --no-prompts
