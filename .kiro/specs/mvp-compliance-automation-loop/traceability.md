# Matriz de rastreabilidade

Liga cada requisito (`requirements.md`) e cada propriedade de corretude (`design.md`, *Correctness Properties*) à evidência que a sustenta, e registra onde o sistema construído diverge do especificado.

**Status**

- **Atendido** — a evidência cobre todos os critérios de aceitação.
- **Revisado** — o critério foi alterado por decisão de projeto registrada; o que vale é o comportamento revisado, e a coluna *Observação* aponta onde a decisão está justificada.
- **Parcial** — parte dos critérios não é atendida, com o motivo medido.
- **Não atendido** — o critério não é atendido.

**Tipos de evidência**: *unitário* e *propriedade* (`tests/unit`, `tests/property`, offline); *Rego* (`controls/*/policy_test.rego`, OPA); *contrato* (`tests/contract`: estado remediado × política, e padrões e estados avaliados pela própria AWS); *estrutural* (`tests/unit/test_state_machine.py`, propriedades do grafo); *integração* (cenários de `scripts/integration_test.py`, 10 execuções por série); *auditoria* (`scripts/audit_log_completeness.py`, sobre todas as linhas gravadas na série final); *injeção de falha* (`scripts/ops_fault_injection.py`, falhas silenciosas provocadas na conta real); *medição* (séries em `results/`).

## Requisitos

| Req | Tema | Status | Evidência | Observação |
|---|---|---|---|---|
| R1 | Detectar eventos de mutação | **Parcial** | integração C1 · C5 (`t_detection`, 120 amostras); contrato `test_event_pattern`; contrato `test_resolve_resource`; injeção de falha `intake` | **1.1 (captura em até 5 s) não é atendido**: 4 de 120 amostras (3%); média 7,2–8,1 s por série, pior caso 11,71 s. A latência é da entrega do CloudTrail ao EventBridge, fora do sistema (*Síntese por Hipótese*, H3). **1.5 reforçado**: um disparo que o EventBridge não consegue entregar vai para a fila `intake-failures` em vez de sumir, e é reprocessado (injeção `intake`: na fila em 17,9 s, reprocessado com SUCCEEDED, alarme convertido em issue no GitHub). **1.6 revisado**: o Monitor processa os eventos de todos os controles cadastrados — o padrão é gerado pelo `controls-sync` a partir da tabela — e só eventos emitidos pela AWS casam com ele (P25) |
| R2 | Orquestrar transições do laço | **Revisado** | estrutural (rotas por estado, `PlanAction`); integração C1–C10 | **2.5 revisado**: depois de remediar, a execução agenda uma rodada de verificação antes de encerrar (*Fechamento do laço em duas rodadas*). **2.7 atendido com folga**: do evento à convergência confirmada, pior caso 19,23 s, contra 60 s exigidos |
| R3 | Classificar o estado | **Revisado** | propriedade P3–P6, P8; unitário do Analyze; integração C1, C2, C6 | **3.2 generalizado**: cada controle tem sua política Rego. **3.6 revisado**: consulta pelo índice `resource-control-index` (ponto por recurso e controle), não por `control_id` com filtro por recurso |
| R4 | Versionar políticas em Git | **Revisado** | `controls/` versionado; unitário (cache por `policy_version`); integração C7 | **4.2–4.4 revisados**: as políticas são lidas do S3 em tempo de execução, versionadas por `policy_version`; mudar uma política exige `deploy_control.sh`, não um novo deploy da Lambda (*Arquitetura de Extensibilidade*). 4.1, 4.5 e 4.6 atendidos |
| R5 | Exceções com TTL | **Revisado** | integração C2, C3; unitário `exception_scheduler_sync` (12 casos); propriedade P8 | **5.3 revisado**: índice `resource-control-index` (ver R3). A reavaliação na expiração é feita pelo EventBridge Scheduler; o TTL só limpa a tabela, como 5.9 já previa |
| R6 | Registrar o log de violações | **Revisado** | auditoria de completude; unitário P11; integração C1–C10 | **6.1 revisado**: quem grava é o orquestrador (o Analyze é função pura), e uma avaliação que falha não gera linha de estado — é falha de sistema, fora do modelo de três estados (*State Classification Model*) |
| R7 | Remediar recursos em violação | **Atendido** | propriedade P15–P18; contrato (estado remediado × política); integração CTRL-01…05, C10 | **7.8 (até 45 s)**: o Execute leva ~5,5 s quente e ~10,6 s frio. **7.2** reforçado: o recurso-alvo e o escopo da sessão vêm do mesmo ARN classificado (*Autoridade por incidente*) |
| R8 | Escalar não remediadas | **Atendido** | integração C4, C8, C9, CTRL-06; unitário da escalação; estrutural (motivos) | **8.8**: a mensagem traz o ARN da execução, que o console abre diretamente. **8.11**: o rótulo `auto-escalation` faltava e foi acrescentado |
| R9 | Idempotência | **Atendido** | propriedade P15; contrato | Estado local efêmero recriado a cada invocação, com `-refresh=false` |
| R10 | Controle S3 public block | **Atendido** | Rego (5 testes); contrato; integração CTRL-01 | Ampliado para seis controles em dois serviços |
| R11 | Cenário de remediação automática | **Parcial** | integração C1 (10/10) | **11.1 (detecção em até 10 s)**: 109 de 120 amostras (91%), pior caso 11,71 s — mesma causa de 1.1. Demais critérios atendidos |
| R12 | Cenário de exceção válida | **Atendido** | integração C2 (10/10) | Bucket inalterado e `exception_id` na linha |
| R13 | Cenário de exceção expirada | **Atendido** | integração C3 (10/10) | Reavaliação disparada pelo Scheduler |
| R14 | Cenário de falha na remediação | **Atendido** | integração C8 (10/10) | A especificação original do C4 é o C8 atual; o C4 cobre falha do sistema |
| R15 | Parser de eventos CloudTrail | **Revisado** | contrato `test_resolve_resource` (5 formas de entrada) | **15.3 e 15.7 generalizados**: volume (`responseElements.volumeId`) e recurso de conta. **15.6 revisado**: sem campo de recurso, o evento recebe o identificador sintético da conta (L3) em vez de ser descartado — o EventBridge só admite eventos de controles cadastrados, que sempre trazem o recurso |
| R16 | Determinismo | **Atendido** | propriedade P3 | Um único `canonical_timestamp` por invocação |
| R17 | Timeouts | **Revisado** | estrutural; alarme `executions-timed-out` | **17.1–17.3 revisados**: 15/45/60 s não cobriam o pior caso; valem 60/90/600 s (Analyze, Execute, execução), justificados pela cadeia de timeouts da *Property 28* (pior caso serial 550 s) |
| R18 | S_violation após falha | **Atendido** | integração C8 (10/10) | `remediation_status = failed`, estado preservado |
| R19 | Rastrear escalação | **Atendido** | integração C8; auditoria de completude | **19.5** verificado sobre as 1.160 linhas da série final |

