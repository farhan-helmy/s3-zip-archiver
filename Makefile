.DEFAULT_GOAL := help
SHELL := /bin/bash

STACK           ?= s3-zip-archiver
BOOTSTRAP_STACK ?= $(STACK)-bootstrap
ECR_REPO        ?= s3-zip-archiver
REGION          ?= ap-southeast-1
PROFILE         ?= sk8jx

AWS  := aws --region $(REGION) --profile $(PROFILE)
VENV := .venv/bin
PY   := $(VENV)/python

# The image tag is the commit SHA, and that is load-bearing rather than cosmetic.
# SAM publishes a new Lambda version only when the ImageUri string changes, so a
# floating tag such as :latest would mean deploys silently produce no new version
# and the rollback requirement would be quietly unmet.
GIT_SHA := $(shell git rev-parse --short HEAD)
ACCOUNT := $(shell aws sts get-caller-identity --query Account --output text --profile $(PROFILE) 2>/dev/null)
ECR_URI  = $(ACCOUNT).dkr.ecr.$(REGION).amazonaws.com/$(ECR_REPO)
IMAGE    = $(ECR_URI):$(GIT_SHA)

.PHONY: help
help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Local development
# ---------------------------------------------------------------------------
.PHONY: install
install: ## Create the dev virtualenv and install tooling
	uv venv --python 3.12 .venv
	uv pip install --python $(PY) -r requirements-dev.txt

.PHONY: test
test: ## Run unit tests (no AWS account required)
	$(PY) -m pytest -q

.PHONY: lint
lint: ## Lint Python and CloudFormation
	$(VENV)/ruff check src/ tests/ scripts/
	$(VENV)/cfn-lint template.yaml bootstrap.yaml

.PHONY: validate
validate: ## Validate the SAM template
	sam validate --lint --region $(REGION) --profile $(PROFILE)

.PHONY: check
check: lint test validate ## Everything CI runs on a pull request

# ---------------------------------------------------------------------------
# Deployment
# ---------------------------------------------------------------------------
.PHONY: guard-clean
guard-clean:
	@if [ -n "$$(git status --porcelain)" ] && [ -z "$(ALLOW_DIRTY)" ]; then \
		echo "ERROR: uncommitted changes present."; \
		echo "The image is tagged with the commit SHA, so deploying a dirty tree"; \
		echo "would produce an image whose contents no commit describes, and the"; \
		echo "deployed version could never be reproduced or reasoned about."; \
		echo "Commit first, or override with ALLOW_DIRTY=1."; \
		exit 1; \
	fi

.PHONY: bootstrap
bootstrap: ## Deploy the one-time ECR + CI role stack
	$(AWS) cloudformation deploy \
		--template-file bootstrap.yaml \
		--stack-name $(BOOTSTRAP_STACK) \
		--capabilities CAPABILITY_NAMED_IAM \
		--parameter-overrides \
			ExistingOidcProviderArn=$$($(AWS) iam list-open-id-connect-providers \
				--query "OpenIDConnectProviderList[?contains(Arn,'token.actions.githubusercontent.com')].Arn | [0]" \
				--output text | sed 's/^None$$//')

.PHONY: login
login: ## Authenticate Docker against ECR
	$(AWS) ecr get-login-password | docker login --username AWS --password-stdin $(ACCOUNT).dkr.ecr.$(REGION).amazonaws.com

.PHONY: image
image: guard-clean login ## Build and push the Lambda image tagged with the commit SHA
	@echo "Building $(IMAGE)"
	@# oci-mediatypes=false is required. BuildKit defaults to the OCI manifest
	@# format, which Lambda rejects with "The image manifest, config or layer
	@# media type for the source image is not supported". Lambda needs Docker
	@# Image Manifest V2 Schema 2. --provenance/--sbom off for the same reason:
	@# attestation manifests turn the push into a multi-manifest index.
	docker buildx build \
		--platform linux/amd64 \
		--provenance=false \
		--sbom=false \
		--output type=image,name=$(IMAGE),oci-mediatypes=false,push=true \
		src/

.PHONY: deploy
deploy: image ## Build, push and deploy - publishes a new Lambda version
	sam deploy \
		--stack-name $(STACK) \
		--capabilities CAPABILITY_IAM \
		--parameter-overrides ImageUri=$(IMAGE) \
		--image-repository $(ECR_URI) \
		--resolve-s3 \
		--no-confirm-changeset \
		--no-fail-on-empty-changeset \
		--region $(REGION) --profile $(PROFILE)
	@$(MAKE) --no-print-directory outputs

.PHONY: outputs
outputs: ## Show stack outputs
	@$(AWS) cloudformation describe-stacks --stack-name $(STACK) \
		--query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output table

# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
.PHONY: smoke
smoke: ## End-to-end test against the deployed stack
	@STACK=$(STACK) REGION=$(REGION) PROFILE=$(PROFILE) ./scripts/smoke.sh

.PHONY: logs
logs: ## Tail the function's logs
	$(AWS) logs tail /aws/lambda/$(STACK)-compressor --follow

# ---------------------------------------------------------------------------
# Release management
# ---------------------------------------------------------------------------
.PHONY: versions
versions: ## List published versions and what the live alias points at
	@echo "Published versions:"
	@$(AWS) lambda list-versions-by-function --function-name $(STACK)-compressor \
		--query 'Versions[?Version!=`$$LATEST`].[Version,LastModified,ImageConfigResponse.ImageConfig.Command[0]]' \
		--output table
	@echo "Alias 'live' currently serves version:"
	@$(AWS) lambda get-alias --function-name $(STACK)-compressor --name live \
		--query 'FunctionVersion' --output text

.PHONY: rollback
rollback: ## Roll back to a prior version: make rollback VERSION=1
	@if [ -z "$(VERSION)" ]; then echo "Usage: make rollback VERSION=<n>"; exit 1; fi
	$(AWS) lambda update-alias \
		--function-name $(STACK)-compressor \
		--name live \
		--function-version $(VERSION) \
		--query '[Name,FunctionVersion]' --output text
	@echo "Live traffic now served by version $(VERSION)."
	@echo "Note: the CloudFormation stack still records the newer version. Redeploy"
	@echo "from the desired commit to bring the template back into agreement."

# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------
.PHONY: dlq
dlq: ## Show how many events failed permanently
	@$(AWS) sqs get-queue-attributes \
		--queue-url $$($(AWS) cloudformation describe-stacks --stack-name $(STACK) \
			--query "Stacks[0].Outputs[?OutputKey=='DeadLetterQueueUrl'].OutputValue" --output text) \
		--attribute-names ApproximateNumberOfMessages --output text

.PHONY: destroy
destroy: ## Delete the application stack (bucket is retained deliberately)
	$(AWS) cloudformation delete-stack --stack-name $(STACK)
	$(AWS) cloudformation wait stack-delete-complete --stack-name $(STACK)
	@echo "Stack deleted. The S3 bucket has DeletionPolicy: Retain and still exists."
	@echo "Removing it is an explicit, irreversible operator action:"
	@echo "  aws s3 rb s3://$(STACK)-$(ACCOUNT) --force --profile $(PROFILE) --region $(REGION)"
