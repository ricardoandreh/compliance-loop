# Requirements Document

## Introduction

O MVP Compliance Automation Loop é um sistema de automação de conformidade baseado no padrão MAPE-K (Monitor, Analyze, Plan, Execute, Knowledge) que detecta desvios de conformidade em recursos cloud, classifica o estado do recurso, e executa ações corretivas automaticamente ou escala para operadores humanos quando necessário.

O sistema monitora eventos de mutação em recursos AWS via CloudTrail, avalia conformidade usando políticas Rego executadas pelo Open Policy Agent (OPA), e remedia violações usando OpenTofu. O MVP implementa controle para bloqueio de acesso público em buckets S3.

> **Escopo construído.** O texto acima e os requisitos 1–19 são a especificação inicial, preservada. O sistema construído implementa seis controles em dois serviços (S3 e EC2), verifica a convergência de cada remediação numa segunda rodada, escala por cinco motivos (não só falha de remediação), remedia com uma autoridade restrita a cada incidente e tem uma camada de operação. O que mudou em relação aos requisitos 1–19 está em *Revisões dos requisitos*; o que foi acrescentado sem estar previsto está em *Requisitos acrescentados durante o desenvolvimento* (20–27).

## Revisões dos requisitos

Este documento é a especificação inicial do MVP e fica preservado como tal. O sistema construído a partir dele evoluiu, e alguns critérios foram alterados por decisão de projeto — cada um está marcado abaixo com *(revisado)*, *(parcialmente atendido)* ou *(não atendido)* e justificado no `design.md`. A situação de cada requisito e de cada propriedade de corretude, com a evidência correspondente, está em [`traceability.md`](traceability.md).

| Critério | Especificado | Construído | Justificativa |
|---|---|---|---|
| 1.1 | captura em até 5 s, só para `PutBucketPublicAccessBlock` | 7,2–8,1 s em média por série; 5 de 180 amostras (nove séries) abaixo de 5 s | latência de entrega do CloudTrail, fora do sistema — *não atendido* |
| 1.6, 10.1 | apenas `ctrl-s3-public-block` | seis controles em dois serviços | extensibilidade: controles cadastrados em tabela (*Arquitetura de Extensibilidade*) |
| 2.5 | encerrar após a remediação | agenda uma rodada de verificação antes de encerrar | fechamento explícito do laço (*Fechamento do laço em duas rodadas*) |
| 3.2 | política `s3_public_access` | uma política Rego por controle | idem 1.6 |
| 3.6, 5.3 | índice por `control_id` | índice `resource-control-index` | consulta pontual por recurso e controle, sem varredura |
| 4.2–4.4 | política no pacote da Lambda | política no S3, versionada por `policy_version` | mudar política sem novo deploy da Lambda |
| 5.2 | `exception_id` UUID | identificador de texto escolhido por quem registra; o sistema não gera nem valida o formato | não há fluxo de cadastro de exceções: o registro é uma escrita direta na tabela (o fluxo de aprovação está fora do escopo) |
| 6.1 | o Analyze grava a linha | o orquestrador grava; falha de avaliação não gera linha | Analyze sem efeitos colaterais; modelo de três estados |
| 8.1, 19.1 | escalar quando o Execute falha | escalar por cinco motivos: remediação falha, não convergência, reincidência (disjuntor), controle detectivo e falha do sistema | os requisitos 20–22 criaram caminhos de escalação sem Execute |
| 8.6 | timestamp da violação | instante em que a escalação foi decidida (a avaliação fica na linha do log, ligada pelo `violation_id`) | a mensagem descreve a escalação; a violação já tem registro próprio |
| 8.8 | URL da execução | ARN da execução (`step_functions_execution_arn`), que a busca do console abre | o ARN é estável e não depende da região do console |
| 9.4 | `/tmp/remediation.tfstate` | `/tmp/<control_id>/<recurso>.tfstate`, num diretório apagado a cada invocação | um estado que sobrevivesse de uma invocação anterior, com `-refresh=false`, faria o OpenTofu responder *No changes* a um recurso violado de novo e pular a remediação |
| 11.1 | detecção em até 10 s, com o bucket criado já público | 164 de 180 amostras (91%), pior caso 12,08 s; o cenário cria o bucket e em seguida remove o bloqueio | idem 1.1 — *parcialmente atendido*. Desde 2023 o S3 cria buckets com o bloqueio de acesso público ativo, então o cenário precisa removê-lo |
| 15.3, 15.7 | apenas `bucketName` | também volume e recurso de conta | controles de EC2 |
| 15.6 | descartar evento incompleto | identificador sintético da conta | o EventBridge só admite eventos de controles cadastrados (L3) |
| 16.2 | nenhum estado externo além do Knowledge_Base | também a configuração do recurso (3.1) e a política no S3 (4.2 revisado) | o critério contradizia o 3.1; o que vale é que a classificação depende só dessas entradas, lidas uma vez por avaliação |
| 17.1–17.3 | 15 / 45 / 60 s | 60 / 90 / 600 s | os valores originais não cobriam o pior caso (*Property 28*) |
| 17.4 | registrar o timeout e encerrar | timeout do Analyze ou do Execute é escalado; timeout da execução inteira dispara um alarme | encerrar em silêncio deixaria o recurso sem dono |
| 19.5 | issue nula quando não houve escalação, lida como "o Execute não rodou ou teve sucesso" (*Property 23*) | issue só em linha escalada: remediação falha ou `S_violation` escalada pelo `PlanAction` | controles detectivos, disjuntor e verificação escalam sem invocar o Execute |

