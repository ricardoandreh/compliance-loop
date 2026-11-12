# Entry points for a fresh account and for the day-to-day loop. Each target is
# one documented command; see README.md for what they do and in which order.
#
# TOFU: `tofu` on the PATH, or the Docker image (run from the repo root, so the
# container sees lambdas/ as well as infra/):
#   make apply TOFU='docker run --rm -v $$PWD:/work -w /work ghcr.io/opentofu/opentofu:1.12.6'
TOFU    ?= tofu
PY      ?= .venv/bin/python
REGION  ?= us-east-1
PROJECT ?= compliance-loop
ENV     ?= dev
SERIE   ?= nova-serie

.PHONY: bootstrap layers provider-cache init plan apply controls sync-rule \
        test policies integration series destroy

bootstrap:        ## bucket de artefatos, backend.hcl, tfvars, layers e cache do provider
	bash scripts/bootstrap.sh

layers:           ## só as duas layers (OPA e OpenTofu)
	bash scripts/build_analyze.sh
	bash scripts/build_execute.sh

provider-cache:   ## só o cache do provider (ARTIFACT_BUCKET no ambiente)
	bash scripts/build_provider_cache.sh

init:             ## backend e variáveis do ambiente ENV (arquivos gerados pelo bootstrap)
	$(TOFU) -chdir=infra init -reconfigure -backend-config=backend.$(ENV).hcl

plan:
	$(TOFU) -chdir=infra plan -var-file=$(ENV).tfvars

apply:
	$(TOFU) -chdir=infra apply -var-file=$(ENV).tfvars

controls:         ## publica controls/* no bucket e na tabela
	AWS_REGION=$(REGION) PROJECT_NAME=$(PROJECT) ENVIRONMENT=$(ENV) ./scripts/deploy_control.sh --all

sync-rule:        ## regenera a regra do EventBridge a partir da tabela de controles
	aws lambda invoke --region $(REGION) --function-name $(PROJECT)-controls-sync-$(ENV) --payload '{}' /dev/stdout

test:             ## unitários, propriedade e contrato (sem conta AWS)
	$(PY) -m pytest tests -q

policies:         ## testes Rego e contrato remediação x política (Docker)
	./scripts/test_policies.sh

integration:      ## os 18 cenários contra a conta
	$(PY) scripts/integration_test.py --region $(REGION) --prefix $(PROJECT) --env $(ENV)

series:           ## dez execuções da suíte -> results/$(SERIE)
	$(PY) scripts/benchmark.py --region $(REGION) --prefix $(PROJECT) --env $(ENV) --runs 10 --out results/$(SERIE)

destroy:
	$(TOFU) -chdir=infra destroy -var-file=$(ENV).tfvars
