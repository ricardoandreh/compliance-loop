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
│              detecção → remedia (salvo disjuntor ou      │
│              controle detectivo); verificação → escala   │
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
ignora a role de remediação —, e a rodada de verificação nunca
remedeia, então o loop não tem como oscilar.

Só eventos emitidos pela própria AWS iniciam o laço: a regra exige `source`
com prefixo `aws.`, que o `PutEvents` recusa a qualquer outro emissor. Sem
isso, quem tivesse `events:PutEvents` na conta podia forjar um "AWS API Call
via CloudTrail" e disparar avaliações — medido na regra ativa.

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
role, e a role de remediação é limitada a 10 APIs de configuração (8 do S3, 2 do EC2), à própria
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
| Escalação | SQS FIFO + Lambda + GitHub API | Cria issues para as escalações (cinco motivos) e para os alarmes de operação |
| Scheduler | EventBridge Scheduler | Re-avalia após expiração de exceções |
| Controls Sync | DynamoDB Stream → Lambda | Regenera a regra do EventBridge ao inserir, modificar ou remover controles, excluindo eventos da própria remediação |

## Extensibilidade

Adicionar um novo controle que use leituras e ações já concedidas **não requer rebuild de nenhuma Lambda nem `apply` da infra**:

1. Criar `controls/<control-id>/policy.rego`, `remediation.tf` e `control.json`
2. Executar: `./scripts/deploy_control.sh <control-id>`

Se o controle usa APIs de remediação que a role ainda não concede, acrescente-as
a `local.remediation_actions` em `infra/modules/execute/main.tf` e rode o `apply`:
a mesma lista dá a permissão à role e alimenta a checagem do Execute, sem mudar
código. Um controle é um dado numa tabela, e por isso não pode ampliar sozinho as
próprias permissões.

O script faz upload para S3 e insert na tabela DynamoDB. O `controls-sync` atualiza o EventBridge automaticamente.

Se o controle lê a configuração por uma chamada que a Analyze ainda não faz
(`fetch_config`), acrescente a ação IAM a `local.resource_read_actions` em
`infra/modules/analyze/main.tf` e rode o `apply`. A role concede só as leituras
que os controles atuais fazem, e `tests/contract/test_analyze_permissions.py`
falha enquanto as duas coisas não baterem.

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
│       ├── analyze/                # Lambda Analyze + IAM (só as leituras dos controles)
│       ├── execute/                # Lambda Execute + IAM
│       ├── escalation_queue/       # SQS FIFO + DLQ
│       ├── escalation_lambda/      # Lambda Escalation + IAM
│       ├── controls_sync/          # Lambda que regenera a regra do EventBridge
│       ├── exception_scheduler/    # Lambda sync + EventBridge Scheduler
│       ├── alerting/               # SNS → SQS dos alarmes → Lambda de escalação
│       ├── operations/             # Alarmes, painel, fila intake-failures
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
    ├── unit/                       # Testes unitários e estruturais do grafo
    ├── property/                   # Property-based tests (Hypothesis)
    └── contract/                   # Contratos: política × remediação, permissões, AWS
