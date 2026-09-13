# swesim: developer checks and the hosted-stack lifecycle on GCP. Every cloud target works from a fresh clone with
# SWESIM_ENV_FILE pointing at your env file (see .env.example) and gcloud authenticated as a project owner
# (`gcloud auth login` and `gcloud auth application-default login`; Terraform uses the latter). Nothing here prints a
# secret: values travel from the env file to Secret Manager through scripts/cloud/push_secrets.py on stdin.
#
#   make check                       uv sync + the offline test suite
#   make cloud-bootstrap             APIs, state bucket, tfvars, terraform apply, secrets, image, pools/console, verify
#   make deploy SHA=<sha>            build <sha> -> terraform apply -var image=<digest> -> verify-cloud
#   make build | push-secrets | verify-cloud | pull | plan | apply | outputs | fmt-check
#   make cloud-destroy [PURGE=1]     destroy everything; the workspace bucket is retained unless PURGE=1
#
# Variables: PROJECT (default: gcloud config), REGION (us-central1), SHA (HEAD), SWESIM_ENV_FILE (./.env),
# EXTRA_ENV_FILES (more dotenv files for push-secrets, e.g. one holding OPENROUTER_API_KEY), MODAL_TOML (~/.modal.toml),
# EVIDENCE_DIR (workspace/evidence/cloud), TF_ARGS (extra terraform apply/plan arguments).
SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c
# gcloud's tarball install location and terraform's user-local location; harmless when they are already on PATH.
export PATH := $(HOME)/google-cloud-sdk/bin:$(HOME)/.local/bin:$(PATH)

# Terraform (the google provider and the GCS backend) authenticates with Application Default Credentials. On a machine
# without them (no `gcloud auth application-default login` yet) fall back to the active gcloud account's short-lived
# access token, which the provider and backend accept through GOOGLE_OAUTH_ACCESS_TOKEN. Only for cloud goals.
# ($(shell ...) does not inherit the exported PATH on GNU make 3.81, macOS's default, hence the explicit PATH= prefix.)
ifneq ($(filter-out check fmt-check,$(MAKECMDGOALS)),)
ADC_OK := $(shell PATH="$(PATH)" gcloud auth application-default print-access-token >/dev/null 2>&1 && echo 1)
ifeq ($(ADC_OK),)
GCLOUD_TOKEN := $(shell PATH="$(PATH)" gcloud auth print-access-token 2>/dev/null)
ifneq ($(GCLOUD_TOKEN),)
export GOOGLE_OAUTH_ACCESS_TOKEN ?= $(GCLOUD_TOKEN)
endif
endif
endif

PROJECT ?= $(shell PATH="$(PATH)" gcloud config get project 2>/dev/null)
REGION ?= us-central1
SHA ?= HEAD
SWESIM_ENV_FILE ?= ./.env
EXTRA_ENV_FILES ?=
MODAL_TOML ?= $(HOME)/.modal.toml
EVIDENCE_DIR ?= workspace/evidence/cloud
TF_ARGS ?=
export SWESIM_ENV_FILE

UV := uv run --frozen
TF := terraform -chdir=infra
TFVARS := infra/env.auto.tfvars.json
STATE_BUCKET := $(PROJECT)-swesim-tfstate
ENV_FILE_FLAGS := --env-file $(SWESIM_ENV_FILE) $(foreach f,$(EXTRA_ENV_FILES),--env-file $(f))

.PHONY: check fmt-check require-project tf-init tfvars plan apply outputs push-secrets seed-registry build pin-image \
        deploy verify-cloud migrate pull gh-variables import-bucket cloud-bootstrap cloud-destroy

check:
	uv sync --extra dev --frozen
	uv run --frozen --offline python -m pytest -q tests --ignore=tests/integration

fmt-check:
	$(TF) fmt -check -recursive
	$(TF) init -input=false -backend=false >/dev/null
	$(TF) validate

require-project:
	@test -n "$(PROJECT)" || { echo "PROJECT is empty: pass PROJECT=<id> or run 'gcloud config set project <id>'"; exit 2; }
	@test -f "$(SWESIM_ENV_FILE)" || { echo "SWESIM_ENV_FILE=$(SWESIM_ENV_FILE) does not exist (copy .env.example)"; exit 2; }

tf-init: require-project
	bash scripts/cloud/bootstrap.sh $(PROJECT) $(REGION)

# Non-secret Terraform inputs from the env file + gcloud account + git remote; a copy goes next to the state so the
# GitHub deploy workflow applies with the same values.
tfvars: tf-init
	$(UV) python scripts/cloud/tfvars_from_env.py $(ENV_FILE_FLAGS) --project $(PROJECT) --region $(REGION) --out $(TFVARS)
	gcloud storage cp $(TFVARS) gs://$(STATE_BUCKET)/env.auto.tfvars.json --quiet

