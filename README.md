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
│  1. QueryControls → Resolve quais controles aplicar      │
│                     (dinâmico via tabela DynamoDB)        │
│                                                          │
│  2. Analyze  → Avalia com OPA/Rego (policies do S3)      │
│              → Consulta exceções no DynamoDB              │
│              → Classifica: nominal / exceção / violação   │
│                                                          │
│  3. Execute  → Remedia com OpenTofu (HCL do S3)          │
│              (se violação, para cada controle violado)    │
│                                                          │
│  4. Escalate → Cria GitHub Issue                         │
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

## Exception Governance

O sistema implementa um modelo de exceções com governança temporal, indo além de um simples flag booleano de supressão. Cada exceção é um registro estruturado com:

| Atributo | Tipo | Função |
|---|---|---|
| `exception_id` | string | Identificador único |
| `control_id` | string | Controle ao qual se aplica |
| `resource_arn` | string | Recurso específico (escopo granular) |
| `valid_until` | number (epoch) | TTL — expiração automática |
| `approved_by` | string | Responsável pela aprovação (auditabilidade) |
| `reason` | string | Justificativa documentada |

O ciclo de vida de uma exceção:

1. **Criação**: insert na tabela `exceptions` → Lambda `exception-scheduler-sync` cria um EventBridge Schedule para `valid_until + 30s`
2. **Vigência**: Analyze detecta a exceção ativa → classifica como `S_exception` → sem remediação
3. **Expiração**: Schedule dispara → Step Function re-avalia → sem exceção válida → `S_violation` → remediação automática
4. **Modificação**: update do `valid_until` → Schedule é recriado com novo horário
5. **Remoção**: delete da exceção → Schedule é removido → próxima avaliação remedia

Isso garante que exceções não se tornem permanentes por esquecimento — toda exceção tem prazo e responsável.

## Arquitetura

| Componente | Tecnologia | Função |
|---|---|---|
| Monitor | CloudTrail + EventBridge | Detecta mudanças em recursos S3 |
| Orchestrator | Step Functions (Map state) | Resolve controles, processa resultados por controle |
| Analyze | Lambda + OPA/Rego (arm64) | Avalia conformidade multi-control em invocação única |
| Execute | Lambda + OpenTofu (container arm64) | Remedia violações via IaC declarativa |
| Knowledge | DynamoDB (3 tabelas) | Controles, exceções e log de violações |
| Escalação | SQS FIFO + Lambda + GitHub API | Cria issues para falhas de remediação |
| Scheduler | EventBridge Scheduler | Re-avalia após expiração de exceções |
| Controls Sync | DynamoDB Stream → Lambda | Atualiza EventBridge rule ao inserir/modificar controles |

## Controles implementados

| Control ID | Política | Remediação | Eventos CloudTrail |
|---|---|---|---|
| `ctrl-s3-public-block` | Todos os 4 public access block settings habilitados | `aws_s3_bucket_public_access_block` | PutBucketPublicAccessBlock, DeleteBucketPublicAccessBlock, CreateBucket |
| `ctrl-s3-ssl-only` | Bucket policy com deny para non-SSL requests | `aws_s3_bucket_policy` (merge com policy existente) | PutBucketPolicy, DeleteBucketPolicy, CreateBucket |

## Extensibilidade

Adicionar um novo controle **não requer rebuild de nenhuma imagem, Lambda ou infra**:

1. Criar `controls/<control-id>/policy.rego` e `remediation.tf`
2. Upload para S3: `aws s3 sync controls/ s3://<bucket>/controls/`
3. Insert na tabela `controls` com `rego_s3_key`, `remediation_s3_key`, `cloudtrail_events`, `rego_policy_package`

O passo 3 dispara automaticamente a Lambda `controls-sync` via DynamoDB Stream, que atualiza o EventBridge rule com os novos eventos CloudTrail. As políticas Rego e templates HCL são baixados do S3 em runtime e cacheados em `/tmp/` entre invocações.

## Estrutura do projeto