```

## Pré-requisitos

- Uma conta AWS com permissão de administrador: o `apply` cria dez papéis IAM,
  uma trail do CloudTrail, tabelas, filas, funções e agendamentos. Região
  `us-east-1` por padrão. Não funciona em contas sem `iam:CreateRole` (AWS
  Academy Learner Lab, por exemplo): a autoridade por incidente e o canal único
  de exceções dependem de papéis dedicados
- AWS CLI v2 autenticada (`aws sts get-caller-identity` responde)
- Docker: o OpenTofu, o OPA dos testes e o build do cache do provider rodam em
  contêineres, sempre em versão fixada
- Python 3.14 com `boto3` para os scripts de teste e benchmark (o build do cache do provider aceita o `zstd` da linha de comando no lugar do 3.14):
  `python3.14 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`
- `jq`, `zip`, `unzip`, `curl`, `file`
- Um repositório no GitHub para as issues de escalação (ver *Escalação para o GitHub*)

Versões fixadas (nenhuma é `latest`):

| Peça | Versão | Onde está fixada |
|---|---|---|
| Runtime das Lambdas | Python 3.14 (arm64) | `infra/modules/*/main.tf` |
| OpenTofu (deploy e CI) | 1.12.6 | README, `.github/workflows/tests.yml` |
| OpenTofu (layer do Execute) | 1.12.6 | `scripts/build_execute.sh` |
| OPA (layer do Analyze, testes e CI) | 1.21.0 | `scripts/build_analyze.sh` |
| Provider AWS (infra) | 6.66.0 | `infra/main.tf`, `infra/.terraform.lock.hcl` |
| Provider AWS (cache do Execute) | 6.66.0 | `lambdas/execute/provider.tf` (`~> 6.0`), `scripts/build_provider_cache.sh` |
| Memória do Execute | 3008 MB | `infra/modules/execute/main.tf` (medido em `results/perf/`) |

As séries até `results/final/` foram medidas com OPA 1.4.2 e o provider 5.100.0;
a série somativa é `results/optimized/` e a décima, `results/revocation/` (ver
*Ameaças à validade* no `design.md`).

## Deploy do zero

Oito comandos, a partir da raiz do repositório. O `Makefile` tem um alvo para
cada um (`make bootstrap`, `make init`, `make apply`, `make controls`...).

```bash
# 0. Conta e região
aws sts get-caller-identity

# 1. Bucket de artefatos, backend, layers e cache do provider (idempotente)
bash scripts/bootstrap.sh            # cria <project>-artifacts-<conta>, infra/backend.dev.hcl e infra/dev.tfvars

# 2. Infraestrutura
tofu -chdir=infra init -reconfigure -backend-config=backend.dev.hcl
tofu -chdir=infra apply -var-file=dev.tfvars

# 3. Controles e regra de eventos
./scripts/deploy_control.sh --all    # publica controls/* no bucket e na tabela
aws lambda invoke --function-name compliance-loop-controls-sync-dev --payload '{}' /dev/stdout

# 4. Escalação (token e repositório do GitHub no SSM)
./scripts/setup_ssm_github.sh <token> <owner> <repo>

# 5. Um cenário, depois a suíte inteira
.venv/bin/python scripts/integration_test.py --test C1
.venv/bin/python scripts/integration_test.py
```

Uma trail recém-criada leva alguns minutos para começar a entregar eventos ao
EventBridge: numa implantação nova, os cenários disparados 1,2 e 3,4 min depois
do início da trail não geraram execução, e o de 4,3 min já gerou. A suíte
espera a trail completar cinco minutos antes de começar (`TRAIL_WARMUP_S=0`
desliga a espera).

Os scripts derivam os nomes dos recursos das mesmas três variáveis do
OpenTofu, lidas do ambiente: `AWS_REGION`, `PROJECT_NAME` e `ENVIRONMENT`
(padrões `us-east-1`, `compliance-loop`, `dev`; ver `scripts/deployment.py`).
Uma implantação com outro prefixo ou ambiente só precisa exportá-las.

**Dependências de rede do bootstrap.** O binário do OPA, o do OpenTofu e o
provider AWS vêm de *releases* no GitHub, que ficam fora do ar de vez em quando
(504 por horas, visto em 09/11/2026). Os scripts tentam de novo, o do OpenTofu
cai para a imagem `ghcr.io/opentofu/opentofu:1.12.6`, e as layers já
construídas podem ser mantidas com `--skip-layers`. O cache do provider não tem
mirror: com o registro indisponível, copie o de uma implantação existente
(`aws s3 cp s3://<bucket-antigo>/provider-cache/tf-dot-terraform.tar.zst
s3://<bucket-novo>/provider-cache/`) e rode o bootstrap com `--skip-cache`. O
`tofu init` da infraestrutura também baixa o provider do registro; um
`TF_PLUGIN_CACHE_DIR` reaproveita o que já foi baixado entre ambientes.

O bucket de artefatos guarda o state, `controls/*/policy.rego` e
`remediation.tf` e o cache do provider. Seu nome padrão é
`<project_name>-artifacts-<id da conta>`, e os scripts derivam o mesmo nome;
com outro nome (`--bucket` no bootstrap), exporte `ARTIFACT_BUCKET` antes de
chamar os scripts. O código das Lambdas é empacotado pelo `archive_file`
durante o `apply`; as layers (`lambdas/analyze/opa_layer.zip`,
`lambdas/execute/tofu_layer.zip`) são artefatos de build ignorados pelo git,
que o bootstrap gera.

**Depois de um `apply` que mude o padrão da regra**, regenere a regra viva
(passo 3): o OpenTofu não a atualiza (`ignore_changes`); quem a escreve é o
`controls-sync`.

**Este roteiro foi executado do zero em 09/11/2026**, como a cópia `repro` em
`us-east-2` da mesma conta: bootstrap, `apply` de 84 recursos, seis controles
publicados e 11 cenários (C1–C6, C8, C10–C12, CTRL-01) contra a cópia, com o
C8 parando na issue por falta dos parâmetros do GitHub. O que falhou no caminho
virou correção neste README e nos scripts: a espera pela trail, o *retry* e o
*fallback* dos downloads, a versão fixa do provider no cache, a remoção dos
`import` da conta original e a região obrigatória da cópia.

**Segunda cópia na mesma conta** (para reproduzir sem tocar a implantação em
uso): em **outra região**, com outro ambiente:

```bash
bash scripts/bootstrap.sh --region us-east-2 --env repro
make init apply controls sync-rule REGION=us-east-2 ENV=repro
AWS_DEFAULT_REGION=us-east-2 ./scripts/setup_ssm_github.sh <token> <owner> <repo>
make integration REGION=us-east-2 ENV=repro
```

Cada ambiente tem o seu par `infra/backend.<env>.hcl` e `infra/<env>.tfvars`,
e todos os nomes levam o sufixo do ambiente. A outra região é obrigatória: a
trail do CloudTrail e a regra do EventBridge são regionais, e dois laços na
mesma região reagiriam ambos a cada bucket e disputariam os mesmos recursos.
Os parâmetros do GitHub no SSM também são regionais.

**Custo**: uma execução da suíte (18 cenários, ~16 min) custa cerca de US$
0,04 em uso variável; a camada de operação (13 alarmes e o painel) custa US$
4,30 por mês enquanto estiver implantada. `make destroy ENV=<env>` remove tudo,
menos o bucket de artefatos, que guarda o estado e por isso é apagado à mão
depois (é versionado: todas as versões antes do bucket). A cópia `repro` foi
desmontada assim em 09/11/2026; o `destroy` parou uma vez no bucket de logs da
trail, que a trail enche enquanto existe, e o módulo passou a marcá-lo com
`force_destroy`.

> **Credenciais com `aws login`.** O provider AWS dentro do container do
> OpenTofu não lê a sessão do novo `aws login` (`~/.aws/login/cache`). Exporte
> credenciais temporárias e passe-as ao container:
>
> ```bash
> eval "$(aws configure export-credentials --format env)"
> docker run --rm -v "$PWD:/workspace" -w /workspace \
>   -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN -e AWS_REGION=us-east-1 \
>   ghcr.io/opentofu/opentofu:1.12.6 -chdir=infra apply
> ```

> **Não rode de dentro de `infra/`.** Os módulos de layer leem
> `../lambdas/*.zip`; um wrapper Docker que monta só o diretório atual não os
> enxerga e falha em `filebase64sha256`. Rodar da raiz com `-chdir=infra`
> monta o repositório inteiro.

### Escalação para o GitHub

Tudo o que o laço não resolve vira uma issue: as escalações dos cenários e os
alarmes da operação. A Lambda de escalação lê de `/compliance/github/*` no SSM
Parameter Store o token, o dono e o repositório (`scripts/setup_ssm_github.sh`
os grava). Serve qualquer repositório com issues habilitadas, inclusive um
vazio criado para isso; o token precisa só de leitura e escrita em issues
daquele repositório (fine-grained PAT). Sem os parâmetros, as escalações ficam
na fila, vão à DLQ e disparam o alarme `escalation-dlq`; o C8, que afirma a
issue ligada à linha da trilha, falha, e C4, C9 e CTRL-06 passam deixando as
escalações sem issue (visto na cópia `repro` em 09/11/2026). `scripts/close_test_issues.py`
fecha as issues que a suíte abre.

## Testes

| Camada | O que cobre | Onde | Quantidade |
|---|---|---|---|
| Unitários e de propriedade | Lambdas, estados do grafo, deduplicação de issues, alarmes que viram issue, sincronização de exceções, reprocessamento do intake | `tests/unit`, `tests/property` | 157 |
| Contrato | permissões de leitura da Analyze × leituras dos controles; remediação × política (OPA); padrão do EventBridge (identidades e origem forjada) e estado `ResolveResource` avaliados pela própria AWS; escrita na tabela de exceções só pelo papel `exception-writer` | `tests/contract` | 30 |
| Políticas Rego | casos conformes, violados e entradas enganosas, por controle | `controls/*/policy_test.rego` | 44 |
| Integração | 18 cenários contra a conta real (série `results/revocation/`) | `scripts/integration_test.py` | 18 |

A rastreabilidade de cada requisito e de cada propriedade de corretude até a
evidência que a sustenta está em
[`traceability.md`](.kiro/specs/mvp-compliance-automation-loop/traceability.md).
O CI (`.github/workflows/tests.yml`) roda as três primeiras camadas e valida o
OpenTofu a cada push, sem precisar de conta AWS.

```bash
# Offline: unitários, propriedade e contrato
.venv/bin/python -m pytest tests -q