## Glossary

- **Compliance_Loop**: O sistema completo de automação de conformidade baseado no padrão MAPE-K
- **Monitor**: Componente que detecta eventos de mutação em recursos cloud via CloudTrail e EventBridge
- **Analyze**: Componente Lambda que classifica o estado de conformidade do recurso usando OPA/Rego
- **Execute**: Componente Lambda que executa remediação automática usando OpenTofu
- **Knowledge_Base**: Banco de dados DynamoDB que armazena políticas, exceções aprovadas e log de violações *(revisado: a tabela `controls` guarda os metadados de cada controle e a chave da política; o texto Rego fica no S3 — ver 4.2)*
- **Orchestrator**: Step Functions Standard que coordena as transições entre componentes do loop
- **S_nominal**: Estado formal indicando que o recurso está conforme com a política vigente
- **S_exception**: Estado formal indicando que o recurso possui exceção aprovada e vigente
- **S_violation**: Estado formal indicando que o recurso está em violação sem exceção válida
- **Control**: Conjunto de política Rego, evento CloudTrail e recurso alvo que define uma regra de conformidade *(revisado: também a remediação em OpenTofu, a chamada de leitura da configuração — `fetch_config` — e o modo de remediação, automático ou manual)*
- **Exception**: Autorização temporária que permite desvio de conformidade para recurso específico
- **violations_log**: Registro persistente de todas as avaliações de conformidade no Knowledge_Base
- **Escalation_Queue**: Fila SQS FIFO que armazena violações não remediadas para notificação ao operador *(revisado: também falhas do sistema — ver 8.1)*
- **Remediation**: Ação automatizada que corrige configuração de recurso para estado conforme

Termos acrescentados com os requisitos 20–27:

- **Plan**: A etapa do MAPE-K que decide, para um recurso em S_violation, entre remediar e escalar. Não era um componente na especificação inicial (a decisão era implícita no Orchestrator); no sistema construído é o estado `PlanAction`
- **Detection_Round**: A execução disparada por um evento de mutação; pode remediar
- **Verification_Round**: A execução agendada por uma Detection_Round que remediou; relê os controles remediados e nunca remedia
- **Circuit_Breaker**: A regra do Plan que escala, sem remediar, um par (recurso, controle) já remediado repetidas vezes numa janela curta
- **Detective_Control**: Controle cuja correção é disruptiva demais para ser automática (remediação `manual`): o sistema classifica e escala, sem tocar no recurso
- **Remediation_Session**: Credencial temporária da role de remediação, emitida por violação e restrita, por session policy, ao recurso classificado e às ações do controle
- **Exception_Scheduler**: Agendamento do EventBridge Scheduler que reavalia o recurso quando a exceção expira

