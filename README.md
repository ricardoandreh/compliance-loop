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
│  4. Plan     → decide a ação para cada violação:         │
│              detecção → remedia; verificação → escala    │
│                                                          │
│  5. Execute  → Remedia com OpenTofu (HCL do S3)          │
│              (falhou → Escalate: cria GitHub Issue)      │
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
| **S_violation** | Não conforme, sem exceção | Remediação automática; na rodada de verificação, escalação por não convergência |

São os únicos estados. A rodada (detecção ou verificação) não cria estados novos —
só muda a ação diante de uma `S_violation`. Um controle que não pôde ser avaliado
(leitura falhou, OPA falhou, consulta de exceções falhou) não é um quarto estado:
é falha do sistema, fica fora do modelo, não é registrado como estado e é escalado.

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
das 10 execuções é do C1. O desvio do `t_remediation` quente (~0,5s) é a mistura
dos dois tipos de container, não variância do sistema.

| Cenário | Descrição | Resultado | Quente | Frio |
|---|---|---|---|---|
| C1 | Violação simples via console | Remedia e verifica S_nominal | `t_remediation` 5,83s ± 0,52 (n=9) | 14,56s (n=1) |
| C2 | Violação com exceção ativa | Não remedia, registra S_exception | `t_classification` 0,48s ± 0,06 | nenhuma |
| C3 | Exceção expira → remediação | Scheduler dispara, remedia e verifica | `t_remediation` 5,42s ± 0,63 | nenhuma |
| C4 | Falha do sistema (bucket inexistente) | Sem estado; escala via GitHub Issue | `t_escalation` 0,58s ± 0,04 | nenhuma |
| C5 | Violação originada por IaC externo | Detecta, remedia e verifica | `t_remediation` 5,46s ± 0,57 | nenhuma |
| C6 | Recurso já conforme | Confirma S_nominal, sem Execute | `t_classification` 0,45s ± 0,03 | nenhuma |
| C7 | Novo controle → EventBridge atualizado | DynamoDB Stream dispara controls-sync | não classificado: mediana 15,61s | — |
| C8 | Remediação falha (Execute sem permissão) | S_violation preservado, escala com o erro | `t_escalation` 6,23s (observação única)† | — |
| AUDIT | Ausência de execuções falhas inesperadas | Nenhuma além das esperadas do C4 e do C8 | — | — |

†O C8 entrou na suíte depois das 10 execuções agregadas. Uma bucket policy nega
`s3:PutBucketPublicAccessBlock` só à role do Execute: o Analyze classifica
`S_violation`, a remediação falha com `AccessDenied`, o estado é preservado com
`remediation_status=failed` e a issue é vinculada à linha da violação. O C4 não
cobre esse caminho — um bucket inexistente nem chega a ser avaliado.

## Controles validados

| Teste | Controle | Verificação | t_total (n=10) |
|---|---|---|---|
| CTRL-01 | ctrl-s3-public-block | All 4 settings = true | mediana 25,95s (34,17s ± 20,46) |
| CTRL-02 | ctrl-s3-ssl-only | DenyNonSSLRequests present | 26,34s ± 1,34 |
| CTRL-03 | ctrl-s3-versioning | Versioning = Enabled | 25,90s ± 0,05 |
| CTRL-04 | ctrl-s3-encryption | SSEAlgorithm = AES256 | mediana 26,24s (32,48s ± 19,36) |
| CTRL-05 | ctrl-ebs-encryption-default | EbsEncryptionByDefault = true | 28,2s (observação única, container frio) |

O `t_total` das CTRL-* mede o **teste**: criação de bucket, esperas de CloudTrail
e a espera pelas rodadas de verificação, já que o harness só encerra quando
nenhuma execução está em andamento. Não é comparável ao `t_remediation` nem é
classificado em quente/frio. O desvio alto de CTRL-01 e CTRL-04 vem de uma única
rodada, com o tempo extra fora do loop; as medianas não se alteram.

O CTRL-05 entrou na `integration_test.py` depois das 10 execuções agregadas:
desliga a criptografia padrão do EBS na região, exige a remediação e a rodada de
verificação, e religa a configuração se o loop não o fizer. Não cria nenhum
recurso cobrável — é uma configuração da conta. Foi exercitado uma vez, a frio,
então o único serviço com cobertura repetida continua sendo o S3.

O controle é **preventivo**: `EbsEncryptionByDefault` só vale para volumes criados
depois. Um volume existente sem criptografia continua assim, e o controle fica
conforme — a AWS não criptografa nem troca a chave de um volume no lugar (é
snapshot → cópia → volume novo), o que não é remediação para aplicar
automaticamente. Ver L14 no `design.md`.

