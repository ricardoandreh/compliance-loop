# Compliance Automation Loop

Sistema de automação de conformidade para AWS baseado no padrão arquitetural **MAPE-K** (Monitor, Analyze, Plan, Execute, Knowledge). Detecta desvios de configuração em recursos AWS, avalia conformidade com políticas declarativas, remedia automaticamente quando possível, e escala para operadores humanos quando necessário.

## Como funciona

```
Alguém desabilita o bloqueio de acesso público de um bucket S3
    │
    ▼
CloudTrail registra o evento
    │
    ▼
EventBridge detecta e dispara o loop de compliance
    │
    ▼
┌──────────────────────────────────────────────────────────┐
│  Step Functions (orquestrador)                           │
│                                                          │
│  1. ResolveResource → Extrai resource_id/ARN do evento   │
│                                                          │
│  2. QueryControls → Resolve quais controles aplicar      │
│                     (dinâmico via tabela DynamoDB)        │
│                                                          │
│  3. Analyze  → Busca config via fetch_config (boto3)     │
│              → Avalia com OPA/Rego (policies do S3)      │
│              → Consulta exceções no DynamoDB              │
│              → Classifica: nominal / exceção / violação   │
│                                                          │
│  4. Execute  → Remedia com OpenTofu (HCL do S3)          │
│              (se violação, para cada controle violado)    │
│                                                          │
│  5. Escalate → Cria GitHub Issue                         │
│              (se remediação falhar)                       │
└──────────────────────────────────────────────────────────┘
    │
    ▼
CloudTrail captura a remediação → re-avaliação → S_nominal ✓
```

## Estados de classificação

| Estado | Significado | Ação |
|---|---|---|
| **S_nominal** | Recurso conforme | Nenhuma — registra e encerra |
| **S_exception** | Não conforme, mas com exceção aprovada | Nenhuma — monitora até expirar |
| **S_violation** | Não conforme, sem exceção | Remediação automática |

## Controles implementados

| Control ID | Serviço | Política | Remediação |
|---|---|---|---|
| `ctrl-s3-public-block` | S3 | Todos os 4 public access block settings habilitados | `aws_s3_bucket_public_access_block` |
| `ctrl-s3-ssl-only` | S3 | Bucket policy com deny para non-SSL requests | `aws_s3_bucket_policy` (merge) |
| `ctrl-s3-versioning` | S3 | Versioning habilitado | `aws_s3_bucket_versioning` |
| `ctrl-s3-encryption` | S3 | Server-side encryption configurada | `aws_s3_bucket_server_side_encryption_configuration` |
| `ctrl-ebs-encryption-default` | EC2 | EBS encryption by default habilitado | `aws_ebs_encryption_by_default` |

## Arquitetura

| Componente | Tecnologia | Função |
|---|---|---|
| Monitor | CloudTrail + EventBridge | Detecta mudanças em recursos (multi-serviço) |
| Orchestrator | Step Functions (Map state) | Resolve controles, processa resultados por controle |
| Analyze | Lambda + OPA/Rego (arm64) | Avalia conformidade via fetcher genérico + `fetch_config` |
| Execute | Lambda + OpenTofu (arm64) | Remedia violações via IaC declarativa |
| Knowledge | DynamoDB (3 tabelas) | Controles, exceções e log de violações |
| Escalação | SQS FIFO + Lambda + GitHub API | Cria issues para falhas de remediação |
| Scheduler | EventBridge Scheduler | Re-avalia após expiração de exceções |
| Controls Sync | DynamoDB Stream → Lambda | Atualiza EventBridge rule ao inserir/modificar controles |

## Extensibilidade

Adicionar um novo controle para um serviço já suportado **não requer rebuild de nenhuma Lambda ou infra**:

1. Criar `controls/<control-id>/policy.rego`, `remediation.tf` e `control.json`
2. Executar: `./scripts/deploy_control.sh <control-id>`

O script faz upload para S3 e insert na tabela DynamoDB. O `controls-sync` atualiza o EventBridge automaticamente.

Para um serviço AWS **novo** (ex: RDS), é necessário adicionar a permissão IAM no Terraform (`tofu apply`).

## Estrutura do projeto