## Requirements

### Requirement 1: Detectar Eventos de Mutação em Recursos Monitorados

**User Story:** Como operador de segurança, quero que o sistema detecte automaticamente mudanças em recursos cloud, para que nenhuma alteração de configuração passe despercebida.

#### Acceptance Criteria

1. WHEN a PutBucketPublicAccessBlock event occurs in CloudTrail, THE Monitor SHALL capture the event within 5 seconds *(não atendido — ver Revisões)*
2. THE Monitor SHALL extract the bucket ARN from the event detail
3. THE Monitor SHALL extract the AWS account ID from the event detail
4. THE Monitor SHALL extract the AWS region from the event detail
5. THE Monitor SHALL trigger the Orchestrator with the extracted event metadata
6. THE Monitor SHALL process only events matching the control_id "ctrl-s3-public-block" *(revisado)*

### Requirement 2: Orquestrar Transições de Estado do Loop MAPE-K

**User Story:** Como arquiteto de sistema, quero que o loop MAPE-K seja orquestrado de forma determinística, para que cada evento siga o fluxo correto de análise e execução.

#### Acceptance Criteria

1. WHEN the Monitor triggers the Orchestrator, THE Orchestrator SHALL invoke the Analyze component with event metadata
2. WHEN Analyze returns S_nominal, THE Orchestrator SHALL terminate the workflow without invoking Execute
3. WHEN Analyze returns S_exception, THE Orchestrator SHALL terminate the workflow without invoking Execute
4. WHEN Analyze returns S_violation, THE Orchestrator SHALL invoke the Execute component
5. WHEN Execute succeeds, THE Orchestrator SHALL terminate the workflow *(revisado)*
6. IF Execute fails, THEN THE Orchestrator SHALL invoke the escalation mechanism
7. THE Orchestrator SHALL complete the entire loop within 60 seconds

### Requirement 3: Classificar Estado de Conformidade do Recurso

**User Story:** Como analista de conformidade, quero que o sistema classifique precisamente o estado de cada recurso, para que decisões de remediação sejam baseadas em avaliação correta.

#### Acceptance Criteria

1. WHEN Analyze receives event metadata, THE Analyze SHALL retrieve the current resource configuration from AWS
2. THE Analyze SHALL evaluate the resource configuration against the s3_public_access Rego policy using OPA *(revisado)*
3. WHEN the resource is compliant with the policy, THE Analyze SHALL return S_nominal
4. WHEN the resource is non-compliant and has a valid exception, THE Analyze SHALL return S_exception
5. WHEN the resource is non-compliant without valid exception, THE Analyze SHALL return S_violation
6. THE Analyze SHALL query the Knowledge_Base exceptions table via control_id index, filtering by resource_arn match and valid_until greater than current timestamp *(revisado)*
7. WHEN an exception exists with valid_until in the future, THE Analyze SHALL consider the exception valid
8. WHEN an exception exists with valid_until in the past, THE Analyze SHALL consider the exception expired
9. THE Analyze SHALL produce deterministic classification for identical inputs

### Requirement 4: Versionar Políticas Rego em Repositório Git

**User Story:** Como administrador de políticas, quero que políticas Rego sejam versionadas em Git, para que mudanças sejam auditáveis via histórico de commits e revisadas via pull requests.

#### Acceptance Criteria

1. THE Compliance_Loop SHALL version Rego policies in a Git repository
2. THE Analyze Lambda SHALL load Rego policies from the deployment package at runtime *(revisado)*
3. THE Rego policy file SHALL be included in the Lambda deployment artifact *(revisado)*
4. WHEN a policy changes, THE change SHALL require redeployment of the Analyze Lambda *(revisado)*
5. THE Knowledge_Base controls table SHALL reference the policy package name, not store policy text
6. THE Git repository SHALL serve as the single source of truth for policy definitions

