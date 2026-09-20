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

Todos aprovados em **10 de 10** execuções da suíte (160/160, `results/session-policy/`).
Valores: média ± desvio, **separados em regime quente e frio** por evidência de
`Init Duration` na invocação da Lambda (`scripts/classify_cold_starts.py`). A única
amostra fria das 10 execuções é do C5.

| Cenário | Descrição | Resultado | Quente | Frio |
|---|---|---|---|---|
| C1 | Violação simples via console | Remedia e verifica S_nominal | `t_remediation` 6,56s ± 0,14 | nenhuma |
| C2 | Violação com exceção ativa | Não remedia, registra S_exception | `t_classification` 0,49s ± 0,04 | nenhuma |
| C3 | Exceção expira → remediação | Scheduler dispara, remedia e verifica | `t_remediation` 6,53s ± 0,10 | nenhuma |
| C4 | Falha do sistema (bucket inexistente) | Sem estado; escala via GitHub Issue | `t_escalation` 0,46s ± 0,03 | nenhuma |
| C5 | Violação originada por IaC externo | Detecta, remedia e verifica | `t_remediation` 6,52s ± 0,08 (n=9) | 8,45s (n=1) |
| C6 | Recurso já conforme | Confirma S_nominal, sem Execute | `t_classification` 0,47s ± 0,05 | nenhuma |
| C7 | Novo controle → EventBridge atualizado | DynamoDB Stream dispara controls-sync | 15,73s ± 0,22 | — |
| C8 | Remediação falha (Execute negado) | S_violation preservado, escala com o erro | `t_escalation` 6,27s ± 0,08 | nenhuma |
| C9 | Violação reincidente | Disjuntor escala sem remediar de novo | `t_escalation` 0,76s ± 0,05 | nenhuma |
| C10 | Sessão de remediação escopada | Template adulterado barrado por IAM | 10/10 | — |
| AUDIT | Ausência de execuções falhas inesperadas | Nenhuma além das esperadas do C4, C8, C9 e CTRL-06 | — | — |

O **disjuntor disparou na quarta violação em 10 de 10 rodadas**: com limiar 3, as três
primeiras são remediadas e a quarta é a primeira a enxergar três remediações na janela.
No C8, uma bucket policy nega `s3:PutBucketPublicAccessBlock` à role da remediação,
então o `AccessDenied` é real: o estado fica `S_violation` com `remediation_status=failed`
e a issue é vinculada à linha da violação.

## Controles validados

| Teste | Controle | Verificação | Resultado (n=10) |
|---|---|---|---|
| CTRL-01 | ctrl-s3-public-block | All 4 settings = true | `t_total` 30,90s ± 0,21 |
| CTRL-02 | ctrl-s3-ssl-only | DenyNonSSLRequests present | `t_total` 27,29s ± 1,22 |
| CTRL-03 | ctrl-s3-versioning | Versioning = Enabled | `t_total` 26,79s ± 0,08 |
| CTRL-04 | ctrl-s3-encryption | SSEAlgorithm = AES256 | `t_total` 26,91s ± 0,10 |
| CTRL-05 | ctrl-ebs-encryption-default | EbsEncryptionByDefault = true | `t_remediation` 6,51s ± 0,24 |
| CTRL-06 | ctrl-ebs-volume-encrypted (detectivo) | Volume sem criptografia escala sem remediação | `t_escalation` 0,85s ± 0,07 |

O `t_total` das CTRL-* mede o **teste**: criação de bucket, esperas de CloudTrail e a
espera pelas rodadas de verificação. Não é comparável ao `t_remediation`.

**Remediar EBS custa o mesmo que remediar S3** (6,51s contra 6,54s): o tempo do loop
é dominado pela inicialização do `tofu`, não pelo serviço. O **CTRL-06 é detectivo**:
`EbsEncryptionByDefault` só vale para volumes criados depois, e um volume existente não
pode ser criptografado no lugar — o loop classifica e escala, sem tocar no recurso (L14).

## Performance

Agregado de **10 execuções completas da suíte** na configuração final
(160/160 cenários aprovados, `results/session-policy/`), via `scripts/benchmark.py`.
Desvio amostral (n−1) e IC 95% pela distribuição t. **Quente e frio são reportados
separadamente**, classificados por evidência (`Init Duration` na invocação exata da
Lambda), sem corte de duração.

| Métrica | Quente | Frio | Total |
|---|---|---|---|
| **H1 — t_remediation** (C1 · C3 · C5) | **6,54s** ± 0,11 · IC [6,50 · 6,58] · n=29 | **8,45s** (n=1) | 6,60s ± 0,36 · n=30 |
| **H3 — t_exposure** (C1 · C5) | **14,24s** ± 1,63 · IC [13,46 · 15,02] · n=19 | **15,90s** (n=1) | 14,32s ± 1,63 · n=20 |
| **t_verified** (convergência confirmada) | **14,54s** ± 1,63 · IC [13,76 · 15,33] · n=19 | **16,23s** (n=1) | 14,63s ± 1,63 · n=20 |
| t_detection (evento → SFN start) | sem classe: 7,68s ± 1,58 · IC [6,95 · 8,42] · n=20 | | |
| t_remediation EBS (CTRL-05) | 6,51s ± 0,24 · n=10 | nenhuma | |

**Toda a variação acumulada do MTTR entre as séries é atribuível, estado a estado:**
6,03s no laço por eco → +0,18s do estado que agenda a verificação → +0,28s da sessão
escopada = 6,54s. As demais mudanças (leitura imediata, grafo de três estados, disjuntor
e controle detectivo) não mexeram no MTTR.

**O menor privilégio dinâmico custou 4,5% do MTTR.** Os cinco caminhos que remedeiam
subiram juntos (C1 +0,30 · C3 +0,26 · C5 +0,28 · CTRL-05 +0,27 · C8 +0,24) e os que não
remedeiam ficaram parados (C4 0,46 · C2 0,49 · C6 0,47). Dentro do Execute, a duração
mediana subiu 0,20s — o `sts:AssumeRole`.

**A remediação varia 1,7%; a detecção, 21%.** Quatro caminhos independentes, em dois
serviços, concordam dentro de 0,05s (C1 6,56 · C3 6,53 · C5 6,52 · CTRL-05 6,51). A
dispersão da exposição vem do `t_detection` — latência de entrega do CloudTrail, fora do
controle do sistema. A decomposição fecha exatamente:
`t_detection + t_remediation = t_exposure`.

**Leitura imediata na verificação, medida.** A verificação lia depois de uma espera fixa
de 15s, escolhida e não medida. Sem ela, a leitura acontece **0,36s** depois da escrita, e
somando as três séries sem espera são **1.033 leituras de controle, todas conformes**
(taxa de leitura desatualizada < ~0,3% com 95% de confiança). O `t_verified` ficou
**0,30s** acima da exposição, contra 15,4s quando havia a espera.

**Cold start** é raro e nem sempre custa o mesmo: a amostra fria desta série custou
+1,93s, porque só o Analyze subiu do zero; quando o Execute também sobe, o custo é ~8,2s,
quase todo o download do cache do provider dentro do handler.

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