```
.
├── controls/                       # Controles de compliance (source of truth)
│   ├── ctrl-s3-public-block/
│   │   ├── policy.rego             # Política OPA/Rego
│   │   └── remediation.tf          # Template OpenTofu de remediação
│   └── ctrl-s3-ssl-only/
│       ├── policy.rego
│       └── remediation.tf
├── infra/                          # Infraestrutura (OpenTofu, modularizada)
│   ├── main.tf
│   ├── variables.tf
│   ├── state_machine_definition.json
│   └── modules/
│       ├── knowledge/              # DynamoDB (controls, exceptions, violations_log)
│       ├── monitoring/             # CloudTrail + S3 para logs
│       ├── orchestration/          # Step Functions + EventBridge Rule
│       ├── analyze/                # Lambda Analyze + IAM
│       ├── execute/                # Lambda Execute (container) + IAM
│       ├── escalation_queue/       # SQS FIFO + DLQ
│       ├── escalation_lambda/      # Lambda Escalation + IAM
│       ├── exception_scheduler/    # Lambda sync + EventBridge Scheduler
│       └── ecr/                    # ECR repository
├── lambdas/
│   ├── analyze/                    # Classificação multi-control (Python + OPA arm64)
│   ├── execute/                    # Remediação com OpenTofu (container image arm64)
│   ├── escalation/                 # Criação de GitHub Issues
│   ├── exception_scheduler_sync/   # Sincroniza exceções → EventBridge Scheduler
│   └── controls_sync/             # Atualiza EventBridge rule via DynamoDB Stream
├── scripts/
│   ├── build_analyze.sh            # Build do analyze.zip (OPA + handler)
│   ├── smoke_test_analyze.sh       # Validação pós-deploy
│   └── setup_ssm_github.sh        # Configura credenciais GitHub no SSM
└── tests/
    ├── unit/                       # Testes unitários
    ├── property/                   # Property-based tests (Hypothesis)
    └── c5_iac_violation/           # Fixture para teste C5 (violação via IaC)
```

## Pré-requisitos

- Python 3.13+
- Docker (com qemu para builds arm64 em hosts x86_64)
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

# 2. Build e push da container image do Execute Lambda (arm64)
docker run --rm --privileged multiarch/qemu-user-static --reset -p yes
cd lambdas/execute
docker build --platform linux/arm64 -t <ECR_URL>:latest .
docker push <ECR_URL>:latest

# 3. Build e deploy do Analyze Lambda
./scripts/build_analyze.sh
aws lambda update-function-code --function-name <analyze-function> \
  --zip-file fileb://lambdas/analyze/analyze.zip

# 4. Upload dos controles para S3
aws s3 sync controls/ s3://<bucket>/controls/

# 5. Configurar credenciais do GitHub para escalação
./scripts/setup_ssm_github.sh <github_pat> <owner> <repo>
```

## Testes

```bash
# Unit tests
pytest tests/unit/

# Property-based tests
pytest tests/property/ --hypothesis-show-statistics
```

## Cenários validados

| Cenário | Descrição | Resultado |
|---|---|---|
| C1 | Violação simples via console | Remediado em ~5s |
| C2 | Violação com exceção ativa | Não remedia, registra exceção |
| C3 | Exceção expira → remediação | Scheduler dispara, remedia em ~8s |
| C4 | Falha na remediação (bucket inexistente) | Escala via GitHub Issue |
| C5 | Violação originada por IaC externo | Detecta e remedia normalmente |
| C6 | Recurso já conforme | Confirma em <1s, sem ação |
| C7 | Exceção expira (TTL) → remediação | Equivalente a C3 (Scheduler dispara) |
| C8 | Novo controle → EventBridge atualizado | DynamoDB Stream dispara controls-sync em <10s |

## Performance

| Cenário | Tempo de remediação |
|---|---|
| Warm invocation (mesmo controle) | ~5s |
| Cold start (novo controle) | ~5s (symlink, sem init) |
| Detecção (evento → SFN start) | ~8-10s |

A otimização chave: o provider OpenTofu é pré-inicializado no build da imagem Docker e compartilhado via symlink em runtime, eliminando o `tofu init` (~30-50s) de cada invocação.

Evidências detalhadas com timestamps e execution ARNs disponíveis em [`.kiro/specs/mvp-compliance-automation-loop/design.md`](.kiro/specs/mvp-compliance-automation-loop/design.md).

## Licença

Projeto acadêmico (TCC).