```
.
├── controls/                       # Controles de compliance (source of truth)
│   ├── ctrl-s3-public-block/
│   │   ├── policy.rego             # Política OPA/Rego
│   │   ├── remediation.tf          # Template OpenTofu de remediação
│   │   └── control.json            # Metadados DynamoDB
│   ├── ctrl-s3-ssl-only/
│   ├── ctrl-s3-versioning/
│   ├── ctrl-s3-encryption/
│   └── ctrl-ebs-encryption-default/
├── infra/                          # Infraestrutura (OpenTofu, modularizada)
│   ├── main.tf
│   ├── variables.tf
│   ├── state_machine_definition.json
│   └── modules/
│       ├── knowledge/              # DynamoDB (controls, exceptions, violations_log)
│       ├── monitoring/             # CloudTrail + S3 para logs
│       ├── orchestration/          # Step Functions + EventBridge Rule
│       ├── analyze/                # Lambda Analyze + IAM (ABAC)
│       ├── execute/                # Lambda Execute + IAM
│       ├── escalation_queue/       # SQS FIFO + DLQ
│       ├── escalation_lambda/      # Lambda Escalation + IAM
│       ├── exception_scheduler/    # Lambda sync + EventBridge Scheduler
│       ├── opa_layer/              # Lambda Layer OPA binary
│       └── tofu_layer/             # Lambda Layer OpenTofu binary
├── lambdas/
│   ├── analyze/                    # Classificação multi-control (Python + OPA arm64)
│   ├── execute/                    # Remediação com OpenTofu (Layer + S3 provider cache)
│   ├── escalation/                 # Criação de GitHub Issues
│   ├── exception_scheduler_sync/   # Sincroniza exceções → EventBridge Scheduler
│   └── controls_sync/             # Atualiza EventBridge rule via DynamoDB Stream
├── scripts/
│   ├── deploy_control.sh           # Deploy de controle (S3 + DynamoDB)
│   └── integration_test.py         # Testes end-to-end automatizados
└── tests/
    ├── unit/                       # Testes unitários
    └── property/                   # Property-based tests (Hypothesis)
```

## Pré-requisitos

- Python 3.13+
- AWS CLI configurado
- OpenTofu (executado via Docker: `ghcr.io/opentofu/opentofu:latest`)

## Deploy

```bash
# 1. Provisionar infraestrutura
cd infra
docker run --rm -v "$(pwd)/../:/work" -v "$HOME/.aws:/root/.aws:ro" \
  -w /work/infra ghcr.io/opentofu/opentofu:latest init
docker run --rm -v "$(pwd)/../:/work" -v "$HOME/.aws:/root/.aws:ro" \
  -w /work/infra ghcr.io/opentofu/opentofu:latest apply

# 2. Deploy dos controles
./scripts/deploy_control.sh --all

# 3. Build e deploy das Lambdas
cd lambdas/analyze && zip -j analyze.zip handler.py && \
  aws lambda update-function-code --function-name compliance-loop-analyze-dev \
    --zip-file fileb://analyze.zip
cd ../execute && zip -j execute.zip handler.py && \
  aws lambda update-function-code --function-name compliance-loop-execute-dev \
    --zip-file fileb://execute.zip
```

## Testes

```bash
# Testes de integração end-to-end (todos os cenários + controles)
python3 scripts/integration_test.py

# Teste específico
python3 scripts/integration_test.py --test C1
python3 scripts/integration_test.py --test CTRL-03

# Com métricas em JSON
python3 scripts/integration_test.py --json
```

## Cenários validados

| Cenário | Descrição | Resultado |
|---|---|---|
| C1 | Violação simples via console | Remediado em ~14s |
| C2 | Violação com exceção ativa | Não remedia, registra exceção |
| C3 | Exceção expira → remediação | Scheduler dispara, remedia em ~6s |
| C4 | Falha na remediação (bucket inexistente) | Escala via GitHub Issue |
| C5 | Violação originada por IaC externo | Detecta e remedia em ~5s |
| C6 | Recurso já conforme | Confirma em <1s, sem ação |
| C7 | Novo controle → EventBridge atualizado | DynamoDB Stream dispara controls-sync em ~16s |

## Controles validados

| Teste | Controle | Verificação |
|---|---|---|
| CTRL-01 | ctrl-s3-public-block | All 4 settings = true |
| CTRL-02 | ctrl-s3-ssl-only | DenyNonSSLRequests present |
| CTRL-03 | ctrl-s3-versioning | Versioning = Enabled |
| CTRL-04 | ctrl-s3-encryption | SSEAlgorithm = AES256 |
| CTRL-05 | ctrl-ebs-encryption-default | EbsEncryptionByDefault = true |

## Performance

| Métrica | Valor |
|---|---|
| t_detection (evento → SFN start) | ~8s |
| t_remediation cold start | ~14s |
| t_remediation warm start | ~5s |
| t_exposure (evento → remediação) | ~20s |
| H1 (MTTR médio) | **6.8s** |
| H3 (Tempo de exposição médio) | **19.16s** |

A otimização chave: o provider OpenTofu é pré-inicializado e armazenado no S3 como cache (~128MB), baixado e extraído em `/tmp` no cold start, compartilhado via symlink entre controles. Elimina `tofu init` (~30-50s) de cada invocação.

## Licença

Projeto acadêmico (TCC).
