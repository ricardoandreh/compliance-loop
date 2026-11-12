# Arquitetura

Este documento descreve como o laço funciona, onde cada parte está no código e
o que o sistema nunca faz. O roteiro de implantação está em `REPRODUCING.md`, os
resultados medidos em `results/README.md`, e o documento de projeto completo,
com requisitos, propriedades e limitações, em
`.kiro/specs/mvp-compliance-automation-loop/design.md`.

## Visão geral

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

## Componentes

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

## Invariantes

O que o laço garante em qualquer execução, e onde a garantia está no código. Os
nomes entre parênteses são os testes que a exercitam.

1. **Só eventos emitidos pela AWS iniciam o laço.** A regra do EventBridge
   exige `source` com prefixo `aws.`, que o `PutEvents` recusa a outros
   emissores. A regra é gerada pelo `controls_sync` a partir da tabela de
   controles (`tests/contract/test_event_pattern.py`).
2. **A escrita de uma remediação não abre outro incidente.** A mesma regra
   descarta os eventos emitidos pelo papel de remediação.
3. **A análise não grava nada.** A função Analyze lê o recurso e as exceções e
   devolve a classificação. Quem escreve na trilha de auditoria é o
   orquestrador, e só ele.
4. **Falha de avaliação não é estado.** Se a leitura, o OPA ou a consulta de
   exceções falham, nenhum estado é atribuído e o caso é escalado sem ação
   sobre o recurso.
5. **Recurso sob exceção vigente não é remediado.** A exceção é relida na
   tabela antes de valer, e uma exceção revogada ou vencida deixa de proteger
   o recurso na avaliação seguinte.
6. **Um incidente tem no máximo duas execuções.** A rodada de verificação
   relê o recurso e nunca remedeia: confirma `S_nominal` ou escala
   (`tests/unit/test_state_machine.py`).
7. **A reincidência interrompe a remediação.** Com três remediações do mesmo
   par recurso e controle na janela do disjuntor, a violação seguinte é
   escalada em vez de corrigida.
8. **A autoridade de escrita é por incidente.** A função Execute não tem
   permissão de escrita própria. Cada remediação assume o papel de remediação
   com uma política de sessão limitada ao recurso classificado e às ações que
   o controle declarou, com o `violation_id` como nome da sessão.
9. **O laço não reescreve a própria evidência.** O papel de remediação tem
   `Deny` sobre o bucket do CloudTrail e o do estado.
10. **Um controle é dado e não amplia permissões.** A leitura que um controle
    declara só pode ser `get_`, `describe_` ou `list_`, e as ações de leitura e
    de remediação concedidas ficam em listas da infraestrutura
    (`tests/contract/test_analyze_permissions.py`).
11. **Exceções só entram pelo canal de aprovação.** A política de recurso da
    tabela de exceções nega a escrita a qualquer identidade que não seja o
    papel `exception-writer` (`tests/contract/test_exception_writer.py`).

## Mapa do código

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

Os controles, sua extensão e as permissões que cada um exige estão em
`docs/controles.md`. Alarmes, reprocessamento e operação estão em
`docs/operacao.md`.