### Requirement 5: Gerenciar Exceções Aprovadas com TTL Nativo

**User Story:** Como gestor de conformidade, quero registrar exceções temporárias para recursos específicos, para que desvios autorizados não disparem remediação automática.

#### Acceptance Criteria

1. THE Knowledge_Base SHALL store exceptions in the exceptions table
2. THE Knowledge_Base SHALL use exception_id (UUID) as partition key *(revisado)*
3. THE Knowledge_Base SHALL index exceptions by control_id via Global Secondary Index *(revisado)*
4. THE Knowledge_Base SHALL store resource_arn for each exception
5. THE Knowledge_Base SHALL store control_id for each exception
6. THE Knowledge_Base SHALL store valid_until as Unix timestamp
7. THE valid_until field SHALL be configured as TTL attribute in DynamoDB
8. THE Analyze SHALL filter exceptions by valid_until > now() in queries
9. THE TTL mechanism SHALL eventually delete expired items (within 48h), but behavior SHALL NOT depend on physical deletion
10. THE Knowledge_Base SHALL store justification text for each exception
11. THE Knowledge_Base SHALL store approver identity for each exception
12. THE Knowledge_Base SHALL store exception creation timestamp

### Requirement 6: Registrar Log de Violações

**User Story:** Como auditor, quero que todas as avaliações de conformidade sejam registradas permanentemente, para que eu possa rastrear histórico completo de violações e remediações.

#### Acceptance Criteria

1. THE Analyze SHALL create exactly one entry in the violations_log table for each evaluation *(revisado)*
2. THE violations_log entry SHALL use violation_id (UUID) as partition key
3. THE violations_log entry SHALL include resource_arn as indexed attribute
4. THE violations_log entry SHALL include control_id
5. THE violations_log entry SHALL include classification state (S_nominal, S_exception, or S_violation)
6. THE violations_log entry SHALL include evaluation timestamp in ISO 8601 format
7. THE violations_log entry SHALL include policy_version used for evaluation
8. WHEN classification is S_exception, THE violations_log entry SHALL include exception_id reference
9. WHEN Execute is invoked, THE violations_log entry SHALL include remediation_status field
10. THE remediation_status field SHALL contain value "success" when Execute succeeds
11. THE remediation_status field SHALL contain value "failed" when Execute fails

### Requirement 7: Remediar Recursos em Violação

**User Story:** Como engenheiro de segurança, quero que recursos em violação sejam corrigidos automaticamente, para que a superfície de ataque seja reduzida sem intervenção manual.

#### Acceptance Criteria

1. WHEN Execute receives S_violation from Orchestrator, THE Execute SHALL apply OpenTofu configuration to the resource
2. THE Execute SHALL use the resource_id from the Orchestrator to identify the target resource
3. THE Execute SHALL apply OpenTofu configuration with -refresh=false flag
4. THE OpenTofu configuration SHALL describe the compliant state for the resource
5. WHEN OpenTofu apply succeeds, THE Execute SHALL return success status to Orchestrator
6. IF OpenTofu apply fails, THEN THE Execute SHALL return failure status with error details
7. THE Execute SHALL operate idempotently on the same resource
8. THE Execute SHALL complete remediation within 45 seconds
9. THE Execute SHALL enforce BlockPublicAcls, IgnorePublicAcls, BlockPublicPolicy, and RestrictPublicBuckets for S3 buckets

### Requirement 8: Escalar Violações Não Remediadas

**User Story:** Como operador de segurança, quero ser notificado quando remediação automática falha, para que eu possa intervir manualmente em casos complexos.

#### Acceptance Criteria

