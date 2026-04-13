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
┌─────────────────────────────────────────────────┐
│  Step Functions (orquestrador)                  │
│                                                 │
│  1. Analyze  → Avalia com OPA/Rego              │
│               → Consulta exceções no DynamoDB   │
│               → Classifica: nominal / exceção   │
│                             / violação          │
│                                                 │
│  2. Execute  → Remedia com OpenTofu             │
│               (se violação)                     │
│                                                 │
│  3. Escalate → Cria GitHub Issue                │
│               (se remediação falhar)            │
└─────────────────────────────────────────────────┘
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

## Arquitetura

| Componente | Tecnologia | Função |
|---|---|---|
| Monitor | CloudTrail + EventBridge | Detecta mudanças em recursos S3 |
| Analyze | Lambda + OPA/Rego | Avalia conformidade e classifica estado |
| Execute | Lambda + OpenTofu (container) | Remedia violações via IaC declarativa |
| Knowledge | DynamoDB (3 tabelas) | Controles, exceções e log de violações |
| Escalação | SQS FIFO + Lambda + GitHub API | Cria issues para falhas de remediação |
| Scheduler | EventBridge Scheduler | Re-avalia após expiração de exceções |

## Estrutura do projeto

```
.
├── infra/                          # Infraestrutura (OpenTofu, modularizada)
│   ├── main.tf                     # Root module
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
│   ├── analyze/                    # Classificação de conformidade (Python + OPA)
│   ├── execute/                    # Remediação com OpenTofu (container image)
│   ├── escalation/                 # Criação de GitHub Issues
│   └── exception_scheduler_sync/   # Sincroniza exceções → Scheduler
├── policies/                       # Políticas Rego
├── scripts/                        # Scripts utilitários
├── tests/
│   ├── unit/                       # Testes unitários
│   └── property/                   # Property-based tests (Hypothesis)
└── docker-compose.yml              # Build da container image
```

## Pré-requisitos

- Python 3.12+
- Docker
- AWS CLI configurado
- OpenTofu 1.6+ (executado via Docker: `ghcr.io/opentofu/opentofu:latest`)

## Deploy

```bash
# 1. Provisionar infraestrutura
cd infra
docker run --rm -v "$(pwd)/../:/work" -v "$HOME/.aws:/root/.aws:ro" \
  -w /work/infra ghcr.io/opentofu/opentofu:latest init
docker run --rm -v "$(pwd)/../:/work" -v "$HOME/.aws:/root/.aws:ro" \
  -w /work/infra ghcr.io/opentofu/opentofu:latest apply

# 2. Build e push da container image do Execute Lambda
cd ../lambdas/execute
docker build -t <ECR_URL>:latest .
docker push <ECR_URL>:latest

# 3. Configurar credenciais do GitHub para escalação
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
| C1 | Violação simples via console | Remediado em ~18s |
| C2 | Violação com exceção ativa | Não remedia, registra exceção |
| C3 | Exceção expira | Scheduler dispara, remedia em ~8s |
| C4 | Falha na remediação | Escala via GitHub Issue |
| C5 | Violação originada por IaC externo | Detecta e remedia normalmente |
| C6 | Recurso já conforme | Confirma em <1s, sem ação |

Evidências detalhadas com timestamps e execution ARNs disponíveis em [`.kiro/specs/mvp-compliance-automation-loop/design.md`](.kiro/specs/mvp-compliance-automation-loop/design.md).

## Licença

Projeto acadêmico (TCC).
