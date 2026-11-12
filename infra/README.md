# Infrastructure - MVP Compliance Automation Loop

Infraestrutura OpenTofu modularizada para o sistema de compliance automation.

## Módulos

| Módulo | Recursos |
|---|---|
| `knowledge/` | DynamoDB (controls, exceptions, violations_log) + Streams |
| `monitoring/` | CloudTrail + S3 bucket para logs |
| `orchestration/` | Step Functions + EventBridge Rule + Input Transformer |
| `analyze/` | Lambda Analyze + IAM (ABAC) + OPA Layer |
| `execute/` | Lambda Execute + IAM + OpenTofu Layer |
| `escalation_queue/` | SQS FIFO + DLQ |
| `escalation_lambda/` | Lambda Escalation + IAM |
| `exception_scheduler/` | Lambda sync + EventBridge Scheduler + IAM |
| `opa_layer/` | Lambda Layer com binário OPA arm64 |
| `tofu_layer/` | Lambda Layer com binário OpenTofu arm64 |

## Deploy

```bash
# Via Docker (recomendado)
docker run --rm \
  -v "$(pwd)/../:/project" \
  -v "$HOME/.aws:/root/.aws:ro" \
  -w /project/infra \
  -e AWS_REGION=us-east-1 \
  ghcr.io/opentofu/opentofu:1.12.6 \
  apply -auto-approve

# Ou localmente (se tofu instalado)
cd infra && tofu apply
```

## Variáveis

| Variável | Default | Descrição |
|---|---|---|
| `region` | `us-east-1` | Região AWS |
| `environment` | `dev` | Ambiente (dev/staging/prod) |
| `project_name` | `compliance-loop` | Prefixo para nomes de recursos |
| `artifact_bucket` | `<project_name>-artifacts-<conta>` | Bucket do state, dos artefatos dos controles e do cache do provider (criado por `scripts/bootstrap.sh`) |
| `exception_writer_principals` | `[]` (confia na conta) | Quem pode assumir o papel `exception-writer`, o único que escreve exceções |
| `github_token` | — | PAT para criação de issues |
| `github_repo` | — | Repositório no formato `owner/repo` |

## Outputs

```bash
tofu output state_machine_arn
tofu output analyze_function_name
tofu output execute_function_name
tofu output controls_table_name
tofu output exceptions_table_name
tofu output violations_log_table_name
tofu output eventbridge_rule_name
```

## Naming Convention

Recursos seguem: `{project_name}-{component}-{environment}`

Exemplos:
- `compliance-loop-analyze-dev`
- `compliance-loop-controls-dev`
- `compliance-loop-compliance-loop-dev` (state machine)

## State

Backend S3 no bucket de artefatos, configurado no `init`:

```bash
tofu -chdir=infra init -backend-config=backend.hcl   # arquivo gerado por scripts/bootstrap.sh
```

`backend.hcl.example` mostra o formato. Rode sempre a partir da raiz do repositório com `-chdir=infra` (ver README principal).

## Custo estimado (dev, us-east-1)

| Serviço | Custo/mês |
|---|---|
| DynamoDB (on-demand) | ~$1 |
| CloudTrail | ~$2 |
| Step Functions | ~$0.50 |
| Lambda (5 funções) | ~$1 |
| S3 (policies + provider cache) | ~$0.10 |
| SQS | ~$0.01 |
| **Total** | **~$5** |