# Every plan/apply leaves its full output under EVIDENCE_DIR (terraform-plan-<UTC>.txt / terraform-apply-<UTC>.txt).
plan:
	mkdir -p $(EVIDENCE_DIR)
	$(TF) plan -input=false -no-color $(TF_ARGS) 2>&1 | tee $(EVIDENCE_DIR)/terraform-plan-$$(date -u +%Y%m%dT%H%M%SZ).txt

apply:
	mkdir -p $(EVIDENCE_DIR)
	$(TF) apply -input=false -auto-approve -no-color $(TF_ARGS) 2>&1 | tee $(EVIDENCE_DIR)/terraform-apply-$$(date -u +%Y%m%dT%H%M%SZ).txt
	$(MAKE) outputs

outputs:
	$(TF) output -json > infra/outputs.json

push-secrets: require-project
	$(UV) python scripts/cloud/push_secrets.py --project $(PROJECT) $(ENV_FILE_FLAGS) --modal-toml $(MODAL_TOML)

# The bucket must hold a registry before the console/verify are useful; the tracked registry.json seeds an empty bucket
# (never overwrites), scripts/cloud/import_vm.sh replaces it with the VM record.
seed-registry:
	gcloud storage cp --no-clobber registry.json gs://$$($(TF) output -raw bucket)/registry.json --quiet

build: require-project
	bash scripts/cloud/build.sh $(PROJECT) $(REGION) $(SHA)

# The pinned digest is persisted in the git-ignored infra/image.auto.tfvars.json, so a later plain `make apply`
# keeps the pools on that digest instead of silently reverting them to :latest.
pin-image: require-project
	digest=$$(bash scripts/cloud/build.sh $(PROJECT) $(REGION) $(SHA) | tail -n1); \
	case "$$digest" in *@sha256:*) ;; *) echo "build did not yield a digest; not applying"; exit 1 ;; esac; \
	printf '{"image": "%s"}\n' "$$digest" > infra/image.auto.tfvars.json; \
	echo "pinned $$digest"

deploy: require-project pin-image
	$(MAKE) apply
	$(MAKE) migrate
	$(MAKE) verify-cloud

verify-cloud:
	EVIDENCE_DIR=$(EVIDENCE_DIR) bash scripts/cloud/verify_cloud.sh

# Run migrations where the Cloud SQL socket exists. A failed migration stops deployment.
migrate:
	gcloud run jobs execute "$$($(TF) output -raw index_maintenance_job)" --project $(PROJECT) --region $(REGION) --wait

pull:
	bash scripts/cloud/pull.sh

# Repository variables the GitHub deploy workflow reads (gh must be authenticated for this repository).
gh-variables:
	gh variable set GCP_PROJECT --body "$$($(TF) output -raw project)"
	gh variable set GCP_REGION --body "$$($(TF) output -raw region)"
	gh variable set WIF_PROVIDER --body "$$($(TF) output -raw wif_provider)"
	gh variable set DEPLOYER_SA --body "$$($(TF) output -raw deployer_service_account)"

# After a cloud-destroy without PURGE the retained bucket is unknown to a fresh state; adopt it before the next apply.
import-bucket: require-project
	$(TF) import -input=false module.storage.google_storage_bucket.workspace $(PROJECT)/swesim-$(PROJECT)-workspace

cloud-bootstrap: require-project tf-init tfvars
	state=$$($(TF) state list); \
	if printf '%s\n' "$$state" | grep -q '^module\.run\['; then \
	  echo "workloads already exist; preserving them during bootstrap"; \
	else $(MAKE) apply TF_ARGS="-var deploy_workloads=false"; fi
	$(MAKE) push-secrets
	$(MAKE) seed-registry
	$(MAKE) pin-image
	$(MAKE) apply
	$(MAKE) migrate
	$(MAKE) verify-cloud

cloud-destroy: require-project
ifeq ($(PURGE),1)
	$(TF) apply -input=false -auto-approve -var bucket_force_destroy=true -target=module.storage.google_storage_bucket.workspace
	$(TF) destroy -input=false -auto-approve -var bucket_force_destroy=true
else
	$(TF) state rm module.storage.google_storage_bucket.workspace || true
	$(TF) destroy -input=false -auto-approve
	@echo "workspace bucket retained (PURGE=1 deletes it); the state bucket gs://$(STATE_BUCKET) is never deleted here"
endif