1. WHEN Execute returns failure status, THE Orchestrator SHALL send a message to the Escalation_Queue *(revisado: ampliado)*
2. THE Escalation_Queue SHALL be a SQS FIFO queue
3. THE escalation message SHALL include resource_arn
4. THE escalation message SHALL include control_id
5. THE escalation message SHALL include error details from Execute
6. THE escalation message SHALL include violation timestamp *(revisado)*
7. THE escalation message SHALL include violation_id reference
8. THE escalation message SHALL include step_functions_execution_url for navigation to execution details *(revisado)*
9. WHEN a message is added to Escalation_Queue, THE Compliance_Loop SHALL create a GitHub Issue
10. THE GitHub Issue SHALL include all escalation message details
11. THE GitHub Issue SHALL be labeled with "compliance-violation" and "auto-escalation"
12. THE github_issue_id returned by GitHub API SHALL be saved to the violations_log entry

### Requirement 9: Garantir Idempotência na Remediação

**User Story:** Como desenvolvedor do sistema, quero que remediação seja idempotente, para que execuções repetidas não causem efeitos colaterais indesejados.

#### Acceptance Criteria

1. WHEN Execute is invoked multiple times for the same resource in S_violation, THE Execute SHALL produce the same final resource state
2. THE Execute SHALL use OpenTofu declarative configuration to ensure idempotency
3. WHEN the resource is already in compliant state, THE OpenTofu apply SHALL succeed without causing configuration drift
4. THE Execute SHALL use local ephemeral state backend (/tmp/remediation.tfstate) *(revisado)*

### Requirement 10: Implementar Controle S3 Public Block

**User Story:** Como especialista em segurança S3, quero que o MVP implemente controle de bloqueio de acesso público, para que buckets não sejam expostos inadvertidamente.

#### Acceptance Criteria

1. THE Compliance_Loop SHALL implement control_id "ctrl-s3-public-block" *(revisado: ampliado)*
2. THE s3_public_access policy SHALL evaluate BlockPublicAcls setting
3. THE s3_public_access policy SHALL evaluate IgnorePublicAcls setting
4. THE s3_public_access policy SHALL evaluate BlockPublicPolicy setting
5. THE s3_public_access policy SHALL evaluate RestrictPublicBuckets setting
6. WHEN any of the four settings is false, THE s3_public_access policy SHALL return violation
7. WHEN all four settings are true, THE s3_public_access policy SHALL return compliant

### Requirement 11: Validar Cenário de Remediação Automática

**User Story:** Como testador do sistema, quero validar que remediação automática funciona end-to-end, para que eu possa confiar no comportamento do MVP.

#### Acceptance Criteria

1. WHEN a bucket is created with public access enabled, THE Compliance_Loop SHALL detect the violation within 10 seconds *(parcialmente atendido — ver Revisões)*
2. THE Analyze SHALL classify the resource as S_violation
3. THE Execute SHALL remediate the bucket to compliant state
4. THE violations_log SHALL contain entry with remediation_status "success"
5. WHEN the bucket configuration is queried after remediation, THE bucket SHALL have all four public access blocks enabled

### Requirement 12: Validar Cenário de Exceção Válida

**User Story:** Como testador do sistema, quero validar que exceções aprovadas impedem remediação, para que recursos autorizados não sejam modificados incorretamente.

#### Acceptance Criteria

1. WHEN a bucket has public access enabled and a valid exception exists, THE Analyze SHALL classify the resource as S_exception
2. THE Orchestrator SHALL terminate without invoking Execute
3. THE violations_log SHALL contain entry with classification S_exception
4. THE violations_log entry SHALL reference the exception_id
5. THE bucket configuration SHALL remain unchanged

### Requirement 13: Validar Cenário de Exceção Expirada

**User Story:** Como testador do sistema, quero validar que exceções expiradas não impedem remediação, para que proteções temporárias não se tornem permanentes.

#### Acceptance Criteria

1. WHEN a bucket has public access enabled and an expired exception exists, THE Analyze SHALL classify the resource as S_violation
2. THE Analyze SHALL ignore the expired exception during classification
3. THE Execute SHALL remediate the bucket to compliant state
4. THE violations_log SHALL contain entry with classification S_violation and remediation_status "success"