# Testes de integração end-to-end (todos os cenários + controles)
.venv/bin/python scripts/integration_test.py

# Teste específico
.venv/bin/python scripts/integration_test.py --test C1
.venv/bin/python scripts/integration_test.py --test CTRL-03

# Com métricas em JSON
.venv/bin/python scripts/integration_test.py --json
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
.venv/bin/python scripts/benchmark.py --runs 10 --out results/<série>

# Reagrega o que já está em disco, sem tocar na AWS
.venv/bin/python scripts/benchmark.py --aggregate-only --out results/optimized
```

Reporta n, média, desvio amostral, mediana, amplitude e IC 95% por cenário e
por hipótese. Latências contam apenas rodadas em que o cenário passou; a taxa
de sucesso aparece ao lado. Métricas com n=1 são marcadas como observação
única, não como média.

### Reproduzir os números publicados

Os dados brutos de cada série estão em `results/<série>/run-NN.json`, uma
pasta por configuração do artefato, e as tags marcam o código que os produziu:
`v1.0.0` é a série somativa (`results/optimized/`) e `v1.1.0`, a décima
(`results/revocation/`). Tudo o que agrega esses arquivos roda sem conta AWS:

```bash
.venv/bin/python scripts/benchmark.py --aggregate-only --out results/optimized   # summary.{json,md}
.venv/bin/python scripts/compare_series.py                                       # Welch, permutação, Holm, g -> results/comparisons.json
python3 scripts/build_report_data.py --results results/optimized                 # report_data.json
```

O que lê a conta (`classify_cold_starts.py`, `verification_consistency.py`,
`audit_log_completeness.py`, `audit_cloudtrail_link.py`, `cost_model.py`,
`lambda_sizing.py`, `collect_series_facts.py`) consulta logs, tabelas e
o histórico do CloudTrail da janela da série, e só funciona enquanto eles
existem: 90 dias para o CloudTrail, 30 dias para os logs das Lambdas. Os
resultados dessas conferências estão gravados ao lado dos dados
(`log_audit.json`, `cloudtrail_link.json`, `cost_model.json`,
`series_facts.json`), e uma série nova (`make series SERIE=<nome>`) refaz o
caminho inteiro.

## Cenários validados

Todos aprovados em **10 de 10** execuções da suíte (160/160, `results/optimized/`).
Valores: média ± desvio. Nenhuma amostra foi fria nesta série (classificação por
`Init Duration` na invocação da Lambda, `scripts/classify_cold_starts.py`); as médias
de remediação juntam os dois patamares de ambiente — separados, estão em *Performance*.

| Cenário | Descrição | Resultado | Métrica (n=10) |
|---|---|---|---|
| C1 | Acesso público do S3 desligado pelo console | Remedia e verifica S_nominal | `t_remediation` 5,87s ± 0,81 |
| C2 | Acesso público com exceção ativa | Não remedia, registra S_exception | `t_classification` 0,49s ± 0,04 |
| C3 | Exceção expira → remediação | Scheduler dispara, remedia e verifica | `t_remediation` 6,09s ± 0,77 |
| C4 | Falha do sistema (bucket inexistente) | Sem estado; escala via GitHub Issue | `t_escalation` 0,45s ± 0,04 |
| C5 | Acesso público desligado por IaC externo | Detecta, remedia e verifica | `t_remediation` 5,68s ± 0,71 |
| C6 | Bucket já conforme | Confirma S_nominal, sem Execute | `t_classification` 0,49s ± 0,04 |
| C7 | Controle publicado e removido | DynamoDB Stream dispara controls-sync | `t_sync_insert` 15,62s ± 0,01 |
| C8 | Remediação negada pelo IAM | S_violation preservado, escala com o erro | `t_escalation` 5,98s ± 0,77 |
| C9 | Mesma violação repetida | Disjuntor escala na 4ª, sem remediar | `t_escalation` 0,73s ± 0,06 |
| C10 | Template adulterado para outro bucket | Sessão escopada nega a escrita | 10/10 |
| C11 | Exceção gravada fora do canal de aprovação | A tabela nega a escrita; a do papel `exception-writer` é aceita | 10/10 (`results/revocation/`) |
| C12 | Exceção revogada por status antes do vencimento | Reavalia na hora, remedia e verifica | 10/10: remediado 8,13 s ± 0,48 depois da revogação (`results/revocation/`) |
| AUDIT | Ausência de execuções falhas inesperadas | Nenhuma além das esperadas do C4, C8, C9 e CTRL-06 | 10/10 |

O **disjuntor disparou na quarta violação em 10 de 10 rodadas**: com limiar 3, as três
primeiras são remediadas e a quarta é a primeira a enxergar três remediações na janela.

## Controles validados

| Teste | Controle | Verificação | Resultado (n=10) |
|---|---|---|---|
| CTRL-01 | ctrl-s3-public-block | All 4 settings = true | `t_total` 29,10s ± 1,82 |
| CTRL-02 | ctrl-s3-ssl-only | DenyNonSSLRequests present | `t_total` 27,33s ± 1,35 |
| CTRL-03 | ctrl-s3-versioning | Versioning = Enabled | `t_total` 27,12s ± 0,71 |
| CTRL-04 | ctrl-s3-encryption | Classificado conforme (AES256 padrão) | `t_total` 26,97s ± 0,17 |
| CTRL-05 | ctrl-ebs-encryption-default | EbsEncryptionByDefault = true | `t_remediation` 5,77s ± 0,79 |
| CTRL-06 | ctrl-ebs-volume-encrypted (detectivo) | Volume sem criptografia escala sem remediação | `t_escalation` 0,86s ± 0,12 |

O `t_total` das CTRL-* mede o **teste**: criação de bucket, esperas de CloudTrail e a
espera pelas rodadas de verificação. Não é comparável ao `t_remediation`.

**Remediar EBS custa o mesmo que remediar S3**: o tempo do loop é dominado pela
inicialização do `tofu`, não pelo serviço. O **CTRL-06 é detectivo**: um volume existente
não pode ser criptografado no lugar, então o loop classifica e escala sem tocar no
recurso (L14).

## Performance

Agregado de **10 execuções completas da suíte** na configuração atual
(160/160 cenários aprovados, `results/optimized/`), via `scripts/benchmark.py`.
Desvio amostral (n−1) e IC 95% pela distribuição t.

Uma Lambda roda numa microVM que a AWS cria e reaproveita, e o hospedeiro varia: nesta
série, 123 das 470 invocações do Execute ficaram num patamar ~1,5s mais rápido, todas no
mesmo ambiente de execução. Por isso as comparações entre séries são feitas no **patamar
lento**, o único presente em todas (L12). Nenhuma das 80 amostras foi fria.

| Métrica | Quente (patamar lento) | Patamar rápido |
|---|---|---|
| **H1 — t_exposure** (C1 · C5) | **13,87s** ± 1,71 · IC [12,56 · 15,19] · n=9 | |
| MTTR — t_remediation, componente de H1 (C1 · C3 · C5) | **6,61s** ± 0,11 · IC [6,55 · 6,67] · n=15 | 5,15s ± 0,13 · n=15 |
| **H3 — t_verified** (convergência confirmada) | **14,19s** ± 1,70 · n=9 | |
| t_detection (evento → SFN start), componente de H1 | sem classe: 7,90s ± 1,46 · IC [7,21 · 8,58] · n=20 | |

**Toda a variação do MTTR entre as seis primeiras séries é atribuível, estado a estado:**
6,03s no laço por eco → +0,18s do estado que agenda a verificação → +0,28s da autoridade
por sessão = 6,54s. Leitura imediata, grafo de três estados, disjuntor, controle detectivo
e endurecimento das identidades de serviço **não** mexeram no MTTR.

**A atualização de versões custou mais que qualquer mudança de desenho, e a
otimização do Execute a devolveu.** Com OPA 1.21.0, OpenTofu 1.12.6 e o provider AWS
6.66.0 (`results/upgrade/`), a remediação quente foi a **7,59s ± 0,15** (+1,06s,
p < 0,0001), quase toda no Execute, que carrega um provider 19% maior a cada
`apply`. O Python 3.14 não mudou nada (`results/py314/`, −0,01s, p = 0,83). Com o
Execute a 3008 MB, o cache do provider em zstd baixado no init, os `skip_*` do provider
e o cliente escopado sob demanda (`results/optimized/`), voltou a
**6,61s ± 0,11** (−0,97s, p < 0,0001) — o nível da série final, com as versões novas.
O Python do handler responde por 3% do tempo do Execute; os outros 97% são o `tofu`
(`results/perf/`).

**A exposição é dominada pela detecção:** desvio de 1,46s na detecção contra 0,11s na
remediação, com `t_detection + t_remediation = t_exposure` em todas as execuções. Reduzir
a exposição exigiria trocar o Monitor, não otimizar o loop.

**Leitura imediata na verificação, medida.** Somando as sete séries sem espera fixa são
**2.760 leituras de controle, todas conformes**, feitas ~0,35s depois da escrita. O
`t_verified` fica **0,32s** acima da exposição, contra 15,4s quando havia a espera de 15s.

**Cold start** é raro e não custa sempre o mesmo: ~10s de Execute frio a 3008 MB
(~3,8s de init, que baixa e descompacta o cache, + ~6,1s de handler), contra 16–17s
no upgrade, quando o cache ainda era baixado no handler.

A otimização chave: o provider OpenTofu é pré-inicializado e armazenado no S3 como cache
(123 MB em zstd, 780 MB extraído, provider 6.66.0), baixado e extraído em `/tmp` no init
e compartilhado via symlink entre controles. Elimina `tofu init` (~30-50s) de cada invocação.

Cada série fica numa pasta própria — de `results/echo/` a `results/revocation/`, dez ao
todo; `results/optimized/` é a série somativa da monografia — com os `run-NN.json` brutos. Os dados derivados de cada série são regenerados assim:

```bash
S=results/optimized
# Com acesso à conta (uma vez por série, enquanto o histórico existe):
.venv/bin/python scripts/classify_cold_starts.py --results $S
.venv/bin/python scripts/verification_consistency.py --results $S
.venv/bin/python scripts/collect_series_facts.py --results $S
# Offline, a partir dos arquivos acima:
python3 scripts/build_report_data.py --results $S --out $S/report_data.json
```

O Step Functions guarda o histórico por 90 dias; os três primeiros passos gravam
em cada série o que só existe na conta (classificação frio/quente, consistência
de leitura, containers do Execute e execuções por rodada), e daí em diante a
análise é refeita só com o repositório.

## Operação

O laço tem alarmes para as falhas que **não** viram escalação — o laço parado,
não o laço trabalhando: detecção perdida no EventBridge, expiração de exceção
não reavaliada, falha por registro na sincronização de exceções, registro de
stream abandonado, queda de Lambda, timeout e throttling do Step Functions.
Cada disparo vira uma **issue no GitHub** (rótulo `operations-alarm`), aberta
pela mesma Lambda das escalações: um canal só para tudo o que exige uma pessoa.
A volta a OK comenta na issue, e quem a fecha é quem seguiu o runbook. O
caminho passa por uma fila, então uma queda do GitHub atrasa a notificação em
vez de perdê-la. Os alarmes aparecem também no painel `compliance-loop-dev-loop`,
que é o que resta se o próprio canal do GitHub cair (token expirado, por
exemplo): o tópico tem uma assinatura só, por decisão. O runbook, com o que cada alarme significa e a
primeira ação, está no `design.md` (*Operação: alarmes e runbook*).

Nada que o laço deixa de processar some. Os consumidores dos streams do
DynamoDB têm tentativas limitadas e uma fila de falhas. Uma detecção que o
EventBridge não consegue entregar à máquina de estados (por exemplo, a role
perdeu a permissão) vai para a fila `intake-failures`, e
`scripts/replay_intake.py` inicia depois a execução que ela deveria ter
iniciado — no máximo uma por evento. Os logs das Lambdas têm retenção de 30
dias; o `violations_log` e a tabela de exceções têm recuperação pontual; e a
trilha do CloudTrail tem validação de integridade, para que a evidência
independente usada na H2 seja à prova de adulteração — `validate-logs`: 11 de
11 arquivos válidos desde que foi ligada, depois da série final.

As duas falhas silenciosas mais graves foram provocadas na conta real
(`scripts/ops_fault_injection.py`, resultado em
`results/ops_fault_injection.json`): uma exceção que a sincronização não
consegue agendar e uma detecção recusada por falta de permissão. Nos dois casos
o alarme disparou — o primeiro em ~1 min, o da fila em ~3 min, tempos ditados
pelo período das métricas — e abriu a sua issue no GitHub no mesmo segundo; a
volta a OK virou comentário nela. A detecção recusada ficou na fila até ser
reprocessada, avaliando o bucket como a original teria avaliado.

**Reconciliação periódica (opcional).** O laço é orientado a eventos: o que o
CloudTrail não entrega não é avaliado. Uma varredura periódica é configuração,
não mudança de modelo: um agendamento no EventBridge Scheduler que inicia a
máquina de estados para cada recurso com a entrada de um `CreateBucket` (ou
`CreateVolume`), que já avalia todos os controles daquele recurso — ou, para
controles de conta, com o `control_id`, como a expiração de uma exceção já faz.
Não está ativada; desenho e custo (~US$ 0,0005 por recurso por varredura) no
`design.md`, L15.

## Custo

Medido na série atual (`scripts/cost_model.py`, `results/optimized/`), com
preços de lista de us-east-1: **~US$ 0,0014 por incidente remediado** (US$ 1,40
a cada mil). A maior parcela são as transições do Step Functions Standard (66%),
seguidas pelo processamento das Lambdas (27%, quase todo do Execute a 3008 MB) e
pela ingestão de logs (8%). A camada de
operação custa ~US$ 1,30/mês em alarmes, mais US$ 3,00/mês do painel acima dos
três gratuitos.

## Licença

Projeto acadêmico (TCC).
