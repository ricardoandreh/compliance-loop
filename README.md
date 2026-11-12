# compliance-loop

Laço MAPE-K orientado a eventos que detecta, remedia e verifica desvios de conformidade em recursos da AWS.

Quando um recurso sai de conformidade, o evento do CloudTrail dispara uma
avaliação por política (OPA/Rego). O recurso é classificado em um de três
estados: conforme, sob exceção autorizada ou em violação. A violação é corrigida
com OpenTofu, por uma sessão cuja permissão se limita àquele recurso, e uma
segunda rodada relê o recurso para confirmar a correção. O que o laço não
resolve vira uma issue.

Este repositório é o artefato da monografia *Conformidade em Tempo de Execução
com Governança de Exceções: um laço de realimentação MAPE-K orientado a
eventos* (Instituto Federal Catarinense, 2026). Ele traz o código, a
infraestrutura e os dados das dez séries de medição.

## Sumário

- [Como funciona](#como-funciona)
- [Controles](#controles)
- [Pré-requisitos](#pré-requisitos)
- [Uso](#uso)
- [Resultados](#resultados)
- [Documentação](#documentação)
- [Como citar](#como-citar)
- [Contribuição](#contribuição)
- [Licença](#licença)

## Como funciona

```
mudança no recurso ─► CloudTrail ─► EventBridge ─► Step Functions
                                                      │
        ┌─────────────────────────────────────────────┤
        ▼                                             ▼
   Analyze (OPA/Rego + exceções)                 Knowledge (DynamoDB)
        │                                    controles · exceções · trilha
        ├─ conforme ............ registra e encerra
        ├─ sob exceção ......... registra; reavalia no vencimento
        └─ violação ─► Plan ─► Execute (OpenTofu, sessão por incidente)
                         │            │
                         │            └─► Verificação: relê o recurso
                         └─► escala (issue) na reincidência, na falha
                             ou se o controle for detectivo
```

Um incidente tem no máximo duas execuções: a que remedeia e a que verifica. A
verificação nunca remedeia, e a escrita da própria remediação não dispara o
laço de novo. O desenho completo, com os invariantes e o mapa do código, está
em [`ARCHITECTURE.md`](ARCHITECTURE.md).

## Controles

| Controle | Serviço | Política |
|---|---|---|
| `ctrl-s3-public-block` | S3 | Bloqueio de acesso público ligado nas quatro opções |
| `ctrl-s3-ssl-only` | S3 | Política do bucket nega requisições sem SSL |
| `ctrl-s3-versioning` | S3 | Versionamento habilitado |
| `ctrl-s3-encryption` | S3 | Criptografia no servidor configurada |
| `ctrl-ebs-encryption-default` | EC2 | Criptografia de EBS por padrão |
| `ctrl-ebs-volume-encrypted` | EC2 | Volume criptografado (detectivo: escala, não remedeia) |

Um controle é dado: três arquivos em `controls/`, publicados sem reimplantar
nenhuma função. Veja [`docs/controles.md`](docs/controles.md).

## Pré-requisitos

- Para os testes e as análises: Python 3.14 e Docker.
- Para implantar: uma conta AWS de teste com permissão de administrador, AWS
  CLI v2, Docker, `jq`, `zip`, `unzip`, `curl` e um repositório no GitHub para
  as issues de escalação.

A lista completa e as versões fixadas estão em [`REPRODUCING.md`](REPRODUCING.md).

## Uso

Sem conta AWS, os testes e a reagregação das séries publicadas:

```bash
python3.14 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
./scripts/test_policies.sh
.venv/bin/python scripts/benchmark.py --aggregate-only --out results/optimized
```

Com conta AWS, a implantação do zero e a suíte de integração:

```bash
bash scripts/bootstrap.sh
make init apply controls sync-rule
./scripts/setup_ssm_github.sh <token> <owner> <repo>
make integration
```

A implantação cria recursos faturáveis. Leia [`REPRODUCING.md`](REPRODUCING.md)
antes: ele explica cada passo, o custo, a segunda cópia em outra região e a
desmontagem.

## Resultados

Na série somativa (`results/optimized/`, etiqueta `v1.0.0`), em dez execuções
da suíte contra a conta real:

- os 16 cenários passaram nas dez execuções (160 de 160);
- a exposição de uma violação foi de 13,87 s ± 1,71, do evento ao fim da
  remediação;
- as 430 remediações da série foram ligadas ao seu evento no CloudTrail.

A décima série (`results/revocation/`, etiqueta `v1.1.0`) acrescenta o canal
único de exceções e a revogação. Os dados brutos, o catálogo das séries e a
análise estão em [`results/README.md`](results/README.md).

## Documentação

| Documento | Conteúdo |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Como o laço funciona, componentes, invariantes e mapa do código |
| [`REPRODUCING.md`](REPRODUCING.md) | Como conferir os números e como implantar do zero |
| [`results/README.md`](results/README.md) | As dez séries, os arquivos de cada uma e os resultados |
| [`docs/controles.md`](docs/controles.md) | Os controles e como acrescentar um |
| [`docs/operacao.md`](docs/operacao.md) | Alarmes, reprocessamento e falhas provocadas |
| [`design.md`](.kiro/specs/mvp-compliance-automation-loop/design.md) | Documento de projeto: requisitos, propriedades e limitações conhecidas |
| [`traceability.md`](.kiro/specs/mvp-compliance-automation-loop/traceability.md) | Cada requisito e propriedade ligado à evidência que o sustenta |
| [`CHANGELOG.md`](CHANGELOG.md) | O que mudou entre as versões etiquetadas |

## Como citar

Cite a monografia. Os dados da citação estão em [`CITATION.cff`](CITATION.cff),
que o GitHub mostra no botão *Cite this repository*.

## Contribuição

Este é um artefato de pesquisa mantido por uma pessoa. Dúvidas e relatos de
erro são bem-vindos pelas issues do repositório. Mudanças no código passam
pelos testes offline, que a integração contínua roda a cada envio.

## Licença

[MIT](LICENSE) © 2026 Ricardo André da Silva Lima