### Requirement 14: Validar Cenário de Falha na Remediação

**User Story:** Como testador do sistema, quero validar que falhas de remediação são escaladas corretamente, para que operadores sejam notificados de problemas.

#### Acceptance Criteria

1. WHEN Execute fails to remediate a resource, THE Orchestrator SHALL send message to Escalation_Queue
2. THE Escalation_Queue SHALL contain message with error details
3. THE Compliance_Loop SHALL create GitHub Issue with violation details
4. THE violations_log SHALL contain entry with state S_violation and remediation_status "failed"
5. THE bucket configuration SHALL remain in non-compliant state

### Requirement 15: Implementar Parser de Eventos CloudTrail

**User Story:** Como desenvolvedor, quero parsear eventos CloudTrail de forma confiável, para que metadados de recursos sejam extraídos corretamente.

#### Acceptance Criteria

1. WHEN Monitor receives a CloudTrail event, THE Monitor SHALL parse the event JSON structure
2. THE Monitor SHALL extract detail.eventName field
3. THE Monitor SHALL extract detail.requestParameters.bucketName field *(revisado: ampliado)*
4. THE Monitor SHALL extract detail.userIdentity.accountId field
5. THE Monitor SHALL extract detail.awsRegion field
6. IF any required field is missing, THEN THE Monitor SHALL log error and discard event *(revisado)*
7. THE Monitor SHALL construct resource ARN in format "arn:aws:s3:::bucket-name" *(revisado: ampliado)*

### Requirement 16: Garantir Determinismo na Avaliação de Políticas

**User Story:** Como desenvolvedor de políticas, quero que avaliação de políticas seja determinística, para que o mesmo recurso sempre produza a mesma classificação.

#### Acceptance Criteria

1. WHEN Analyze evaluates the same resource configuration multiple times, THE Analyze SHALL return the same classification
2. THE Analyze SHALL not depend on external state beyond Knowledge_Base *(revisado)*
3. THE Analyze SHALL use consistent timestamp for exception expiration checks within single evaluation
4. THE OPA evaluation SHALL not use non-deterministic built-in functions

### Requirement 17: Implementar Timeout para Componentes

**User Story:** Como engenheiro de confiabilidade, quero que componentes tenham timeout configurado, para que operações lentas não bloqueiem o loop indefinidamente.

#### Acceptance Criteria

1. THE Analyze SHALL timeout after 15 seconds *(revisado)*
2. THE Execute SHALL timeout after 45 seconds *(revisado)*
3. THE Orchestrator SHALL timeout the entire workflow after 60 seconds *(revisado)*
4. WHEN a component times out, THE Orchestrator SHALL log timeout error and terminate workflow *(revisado)*

### Requirement 18: Persistir Estado S_violation Após Falha de Remediação

**User Story:** Como auditor, quero que o estado de violação persista quando a remediação falha, para que o registro reflita a realidade do recurso e não do processo.

#### Acceptance Criteria

1. WHEN Execute fails to remediate a resource, THE violations_log entry SHALL maintain state as S_violation
2. THE violations_log entry SHALL NOT create a new state S_remediation_failed
3. THE remediation_status field SHALL indicate the failure, not the state field
4. THE state field SHALL represent the actual compliance state of the resource
5. WHEN Execute fails, THE violations_log SHALL contain exactly one entry with state S_violation and remediation_status "failed"

### Requirement 19: Rastrear Escalonamento via GitHub Issue ID

**User Story:** Como operador, quero que violações escaladas sejam rastreáveis até a issue de resolução, para que eu possa auditar o ciclo completo de detecção até fechamento.

#### Acceptance Criteria