**Resumo**: 10 atendidos, 7 revisados com justificativa, 2 parciais (ambos pela latência do CloudTrail), nenhum não atendido.

## Propriedades de corretude

| P | Propriedade | Evidência | Status |
|---|---|---|---|
| 1 | Leitura completa do evento CloudTrail | contrato `test_resolve_resource` (bucket, volume, conta, expiração, verificação) | Atendido |
| 2 | Roteamento por estado | estrutural (três estados formais, `Default` em todo `Choice`); integração | Atendido |
| 3 | Determinismo da classificação | propriedade `test_analyze_properties` | Atendido |
| 4 | Recurso conforme → `S_nominal` | propriedade | Atendido |
| 5 | Exceção válida → `S_exception` | propriedade | Atendido |
| 6 | Violação → `S_violation` | propriedade | Atendido |
| 7 | Avaliação da política de acesso público | Rego (5 testes); contrato | Atendido |
| 8 | Consulta de exceções | propriedade; unitário (sem `Limit` antes do filtro) | Atendido |
| 9 | Completude do modelo de exceção | integração C2, C3 (gravação com todos os campos) | Parcial — sem verificação automatizada de todos os campos |
| 10 | Filtro independente do TTL | unitário (`valid_until > :now`) | Atendido |
| 11 | Unicidade da linha do log | unitário (9 avaliações, 9 identificadores) | Atendido |
| 12 | Completude do modelo do log | auditoria de completude: 1.160 linhas da série final, todas completas | Atendido |
| 13 | Referência à exceção no log | integração C2; auditoria | Atendido |
| 14 | Registro do status da remediação | integração C1, C8; auditoria | Atendido |
| 15 | Idempotência do Execute | propriedade `test_execute_properties` | Atendido |
| 16 | Invocação do OpenTofu | propriedade; unitário (credenciais escopadas no `tofu`) | Atendido |
| 17 | Resposta de sucesso do Execute | propriedade | Atendido |
| 18 | Resposta de falha do Execute | propriedade; unitário (sessão recusada não roda o `tofu`) | Atendido |
| 19 | Remediação produz estado conforme | contrato (todos os controles com remediação); integração CTRL-01…05 | Atendido |
| 20 | Escalação na falha | integração C4, C8; estrutural (motivos da escalação) | Atendido |
| 21 | Criação da issue | integração C8; unitário (deduplicação pelo log) | Atendido |
| 22 | Persistência do número da issue | integração C8 | Atendido |
| 23 | Sem issue para violação não escalada | auditoria de completude: 30 linhas com issue, todas escaladas; nenhuma em linha conforme, excetuada ou remediada | Atendido |
| 24 | Estado preservado após falha | integração C8 | Atendido |
| 25 | Filtro de eventos do EventBridge | contrato `test_event_pattern` (quatro identidades e uma origem forjada, que só deixou de casar com o prefixo `aws.` no `source`) | Atendido |
| 26 | Tratamento de evento incompleto no Monitor | contrato `test_resolve_resource` (caso de conta) | Revisado — ver R15.6 |
| 27 | Formato do ARN | contrato `test_resolve_resource` | Atendido |
| 28 | Tratamento de timeout | estrutural; alarme `executions-timed-out` | Atendido |
| 29 | Recurso inalterado em `S_exception` | integração C2 | Atendido |
| 30 | Recurso inalterado após falha | integração C8 | Atendido |

**Resumo**: 28 atendidas, 1 revisada, 1 parcial (P9).
