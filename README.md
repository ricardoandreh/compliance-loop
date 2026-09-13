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
│                                                          │
│  6. Verify   → se remediou, agenda UMA nova rodada:      │
│              reavalia os controles corrigidos e          │
│              confirma S_nominal ou escala                │
└──────────────────────────────────────────────────────────┘
```

O laço se fecha em **no máximo duas execuções** por incidente. As escritas da
própria remediação não disparam o sistema de novo — a regra do EventBridge
ignora a identidade da Lambda Execute —, e a rodada de verificação nunca
remedeia, então o loop não tem como oscilar.

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
| Controls Sync | DynamoDB Stream → Lambda | Atualiza a EventBridge rule ao inserir/modificar controles, excluindo eventos da própria remediação |

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

**Depois de um `apply` que mude o padrão da regra**, regenere a regra viva: o
Terraform não a atualiza (`ignore_changes`), quem a escreve é o `controls-sync`.

```bash
aws lambda invoke --function-name compliance-loop-controls-sync-dev --payload '{}' /dev/stdout
```

> **Credenciais com `aws login`.** O provider AWS dentro do container do
> OpenTofu não lê a sessão do novo `aws login` (`~/.aws/login/cache`). Exporte
> credenciais temporárias e passe-as ao container:
>
> ```bash
> eval "$(aws configure export-credentials --format env)"
> docker run --rm -v "$PWD:/workspace" -w /workspace \
>   -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN -e AWS_REGION=us-east-1 \
>   ghcr.io/opentofu/opentofu:latest -chdir=infra apply
> ```

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

### Políticas e contrato de remediação

```bash
./scripts/test_policies.sh
```

Roda os testes Rego (`policy_test.rego`) e o **contrato de remediação** — verifica
que o estado produzido por cada `remediation.tf` é aceito pela própria política,
que é o que garante que o loop converge em vez de oscilar. Usa a imagem oficial
do OPA na mesma versão do layer da Lambda (lida do `build_analyze.sh`); requer
Docker.

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

Todos aprovados em **10 de 10** execuções da suíte. Valores: média ± desvio,
**separados em regime quente e frio** por evidência de `Init Duration` na
invocação da Lambda (`scripts/classify_cold_starts.py`). A única amostra fria
das 10 execuções é do C1.

| Cenário | Descrição | Resultado | Quente | Frio |
|---|---|---|---|---|
| C1 | Violação simples via console | Remedia e verifica S_nominal | `t_remediation` 6,19s ± 0,05 (n=9) | 14,78s (n=1) |
| C2 | Violação com exceção ativa | Não remedia, registra S_exception | `t_classification` 0,47s ± 0,04 | nenhuma |
| C3 | Exceção expira → remediação | Scheduler dispara, remedia e verifica | `t_remediation` 6,24s ± 0,08 | nenhuma |
| C4 | Falha na remediação (bucket inexistente) | Escala via GitHub Issue | `t_escalation` 0,58s ± 0,07 | nenhuma |
| C5 | Violação originada por IaC externo | Detecta, remedia e verifica | `t_remediation` 6,18s ± 0,15 | nenhuma |
| C6 | Recurso já conforme | Confirma S_nominal, sem Execute | `t_classification` 0,47s ± 0,04 | nenhuma |
| C7 | Novo controle → EventBridge atualizado | DynamoDB Stream dispara controls-sync | não classificado: 15,64s ± 0,08 | — |
| AUDIT | Ausência de execuções falhas inesperadas | Nenhuma além da esperada do C4 | — | — |

## Controles validados

| Teste | Controle | Verificação | t_total (n=10) |
|---|---|---|---|
| CTRL-01 | ctrl-s3-public-block | All 4 settings = true | 44,37s ± 2,04 |
| CTRL-02 | ctrl-s3-ssl-only | DenyNonSSLRequests present | 38,57s ± 1,06 |
| CTRL-03 | ctrl-s3-versioning | Versioning = Enabled | 38,05s ± 2,06 |
| CTRL-04 | ctrl-s3-encryption | SSEAlgorithm = AES256 | 38,98s ± 1,43 |
| CTRL-05 | ctrl-ebs-encryption-default | EbsEncryptionByDefault = true | validado à parte, fora da suíte |

O `t_total` das CTRL-* mede o **teste**: criação de bucket, esperas de CloudTrail
e a espera pelas rodadas de verificação, já que o harness só encerra quando
nenhuma execução está em andamento. Não é comparável ao `t_remediation` nem é
classificado em quente/frio.

O CTRL-05 não tem teste automatizado: a `integration_test.py` cobre CTRL-01 a
CTRL-04. O controle está implantado e foi validado numa execução isolada
(registrada no `design.md`), mas não entra nas 10 execuções agregadas acima —
logo, o único serviço com cobertura repetida é o S3.

## Performance

Agregado de **10 execuções completas da suíte** com o laço em duas rodadas
(120/120 cenários aprovados), via `scripts/benchmark.py`. Desvio amostral (n−1)
e IC 95% pela distribuição t. **Quente e frio são reportados separadamente**,
classificados por evidência (`Init Duration` na invocação exata da Lambda),
sem corte de duração. Uma amostra fria com n=1 é reportada como observação
única.

| Métrica | Quente | Frio | Total |
|---|---|---|---|
| **H1 — t_remediation** | **6,20s** ± 0,10 · IC [6,16 · 6,24] · n=29 | **14,78s** (n=1) | 6,49s ± 1,57 · mediana 6,20 · n=30 |
| **H3 — t_exposure** | **13,90s** ± 1,87 · IC [13,00 · 14,80] · n=19 | **20,06s** (n=1) | 14,21s ± 2,28 · n=20 |
| **t_verified** (convergência confirmada) | **29,26s** ± 1,88 · IC [28,35 · 30,16] · n=19 | **35,42s** (n=1) | 29,57s ± 2,29 · n=20 |
| t_detection (evento → SFN start) | sem classe: 7,59s ± 1,85 · IC [6,73 · 8,46] · n=20 | | |
| t_classification (recurso conforme) | 0,47s ± 0,04 · n=10 | nenhuma | |
| t_escalation (falha → fila SQS) | 0,58s ± 0,07 · n=10 | nenhuma | |

**A remediação quente varia 1,6%; a detecção, 24% — quinze vezes mais.** Os três
cenários que remedeiam por rotas independentes concordam dentro de 0,06s no
regime quente (C1 6,19 · C3 6,24 · C5 6,18). Toda a dispersão do tempo de
exposição vem do `t_detection` — latência de entrega do CloudTrail ao
EventBridge, fora do controle do sistema. A decomposição fecha exatamente:
`t_detection + t_remediation = t_exposure`. O `t_detection` não recebe classe
porque termina antes de qualquer Lambda rodar.

**Cold start** foi a única amostra fria em 30: C1 na primeira execução, após
horas sem tráfego, com Analyze e Execute em containers novos. O `Init Duration`
é só ~0,6s; o custo real (~+5s no Execute) é o download do cache de provider
dentro do handler. O desvio de 1,57s do total é inteiramente essa amostra — a
mediana fica em 6,20s.

**Comparado ao laço anterior** (confirmação por eco do CloudTrail, 60 amostras,
nenhuma fria): `t_remediation` era 6,03s ± 0,11 · IC [5,98 · 6,07]. Os +0,18s no
regime quente são o novo estado `ScheduleVerification` (0,156s medidos no
histórico). `t_exposure` era 14,15s ± 1,48. As execuções por rodada da suíte
caíram de **48,5 para 36,0 (−26%)**, e um incidente com N controles remediados
passou de 1 + N para **no máximo 2** execuções. O `t_verified` inclui a espera
de consistência de 15s da rodada de verificação — uma premissa de projeto.

A otimização chave: o provider OpenTofu é pré-inicializado e armazenado no S3 como cache (~128MB), baixado e extraído em `/tmp` no cold start, compartilhado via symlink entre controles. Elimina `tofu init` (~30-50s) de cada invocação.

Relatório com os gráficos interativos e a tabela completa: `results/report.html`
(dados brutos em `results/run-NN.json`, regeneráveis com `scripts/build_report_data.py`).

## Licença

Projeto acadêmico (TCC).