1. WHEN a GitHub Issue is created for a failed remediation, THE Compliance_Loop SHALL capture the issue number returned by GitHub API *(revisado: ampliado)*
2. THE github_issue_id SHALL be saved to the violations_log entry corresponding to the violation
3. THE violations_log entry SHALL link the violation event to the operational resolution workflow
4. WHEN querying violations_log by violation_id, THE result SHALL include github_issue_id if escalation occurred
5. THE github_issue_id field SHALL be null for violations that did not require escalation *(revisado)*

## Requisitos acrescentados durante o desenvolvimento

Os requisitos abaixo não estavam na especificação inicial. Cada um nasceu de um problema encontrado ao construir ou medir o sistema, e está registrado com a mesma forma dos anteriores (história de usuário e critérios EARS) para que a matriz de rastreabilidade os cubra do mesmo modo. A motivação de cada um está na seção do `design.md` indicada.

### Requirement 20: Verificar a Convergência da Remediação

**User Story:** Como auditor, quero que cada remediação seja confirmada por uma nova leitura do recurso, para que o registro diga que o recurso convergiu, e não apenas que o comando de remediação terminou sem erro.

*Motivação: a primeira versão confirmava a convergência pelo eco da própria remediação no CloudTrail, um evento que o sistema provoca (design.md, Fechamento do laço em duas rodadas).*

#### Acceptance Criteria

1. WHEN a Detection_Round remediates at least one control successfully, THE Orchestrator SHALL schedule exactly one Verification_Round for that resource
2. THE Verification_Round SHALL re-evaluate only the controls that were remediated, reading the resource configuration without a fixed wait
3. THE Verification_Round SHALL NOT invoke Execute and SHALL NOT schedule another Verification_Round
4. WHEN the Verification_Round classifies a remediated control as S_violation, THE Orchestrator SHALL escalate it with reason "non_convergence"
5. WHEN the Verification_Round cannot be scheduled or cannot evaluate the remediated controls, THE Orchestrator SHALL escalate the incident
6. THE violations_log entry of a Verification_Round SHALL reference the origin execution and the violation_id it verifies
7. THE Orchestrator SHALL run at most two executions per incident, whatever the number of remediated controls

### Requirement 21: Interromper Remediações Reincidentes

**User Story:** Como engenheiro de confiabilidade, quero que o sistema pare de remediar um recurso que algo continua revertendo, para que dois automatismos não disputem o mesmo recurso indefinidamente.

*Motivação: sem limite, uma remediação revertida por outro processo (ou recusada pela própria política) se repete a cada evento (design.md, L11).*

#### Acceptance Criteria

1. WHEN a Detection_Round classifies a control with automatic remediation as S_violation, THE Plan SHALL count the successful remediations of the same (resource, control) pair within a configurable window (default 10 minutes) before invoking Execute
2. WHEN the count reaches a configurable threshold (default 3), THE Plan SHALL escalate with reason "recurrent_violation" instead of invoking Execute
3. IF the count cannot be read, THEN THE Plan SHALL proceed as if no recent remediation existed and the decision SHALL remain recorded in the execution history

### Requirement 22: Suportar Controles Detectivos

**User Story:** Como engenheiro de segurança, quero registrar controles que apenas detectam e escalam, para que violações cuja correção é disruptiva (como criptografar um volume em uso) sejam tratadas por uma pessoa.

#### Acceptance Criteria

1. THE controls table SHALL allow a control to declare its remediation as "automatic" or "manual"
2. WHEN a control with manual remediation is classified as S_violation, THE Plan SHALL escalate with reason "manual_remediation_required" and SHALL NOT invoke Execute
3. THE resource under a Detective_Control SHALL remain unchanged by the system

### Requirement 23: Limitar a Autoridade do Sistema ao Mínimo Necessário

**User Story:** Como responsável pela segurança da conta, quero que o sistema de conformidade só consiga ler e alterar o que cada incidente exige, para que ele próprio não se torne o maior risco da conta.

*Motivação: um controle é um dado numa tabela; sem limite, um controle adulterado dirigiria a remediação para qualquer recurso (design.md, Autoridade por incidente; Modelo de Permissões).*

