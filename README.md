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
# 10 execuções -> results/<série>/run-NN.json + summary.{json,md}
# Cada série é uma configuração do sistema: uma pasta por série, nunca misturadas
lambdas/analyze/.venv/bin/python scripts/benchmark.py --runs 10 --out results/<série>

# Reagrega o que já está em disco, sem tocar na AWS
lambdas/analyze/.venv/bin/python scripts/benchmark.py --aggregate-only --out results/three-state
```

Reporta n, média, desvio amostral, mediana, amplitude e IC 95% por cenário e
por hipótese. Latências contam apenas rodadas em que o cenário passou; a taxa
de sucesso aparece ao lado. Métricas com n=1 são marcadas como observação
única, não como média.

## Cenários validados

Todos aprovados em **10 de 10** execuções da suíte (140/140, `results/three-state/`).
Valores: média ± desvio, **separados em regime quente e frio** por evidência de
`Init Duration` na invocação da Lambda (`scripts/classify_cold_starts.py`). A única
amostra fria das 10 execuções é do C1.

| Cenário | Descrição | Resultado | Quente | Frio |
|---|---|---|---|---|
| C1 | Violação simples via console | Remedia e verifica S_nominal | `t_remediation` 6,26s ± 0,11 (n=9) | 14,44s (n=1) |
| C2 | Violação com exceção ativa | Não remedia, registra S_exception | `t_classification` 0,48s ± 0,07 | nenhuma |
| C3 | Exceção expira → remediação | Scheduler dispara, remedia e verifica | `t_remediation` 6,27s ± 0,12 | nenhuma |
| C4 | Falha do sistema (bucket inexistente) | Sem estado; escala via GitHub Issue | `t_escalation` 0,48s ± 0,05 | nenhuma |
| C5 | Violação originada por IaC externo | Detecta, remedia e verifica | `t_remediation` 6,24s ± 0,11 | nenhuma |
| C6 | Recurso já conforme | Confirma S_nominal, sem Execute | `t_classification` 0,47s ± 0,03 | nenhuma |
| C7 | Novo controle → EventBridge atualizado | DynamoDB Stream dispara controls-sync | não classificado: 15,62s ± 0,03 | — |
| C8 | Remediação falha (Execute sem permissão) | S_violation preservado, escala com o erro | `t_escalation` 6,03s ± 0,13 | nenhuma |
| AUDIT | Ausência de execuções falhas inesperadas | Nenhuma além das esperadas do C4 e do C8 | — | — |

No C8, uma bucket policy nega `s3:PutBucketPublicAccessBlock` só à role do
Execute: o Analyze classifica `S_violation`, a remediação falha com
`AccessDenied`, o estado é preservado com `remediation_status=failed` e a issue é
vinculada à linha da violação. O C4 não cobre esse caminho — um bucket
inexistente nem chega a ser avaliado. O C4 ficou 0,10s mais rápido que nas séries
anteriores: é o `putItem` do antigo `RegisterError`, que deixou de existir.

## Controles validados

| Teste | Controle | Verificação | t_total (n=10) |
|---|---|---|---|
| CTRL-01 | ctrl-s3-public-block | All 4 settings = true | 29,56s ± 2,43 |
| CTRL-02 | ctrl-s3-ssl-only | DenyNonSSLRequests present | 26,18s ± 1,05 |
| CTRL-03 | ctrl-s3-versioning | Versioning = Enabled | 25,91s ± 0,09 |
| CTRL-04 | ctrl-s3-encryption | SSEAlgorithm = AES256 | 26,27s ± 0,04 |
| CTRL-05 | ctrl-ebs-encryption-default | EbsEncryptionByDefault = true | 28,54s ± 0,23 |

O `t_total` das CTRL-* mede o **teste**: criação de bucket, esperas de CloudTrail
e a espera pelas rodadas de verificação, já que o harness só encerra quando
nenhuma execução está em andamento. Não é comparável ao `t_remediation` nem é
classificado em quente/frio.

O **CTRL-05** desliga a criptografia padrão do EBS na região, exige a remediação e
a rodada de verificação, e religa a configuração se o loop não o fizer. Não cria
nenhum recurso cobrável — é uma configuração da conta. A remediação de EBS custa
o mesmo que a de S3: `t_remediation` **6,24s ± 0,24** (n=10), contra 6,26s — o
tempo do loop é dominado pela inicialização do `tofu`, não pelo serviço.

O controle é **preventivo**: `EbsEncryptionByDefault` só vale para volumes criados
depois. Um volume existente sem criptografia continua assim, e o controle fica
conforme — a AWS não criptografa nem troca a chave de um volume no lugar (é
snapshot → cópia → volume novo), o que não é remediação para aplicar
automaticamente. Ver L14 no `design.md`.

## Performance

Agregado de **10 execuções completas da suíte** com o grafo de três estados,
leitura imediata na verificação, C8 e CTRL-05 (140/140 cenários aprovados,
`results/three-state/`), via `scripts/benchmark.py`. Desvio amostral (n−1) e IC 95%
pela distribuição t. **Quente e frio são reportados separadamente**,
classificados por evidência (`Init Duration` na invocação exata da Lambda), sem
corte de duração. Uma amostra fria com n=1 é reportada como observação única.

| Métrica | Quente | Frio | Total |
|---|---|---|---|
| **H1 — t_remediation** (C1 · C3 · C5) | **6,26s** ± 0,11 · IC [6,22 · 6,30] · n=29 | **14,44s** (n=1) | 6,53s ± 1,50 · mediana 6,26 · n=30 |
| **H3 — t_exposure** (C1 · C5) | **13,31s** ± 1,77 · IC [12,46 · 14,17] · n=19 | **23,50s** (n=1) | 13,82s ± 2,86 · n=20 |
| **t_verified** (convergência confirmada) | **13,58s** ± 1,76 · IC [12,73 · 14,43] · n=19 | **23,84s** (n=1) | 14,09s ± 2,86 · n=20 |
| t_detection (evento → SFN start) | sem classe: 7,16s ± 1,81 · IC [6,31 · 8,01] · n=20 | | |
| t_remediation EBS (CTRL-05) | 6,24s ± 0,24 · IC [6,07 · 6,41] · n=10 | nenhuma | |
| t_classification (recurso conforme) | 0,47–0,48s · n=10 | nenhuma | |
| t_escalation, falha de remediação (C8) | 6,03s ± 0,13 · n=10 | nenhuma | |
| t_escalation, falha do sistema (C4) | 0,48s ± 0,05 · n=10 | nenhuma | |

**A remediação varia 1,8%; a detecção, 25%.** Os três cenários que remedeiam por
rotas independentes concordam dentro de 0,03s (C1 6,26 · C3 6,27 · C5 6,24), e o
CTRL-05, sobre outro serviço, cai no mesmo valor (6,24). A dispersão do tempo de
exposição vem do `t_detection` — latência de entrega do CloudTrail ao
EventBridge, fora do controle do sistema. A decomposição fecha exatamente:
`t_detection + t_remediation = t_exposure`.

**"Quente" não é um regime só.** Na série anterior (leitura imediata), o Execute
rodou ~1,1s mais rápido em alguns containers da Lambda: 98 das 283 invocações, em
2 dos 5 containers, e as 17 amostras rápidas de `t_remediation` num só deles. O
agregado daquela série (5,56s) sugeria uma melhoria que era do container. Nesta
série nenhum container foi rápido e o valor voltou a 6,26s — o patamar lento das
séries anteriores (6,24s e 6,20s). Por isso as séries são comparadas por tipo de
container.

**Cold start** foi a única amostra fria em 30: C1 na primeira execução, com
Analyze e Execute em containers novos. O `Init Duration` é só ~0,5s; o custo real
é o download do cache de provider dentro do handler.

**Leitura imediata na verificação, medida.** A rodada de verificação lia depois
de uma espera fixa de 15s, escolhida e não medida. Sem ela, a leitura acontece
**0,37s** depois da última escrita da remediação, e as **603 leituras de controle**
das duas séries sem espera saíram `verified` (10 delas do EBS) — como as 280 com
espera. Taxa de leitura desatualizada < ~0,5% com 95% de confiança
(`scripts/verification_consistency.py`). Se ocorrer, o erro é seguro: escalação
indevida, nunca `S_nominal` falso. O `t_verified` caiu de 29,26s para ~13,6s e
fica só ~0,27s acima da exposição.

**Comparado às séries anteriores**, no container lento: `t_remediation` 6,03s
(eco) → 6,20s (espera de 15s) → 6,24s (leitura imediata) → 6,26s (três estados).
Os +0,18s do laço explícito são o estado `ScheduleVerification` (0,156s medidos
no histórico); nem a remoção da espera nem o grafo de três estados mudaram a
remediação. Um incidente com N controles remediados passou de 1 + N para **no
máximo 2** execuções; as execuções por rodada da suíte foram de 48,5 (eco) a 36,0,
e sobem a 43,0 na série atual só porque C8 e CTRL-05 entraram na suíte.

A otimização chave: o provider OpenTofu é pré-inicializado e armazenado no S3 como cache (~128MB), baixado e extraído em `/tmp` no cold start, compartilhado via symlink entre controles. Elimina `tofu init` (~30-50s) de cada invocação.

Relatório com os gráficos interativos e a tabela completa: `results/report.html`.
Cada série fica numa pasta própria — `results/echo/`, `results/two-round/`,
`results/no-wait/` e `results/three-state/` (a atual) — com os `run-NN.json` brutos,
e a página é regenerada a partir delas:

```bash
S=results/three-state
# Com acesso à conta (uma vez por série, enquanto o histórico existe):
lambdas/analyze/.venv/bin/python scripts/classify_cold_starts.py --results $S
lambdas/analyze/.venv/bin/python scripts/verification_consistency.py --results $S
lambdas/analyze/.venv/bin/python scripts/report/collect_series_facts.py --results $S
# Offline, a partir dos arquivos acima:
python3 scripts/build_report_data.py --results $S --out $S/report_data.json
python3 scripts/report/render_report.py        # -> results/report.html
```

O Step Functions guarda o histórico por 90 dias; os três primeiros passos gravam
em cada série o que só existe na conta (classificação frio/quente, consistência
de leitura, containers do Execute e execuções por rodada), e daí em diante a
página é reconstruída só com o repositório. O CSS, os gráficos e as notas de
metodologia ficam em `scripts/report/`.

## Licença

Projeto acadêmico (TCC).
