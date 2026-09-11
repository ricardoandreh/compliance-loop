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
│   ├── integration_test.py         # Testes end-to-end automatizados
│   └── benchmark.py                # Agrega N execuções da suíte (n, desvio, IC 95%)
└── tests/
    ├── unit/                       # Testes unitários
    └── property/                   # Property-based tests (Hypothesis)
```

## Pré-requisitos

- Python 3.13+ com `boto3` — os scripts de teste e benchmark não rodam sem ele
- AWS CLI configurado
- OpenTofu (executado via Docker: `ghcr.io/opentofu/opentofu:latest`)

O `boto3` precisa estar no interpretador que você usa para chamar os scripts.
Neste ambiente ele vive num virtualenv:

```bash
lambdas/analyze/.venv/bin/python scripts/integration_test.py
lambdas/analyze/.venv/bin/python scripts/benchmark.py --runs 10
```

O `benchmark.py` repassa o próprio interpretador (`sys.executable`) ao invocar
o `integration_test.py`, então basta acertar o de fora. Chamar com o `python3`
do sistema falha com `ModuleNotFoundError: No module named 'boto3'`.

## Deploy

Rode **a partir da raiz do repositório**, com `-chdir=infra`:

```bash
# 1. Provisionar infraestrutura (empacota e publica as Lambdas junto)
tofu -chdir=infra init
tofu -chdir=infra apply

# 2. Deploy dos controles (só quando policy.rego / remediation.tf / control.json mudarem)
./scripts/deploy_control.sh --all
```

O código das Lambdas é empacotado pelo `archive_file` durante o `apply` — não
há passo manual de `zip` + `update-function-code`.

> **Não rode de dentro de `infra/`.** Se o `tofu` do seu PATH for o wrapper
> Docker (`docker run -v "$(pwd):/workspace" -w /workspace ...`), ele monta
> apenas o diretório atual. A partir de `infra/`, o `lambdas/` fica fora do
> mount e os módulos de layer falham com:
>
> ```
> Call to function "filebase64sha256" failed:
>   open ../lambdas/analyze/opa_layer.zip: no such file or directory
> ```
>
> O arquivo existe — é o container que não o enxerga. Rodar da raiz com
> `-chdir=infra` monta o repositório inteiro e resolve. Os caminhos
> `${path.module}/../../../lambdas/...` dos módulos estão corretos.

Alternativa sem wrapper, invocando o Docker diretamente da raiz:

```bash
docker run --rm -v "$(pwd):/work" -v "$HOME/.aws:/root/.aws:ro" \
  -w /work/infra ghcr.io/opentofu/opentofu:latest apply
```

As layers OPA e OpenTofu usam zips pré-construídos
(`lambdas/analyze/opa_layer.zip`, `lambdas/execute/tofu_layer.zip`), que são
artefatos de build ignorados pelo git — precisam existir localmente antes do
`apply`.

## Testes

```bash
# Testes de integração end-to-end (todos os cenários + controles)
lambdas/analyze/.venv/bin/python scripts/integration_test.py

# Teste específico
lambdas/analyze/.venv/bin/python scripts/integration_test.py --test C1
lambdas/analyze/.venv/bin/python scripts/integration_test.py --test CTRL-03

# Com métricas em JSON
lambdas/analyze/.venv/bin/python scripts/integration_test.py --json
```

### Benchmark (múltiplas execuções)

Uma execução da suíte produz uma observação por métrica — suficiente para
demonstrar o loop, insuficiente para afirmar uma latência. O `benchmark.py`
repete a suíte, preserva o JSON bruto de cada rodada e agrega:

```bash
# 10 execuções -> results/run-NN.json + summary.{json,md}
lambdas/analyze/.venv/bin/python scripts/benchmark.py --runs 10