#### Acceptance Criteria

1. THE Execute SHALL remediate using a Remediation_Session issued per violation, whose session name is the violation_id
2. THE Remediation_Session SHALL be restricted to the classified resource ARN and to the actions of the control being remediated
3. IF a control requests an action outside the remediation allowlist, THEN THE Execute SHALL refuse it without running OpenTofu
4. THE remediation allowlist SHALL have a single definition, shared by the remediation role and by the Execute check
5. THE Analyze role SHALL grant exactly the read actions that the registered controls use, and no other
6. WHEN a remediation template targets a resource other than the classified one, THE write SHALL be denied by IAM

### Requirement 24: Reavaliar o Recurso na Expiração da Exceção

**User Story:** Como gestor de conformidade, quero que o recurso seja reavaliado quando a exceção expira, para que a proteção volte sem depender de um novo evento no recurso.

*Motivação: 3.8 só garante que uma exceção expirada é ignorada na próxima avaliação, que pode nunca acontecer; o TTL do DynamoDB leva até 48 h e não dispara avaliação (design.md, reavaliação por Scheduler).*

#### Acceptance Criteria

1. WHEN an exception is created or modified, THE Compliance_Loop SHALL create an Exception_Scheduler for valid_until plus 30 seconds
2. WHEN the Exception_Scheduler fires, THE Orchestrator SHALL evaluate the resource for the exception's control
3. WHEN an exception is deleted, THE Compliance_Loop SHALL delete its Exception_Scheduler
4. THE Exception_Scheduler SHALL NOT depend on the physical deletion of the exception by TTL

### Requirement 25: Atualizar os Controles sem Novo Deploy

**User Story:** Como administrador de políticas, quero publicar ou remover um controle sem reimplantar funções, para que a cobertura de conformidade evolua na velocidade das políticas.

#### Acceptance Criteria

1. WHEN a control is inserted, modified or removed in the controls table, THE Compliance_Loop SHALL regenerate the EventBridge rule from the union of the registered controls' events
2. THE Analyze SHALL read the resource configuration through the call declared by the control (fetch_config), restricted to read operations
3. WHEN a new control uses only read and remediation actions already granted, THE Compliance_Loop SHALL start evaluating it without a Lambda code deployment

### Requirement 26: Aceitar Apenas Eventos Legítimos

**User Story:** Como responsável pela segurança da conta, quero que o Monitor só aceite eventos emitidos pela AWS, para que ninguém dispare o laço com um evento forjado nem o sistema reaja às próprias remediações.

#### Acceptance Criteria

1. THE Monitor SHALL match only events whose source is emitted by AWS (prefix "aws.")
2. THE Monitor SHALL NOT match events whose identity is the remediation role, so that a remediation does not trigger a new Detection_Round

### Requirement 27: Tornar Visíveis as Falhas Silenciosas

**User Story:** Como operador, quero ser avisado das falhas que não geram escalação, para que o laço parado não pareça o laço funcionando.

*Motivação: uma detecção que o EventBridge não entrega, um agendamento que falha ou uma Lambda que cai não produzem linha nem issue (design.md, Operação: alarmes e runbook).*

#### Acceptance Criteria

1. THE Compliance_Loop SHALL raise an alarm for each failure mode that produces no escalation: undelivered detection, failed exception scheduling, Lambda crash or timeout, execution timeout, throttling and messages in dead-letter queues
2. WHEN an alarm enters ALARM, THE Compliance_Loop SHALL open a GitHub issue labeled "operations-alarm", or comment on the open issue of the same alarm
3. WHEN an alarm returns to OK, THE Compliance_Loop SHALL comment on its open issue without closing it
4. IF the Monitor cannot start the Orchestrator, THEN THE Monitor SHALL keep the detection in a dead-letter queue from which it can be replayed
5. THE Knowledge_Base SHALL keep point-in-time recovery on the violations_log and exceptions tables, and THE Compliance_Loop SHALL set a retention period on the functions' logs
