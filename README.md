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
| **S_violation** | Não conforme, sem exceção | Remediação automática; escalação na rodada de verificação (não convergência), na reincidência (disjuntor) ou se o controle for detectivo |

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
| `ctrl-ebs-volume-encrypted` | EC2 | Volume EBS criptografado | **detectivo**: escala, não remedia (L14) |

## Autoridade da remediação

A Lambda Execute não tem permissão de escrita. Para cada violação ela assume a
role `compliance-loop-remediation-dev` com uma **session policy** que nomeia o
recurso que o Analyze classificou e as ações que aquele controle declarou, e usa
`violation_id` como nome da sessão — então cada escrita aparece no CloudTrail
ligada à violação que a motivou.

Como o IAM intersecta session policy e role, a sessão nunca é mais ampla que a
role, e a role de remediação é limitada às 8 APIs de configuração, à própria
conta, com `Deny` sobre o bucket do CloudTrail e o do tfstate — o laço não
reescreve a própria evidência. O cenário C10 demonstra: um `remediation.tf`
adulterado apontando para outro bucket é barrado com `AccessDenied`.

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
│   ├── ctrl-ebs-encryption-default/
│   └── ctrl-ebs-volume-encrypted/   # detectivo: sem remediation.tf
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

| Camada | O que cobre | Onde | Quantidade |
|---|---|---|---|
| Unitários e de propriedade | Lambdas, estados do grafo, deduplicação de issues, sincronização de exceções | `tests/unit`, `tests/property` | 126 |
| Contrato | remediação × política (OPA); padrão do EventBridge e estado `ResolveResource` avaliados pela própria AWS | `tests/contract` | 22 |
| Políticas Rego | casos conformes, violados e entradas enganosas, por controle | `controls/*/policy_test.rego` | 44 |
| Integração | 16 cenários contra a conta real | `scripts/integration_test.py` | 16 |

A rastreabilidade de cada requisito e de cada propriedade de corretude até a
evidência que a sustenta está em
[`traceability.md`](.kiro/specs/mvp-compliance-automation-loop/traceability.md).
O CI (`.github/workflows/tests.yml`) roda as três primeiras camadas e valida o
OpenTofu a cada push, sem precisar de conta AWS.

