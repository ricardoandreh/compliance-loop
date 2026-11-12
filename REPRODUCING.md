# Reprodução

Há dois níveis. O primeiro refaz as análises a partir dos dados do repositório
e não precisa de conta. O segundo implanta o laço numa conta AWS e mede de novo.

> **Antes de implantar.** O segundo nível cria recursos faturáveis numa conta
> AWS real e exige permissão de administrador: dez papéis IAM, uma trail do
> CloudTrail, tabelas, filas, funções e alarmes. Use uma conta de teste. O custo
> e a desmontagem estão no fim de *Implantação do zero*.

## O que a monografia afirma e como conferir

| Afirmação | Como conferir | Onde está o resultado |
|---|---|---|
| H1: a exposição de uma violação fica na ordem de segundos | `scripts/benchmark.py --aggregate-only --out results/optimized` | `results/optimized/summary.md` |
| H2: cada remediação deixa trilha completa e ligada ao CloudTrail | arquivos já gravados; refazer exige a conta dentro da janela de retenção | `results/optimized/log_audit.json`, `cloudtrail_link.json` |
| H3: o laço converge e não oscila | `scripts/benchmark.py --aggregate-only`; cenários C9 e C10 nos `run-NN.json` | `results/optimized/verification_consistency.json`, `summary.md` |
| Diferenças entre séries | `scripts/compare_series.py` | `results/comparisons.json` |
| Propriedades do modelo | `python -m pytest tests -q` e `./scripts/test_policies.sh` | saída dos testes |

Uma medição nova não reproduz os mesmos números: a detecção depende da entrega
do CloudTrail, que varia de amostra para amostra. O que se espera repetir é a
ordem de grandeza e o resultado de cada cenário.

## Nível 1: sem conta AWS

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

Os testes que não dependem de conta rodam com:

```bash
python3.14 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q     # unitários, propriedade e contrato
./scripts/test_policies.sh              # políticas Rego e contrato de remediação (Docker)
```

## Nível 2: implantação do zero

### Pré-requisitos

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

### Passos

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

## Testes e medição

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