# Reagrega o que já está em disco, sem tocar na AWS
lambdas/analyze/.venv/bin/python scripts/benchmark.py --aggregate-only --out results/
```

Reporta n, média, desvio amostral, mediana, amplitude e IC 95% por cenário e
por hipótese. Latências contam apenas rodadas em que o cenário passou; a taxa
de sucesso aparece ao lado. Métricas com n=1 são marcadas como observação
única, não como média.

## Cenários validados

Todos aprovados em **10 de 10** execuções da suíte. Valores: média ± desvio (n=10).

| Cenário | Descrição | Resultado | Métrica |
|---|---|---|---|
| C1 | Violação simples via console | Remediado, converge a S_nominal | 6,02s ± 0,11 |
| C2 | Violação com exceção ativa | Não remedia, registra S_exception | 0,47s ± 0,08 |
| C3 | Exceção expira → remediação | Scheduler dispara, remedia | 6,04s ± 0,10 |
| C4 | Falha na remediação (bucket inexistente) | Escala via GitHub Issue | 0,60s ± 0,06 |
| C5 | Violação originada por IaC externo | Detecta e remedia | 6,02s ± 0,13 |
| C6 | Recurso já conforme | Confirma S_nominal, sem Execute | 0,45s ± 0,04 |
| C7 | Novo controle → EventBridge atualizado | DynamoDB Stream dispara controls-sync | 15,93s ± 1,02 |
| AUDIT | Ausência de execuções falhas inesperadas | Nenhuma além da esperada do C4 | — |

## Controles validados

| Teste | Controle | Verificação | t_total (n=10) |
|---|---|---|---|
| CTRL-01 | ctrl-s3-public-block | All 4 settings = true | 28,54s ± 2,10 |
| CTRL-02 | ctrl-s3-ssl-only | DenyNonSSLRequests present | 26,81s ± 2,11 |
| CTRL-03 | ctrl-s3-versioning | Versioning = Enabled | 26,32s ± 0,99 |
| CTRL-04 | ctrl-s3-encryption | SSEAlgorithm = AES256 | 27,02s ± 1,29 |
| CTRL-05 | ctrl-ebs-encryption-default | EbsEncryptionByDefault = true | validado à parte, fora da suíte |

O `t_total` das CTRL-* inclui esperas de CloudTrail em série, e não é comparável ao
`t_remediation` — mede o teste, não o loop.

O CTRL-05 não tem teste automatizado: a `integration_test.py` cobre CTRL-01 a
CTRL-04. O controle está implantado e foi validado numa execução isolada
(registrada no `design.md`), mas não entra nas 10 execuções agregadas acima —
logo, o único serviço com cobertura repetida é o S3.

## Performance

Agregado de **10 execuções completas da suíte** (120/120 cenários aprovados),
via `scripts/benchmark.py`. Desvio amostral (n−1) e IC 95% pela distribuição t.

| Métrica | Valor | Desvio | n | IC 95% |
|---|---|---|---|---|
| **H1 — t_remediation** | **6,03s** | 0,11 | 30 | [5,99 · 6,06] |
| **H3 — t_exposure** | **14,15s** | 1,48 | 20 | [13,46 · 14,84] |
| t_detection (evento → SFN start) | 8,13s | 1,46 | 20 | [7,45 · 8,81] |
| t_classification (recurso conforme) | 0,45s | 0,04 | 10 | [0,43 · 0,48] |
| t_escalation (falha → fila SQS) | 0,60s | 0,06 | 10 | [0,55 · 0,64] |

**A remediação é determinística; a detecção é que varia.** `t_remediation` tem
desvio de 0,11s, e os três cenários que remedeiam por rotas independentes
concordam dentro de 0,02s (C1 6,02 · C3 6,04 · C5 6,02). Toda a dispersão do
tempo de exposição vem do `t_detection` — latência de entrega do CloudTrail ao
EventBridge, fora do controle do sistema. A decomposição fecha exatamente:
`t_detection + t_remediation = t_exposure` nas dez execuções.

Cold start custa ~5s (Execute frio 10,09s vs. quente 5,24s) e é raro — 6 de 174
invocações. O `Init Duration` é de apenas 0,40–0,53s; o custo real é o download
do cache de provider dentro do handler.

A otimização chave: o provider OpenTofu é pré-inicializado e armazenado no S3 como cache (~128MB), baixado e extraído em `/tmp` no cold start, compartilhado via symlink entre controles. Elimina `tofu init` (~30-50s) de cada invocação.

Relatório com os gráficos interativos e a tabela completa: `results/report.html`
(dados brutos em `results/run-NN.json`, regeneráveis com `scripts/build_report_data.py`).

## Licença

Projeto acadêmico (TCC).