```bash
# Offline: unitários, propriedade e contrato
lambdas/analyze/.venv/bin/python -m pytest tests -q

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

Todos aprovados em **10 de 10** execuções da suíte (160/160, `results/final/`).
Valores: média ± desvio, **separados em regime quente e frio** por evidência de
`Init Duration` na invocação da Lambda (`scripts/classify_cold_starts.py`).

| Cenário | Descrição | Resultado | Quente | Frio |
|---|---|---|---|---|
| C1 | Violação simples via console | Remedia e verifica S_nominal | `t_remediation` 6,35s ± 0,46 (n=9) | 11,69s |
| C2 | Violação com exceção ativa | Não remedia, registra S_exception | `t_classification` 0,50s ± 0,07 | nenhuma |
| C3 | Exceção expira → remediação | Scheduler dispara, remedia e verifica | `t_remediation` 6,46s ± 0,39 | nenhuma |
| C4 | Falha do sistema (bucket inexistente) | Sem estado; escala via GitHub Issue | `t_escalation` 0,46s ± 0,03 | nenhuma |
| C5 | Violação originada por IaC externo | Detecta, remedia e verifica | `t_remediation` 6,53s ± 0,19 (n=9) | 10,43s |
| C6 | Recurso já conforme | Confirma S_nominal, sem Execute | `t_classification` 0,48s ± 0,04 (n=9) | 1,22s |
| C7 | Novo controle → EventBridge atualizado | DynamoDB Stream dispara controls-sync | 15,63s ± 0,04 | — |
| C8 | Remediação negada por IAM | S_violation preservado, escala com o erro | `t_escalation` 6,04s ± 0,51 | nenhuma |
| C9 | Violação reincidente | Disjuntor escala sem remediar de novo | `t_escalation` 0,73s ± 0,05 | nenhuma |
| C10 | Sessão de remediação escopada | Template adulterado barrado por IAM | 10/10 | — |
| AUDIT | Ausência de execuções falhas inesperadas | Nenhuma além das esperadas do C4, C8, C9 e CTRL-06 | — | — |

O **disjuntor disparou na quarta violação em 10 de 10 rodadas**: com limiar 3, as três
primeiras são remediadas e a quarta é a primeira a enxergar três remediações na janela.

## Controles validados

| Teste | Controle | Verificação | Resultado (n=10) |
|---|---|---|---|
| CTRL-01 | ctrl-s3-public-block | All 4 settings = true | `t_total` 31,53s ± 3,12 |
| CTRL-02 | ctrl-s3-ssl-only | DenyNonSSLRequests present | `t_total` 26,95s ± 0,17 |
| CTRL-03 | ctrl-s3-versioning | Versioning = Enabled | `t_total` 26,84s ± 0,22 |
| CTRL-04 | ctrl-s3-encryption | SSEAlgorithm = AES256 | `t_total` 27,33s ± 1,22 |
| CTRL-05 | ctrl-ebs-encryption-default | EbsEncryptionByDefault = true | `t_remediation` 6,07s ± 0,49 |
| CTRL-06 | ctrl-ebs-volume-encrypted (detectivo) | Volume sem criptografia escala sem remediação | `t_escalation` 0,78s ± 0,05 |

O `t_total` das CTRL-* mede o **teste**: criação de bucket, esperas de CloudTrail e a
espera pelas rodadas de verificação. Não é comparável ao `t_remediation`.

**Remediar EBS custa o mesmo que remediar S3**: o tempo do loop é dominado pela
inicialização do `tofu`, não pelo serviço. O **CTRL-06 é detectivo**: um volume existente
não pode ser criptografado no lugar, então o loop classifica e escala sem tocar no
recurso (L14).

## Performance

Agregado de **10 execuções completas da suíte** na configuração final
(160/160 cenários aprovados, `results/final/`), via `scripts/benchmark.py`.
Desvio amostral (n−1) e IC 95% pela distribuição t.

Uma Lambda roda numa microVM que a AWS cria e reaproveita, e o hospedeiro varia: nesta
série, 49 das 470 invocações do Execute ficaram num patamar ~1,2s mais rápido, todas no
mesmo ambiente de execução. Por isso as comparações entre séries são feitas no **patamar
lento**, o único presente em todas (L12).

| Métrica | Quente (patamar lento) | Frio |
|---|---|---|
| **H1 — t_remediation** (C1 · C3 · C5) | **6,54s** ± 0,14 · IC [6,48 · 6,60] · n=26 | 10,43s e 11,69s |
| **H3 — t_exposure** (C1 · C5) | **14,56s** ± 1,74 · IC [13,67 · 15,46] · n=17 | 18,52s e 18,95s |
| **t_verified** (convergência confirmada) | **14,87s** ± 1,75 · n=17 | — |
| t_detection (evento → SFN start) | sem classe: 8,13s ± 1,68 · IC [7,34 · 8,92] · n=20 | |

**Toda a variação do MTTR entre as seis séries é atribuível, estado a estado:**
6,03s no laço por eco → +0,18s do estado que agenda a verificação → +0,28s da autoridade
por sessão = 6,54s. Leitura imediata, grafo de três estados, disjuntor, controle detectivo
e endurecimento das identidades de serviço **não** mexeram no MTTR — o último foi medido
em 6,54s ± 0,14 contra 6,54s ± 0,11 da série anterior.

**A exposição é dominada pela detecção:** desvio de 1,68s na detecção contra 0,14s na
remediação, com `t_detection + t_remediation = t_exposure` em todas as execuções. Reduzir
a exposição exigiria trocar o Monitor, não otimizar o loop.

**Leitura imediata na verificação, medida.** Somando as quatro séries sem espera fixa são
**1.463 leituras de controle, todas conformes**, feitas ~0,35s depois da escrita (taxa de
leitura desatualizada < ~0,2% com 95% de confiança). O `t_verified` fica **0,31s** acima da
exposição, contra 15,4s quando havia a espera de 15s.

**Cold start** é raro e não custa sempre o mesmo: +4 a +5s quando o Execute sobe do zero e
baixa o cache do provider, +1,9s quando só o Analyze está frio, +0,7s numa classificação
sem remediação.

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

## Operação

O laço tem alarmes para as falhas que **não** viram escalação — o laço parado,
não o laço trabalhando: detecção perdida no EventBridge, expiração de exceção
não reavaliada, falha por registro na sincronização de exceções, registro de
stream abandonado, queda de Lambda, timeout e throttling do Step Functions.
Todos publicam no tópico `compliance-loop-alerts-dev` (e-mail opcional pela
variável `alert_email`) e aparecem no painel `compliance-loop-dev-loop`. O
runbook, com o que cada alarme significa e a primeira ação, está no
`design.md` (*Operação: alarmes e runbook*).

Os consumidores dos streams do DynamoDB têm tentativas limitadas e uma fila de
falhas; os logs das Lambdas têm retenção de 30 dias; o `violations_log` e a
tabela de exceções têm recuperação pontual; e a trilha do CloudTrail tem
validação de integridade, para que a evidência independente usada na H2 seja à
prova de adulteração.

## Custo

Medido na série final (`scripts/cost_model.py`), com preços de lista de
us-east-1: **~US$ 0,0013 por incidente remediado** (US$ 1,30 a cada mil). A
maior parcela são as transições do Step Functions Standard (71%), seguidas pelo
processamento das Lambdas (21%) e pela ingestão de logs (8%). A camada de
operação custa ~US$ 1,30/mês em alarmes, mais US$ 3,00/mês do painel acima dos
três gratuitos.

## Licença

Projeto acadêmico (TCC).