## Performance

Agregado de **10 execuções completas da suíte** com o laço em duas rodadas e
leitura imediata na verificação (120/120 cenários aprovados, `results/no-wait/`),
via `scripts/benchmark.py`. Desvio amostral (n−1) e IC 95% pela distribuição t.
**Quente e frio são reportados separadamente**, classificados por evidência
(`Init Duration` na invocação exata da Lambda), sem corte de duração. Uma
amostra fria com n=1 é reportada como observação única.

| Métrica | Quente | Frio | Total |
|---|---|---|---|
| **H1 — t_remediation**, container lento | **6,24s** ± 0,10 · IC [6,18 · 6,30] · n=12 | **14,56s** (n=1) | |
| **H1 — t_remediation**, container rápido | **5,08s** ± 0,08 · IC [5,04 · 5,12] · n=17 | | |
| H1 — agregado | 5,56s ± 0,59 · mediana 5,17 · n=29 | | 5,86s ± 1,74 · n=30 |
| **H3 — t_exposure** | **12,89s** ± 1,62 · IC [12,11 · 13,67] · n=19 (lento 14,16 · rápido 11,75) | **20,98s** (n=1) | 13,30s ± 2,40 · n=20 |
| **t_verified** (convergência confirmada) | **13,15s** ± 1,63 · IC [12,36 · 13,93] · n=19 | **21,33s** (n=1) | 13,56s ± 2,42 · n=20 |
| t_detection (evento → SFN start) | sem classe: 7,22s ± 1,21 · IC [6,65 · 7,78] · n=20 | | |
| t_classification (recurso conforme) | 0,45–0,48s · n=10 | nenhuma | |
| t_escalation (falha → fila SQS) | 0,58s ± 0,04 · n=10 | nenhuma | |

**"Quente" não é um regime só.** O Execute roda ~1,1s mais rápido num dos
containers da Lambda do que nos outros — 90 das 251 invocações da série, todas no
mesmo container; nas 280 da série anterior, nenhuma. O atraso fica antes da
primeira escrita e não depende do tempo ocioso: é o ambiente de execução, não o
sistema. Por isso H1 e H3 são reportadas por tipo de container, e o agregado não
deve ser lido como melhoria.

**A remediação varia 1,6% dentro de cada container; a detecção, 17%.** Os três
cenários que remedeiam por rotas independentes concordam dentro de cada tipo de
container (lento: C1 6,17 · C3 6,34 · C5 6,28; rápido: 5,14 · 5,03 · 5,11). A
dispersão do tempo de exposição vem do `t_detection` — latência de entrega do
CloudTrail ao EventBridge, fora do controle do sistema. A decomposição fecha
exatamente: `t_detection + t_remediation = t_exposure`.

**Cold start** foi a única amostra fria em 30: C1 na primeira execução, com
Analyze e Execute em containers novos. O `Init Duration` é só ~0,5s; o custo real
é o download do cache de provider dentro do handler.

**Leitura imediata na verificação, medida.** A rodada de verificação lia depois
de uma espera fixa de 15s, escolhida e não medida. Sem ela, a leitura acontece
**0,37s** (0,24–0,70) depois da última escrita da remediação, e as **283 leituras
de controle saíram `verified`** — como as 280 com espera. Taxa de leitura
desatualizada < ~1,1% com 95% de confiança, para os controles S3 remediados
(`scripts/verification_consistency.py`). Se ocorrer, o erro é seguro: escalação
indevida, nunca `S_nominal` falso. O `t_verified` caiu de 29,26s para 13,15s e
fica só 0,26s acima da exposição.

**Comparado às séries anteriores**, no container lento, que é o único tipo
presente nas três: `t_remediation` 6,03s (eco) → 6,20s (espera de 15s) → 6,24s
(atual). Os +0,18s do laço explícito são o estado `ScheduleVerification` (0,156s
medidos no histórico); a remoção da espera não mudou a remediação. As execuções
por rodada da suíte caíram de **48,5 (eco) para 36,0 (−26%)**, e um incidente
com N controles remediados passou de 1 + N para **no máximo 2** execuções.

A otimização chave: o provider OpenTofu é pré-inicializado e armazenado no S3 como cache (~128MB), baixado e extraído em `/tmp` no cold start, compartilhado via symlink entre controles. Elimina `tofu init` (~30-50s) de cada invocação.

Relatório com os gráficos interativos e a tabela completa: `results/report.html`
(dados brutos em `results/no-wait/run-NN.json`; séries anteriores em
`results/two-round/` e `results/`, regeneráveis com `scripts/build_report_data.py`).

## Licença

Projeto acadêmico (TCC).
