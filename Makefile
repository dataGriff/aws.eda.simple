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

.PHONY: help install lint fmt test validate init plan deploy outputs url generate logs logs-events invoke-local delete

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

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
