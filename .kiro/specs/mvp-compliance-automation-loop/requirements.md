# Requirements Document

## Introduction

O MVP Compliance Automation Loop é um sistema de automação de conformidade baseado no padrão MAPE-K (Monitor, Analyze, Plan, Execute, Knowledge) que detecta desvios de conformidade em recursos cloud, classifica o estado do recurso, e executa ações corretivas automaticamente ou escala para operadores humanos quando necessário.

O sistema monitora eventos de mutação em recursos AWS via CloudTrail, avalia conformidade usando políticas Rego executadas pelo Open Policy Agent (OPA), e remedia violações usando OpenTofu. O MVP implementa controle para bloqueio de acesso público em buckets S3.

## Revisões dos requisitos

Este documento é a especificação inicial do MVP e fica preservado como tal. O sistema construído a partir dele evoluiu, e alguns critérios foram alterados por decisão de projeto — cada um está marcado abaixo com *(revisado)* ou *(não atendido)* e justificado no `design.md`. A situação de cada requisito e de cada propriedade de corretude, com a evidência correspondente, está em [`traceability.md`](traceability.md).

| Critério | Especificado | Construído | Justificativa |
|---|---|---|---|
| 1.1 | captura em até 5 s | 7,2–8,1 s em média; 4 de 120 amostras abaixo de 5 s | latência de entrega do CloudTrail, fora do sistema — *não atendido* |
| 1.6, 10.1 | apenas `ctrl-s3-public-block` | seis controles em dois serviços | extensibilidade: controles cadastrados em tabela (*Arquitetura de Extensibilidade*) |
| 2.5 | encerrar após a remediação | agenda uma rodada de verificação antes de encerrar | fechamento explícito do laço (*Fechamento do laço em duas rodadas*) |
| 3.2 | política `s3_public_access` | uma política Rego por controle | idem 1.6 |
| 3.6, 5.3 | índice por `control_id` | índice `resource-control-index` | consulta pontual por recurso e controle, sem varredura |
| 4.2–4.4 | política no pacote da Lambda | política no S3, versionada por `policy_version` | mudar política sem novo deploy da Lambda |
| 6.1 | o Analyze grava a linha | o orquestrador grava; falha de avaliação não gera linha | Analyze sem efeitos colaterais; modelo de três estados |
| 11.1 | detecção em até 10 s | 109 de 120 amostras (91%) | idem 1.1 — *parcialmente atendido* |
| 15.3, 15.7 | apenas `bucketName` | também volume e recurso de conta | controles de EC2 |
| 15.6 | descartar evento incompleto | identificador sintético da conta | o EventBridge só admite eventos de controles cadastrados (L3) |
| 17.1–17.3 | 15 / 45 / 60 s | 60 / 90 / 600 s | os valores originais não cobriam o pior caso (*Property 28*) |

## Glossary

- **Compliance_Loop**: O sistema completo de automação de conformidade baseado no padrão MAPE-K
- **Monitor**: Componente que detecta eventos de mutação em recursos cloud via CloudTrail e EventBridge
- **Analyze**: Componente Lambda que classifica o estado de conformidade do recurso usando OPA/Rego
- **Execute**: Componente Lambda que executa remediação automática usando OpenTofu
- **Knowledge_Base**: Banco de dados DynamoDB que armazena políticas, exceções aprovadas e log de violações
- **Orchestrator**: Step Functions Standard que coordena as transições entre componentes do loop
- **S_nominal**: Estado formal indicando que o recurso está conforme com a política vigente
- **S_exception**: Estado formal indicando que o recurso possui exceção aprovada e vigente
- **S_violation**: Estado formal indicando que o recurso está em violação sem exceção válida
- **Control**: Conjunto de política Rego, evento CloudTrail e recurso alvo que define uma regra de conformidade
- **Exception**: Autorização temporária que permite desvio de conformidade para recurso específico
- **violations_log**: Registro persistente de todas as avaliações de conformidade no Knowledge_Base
- **Escalation_Queue**: Fila SQS FIFO que armazena violações não remediadas para notificação ao operador
- **Remediation**: Ação automatizada que corrige configuração de recurso para estado conforme

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
2. THE Knowledge_Base SHALL use exception_id (UUID) as partition key
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

1. WHEN Execute returns failure status, THE Orchestrator SHALL send a message to the Escalation_Queue
2. THE Escalation_Queue SHALL be a SQS FIFO queue
3. THE escalation message SHALL include resource_arn
4. THE escalation message SHALL include control_id
5. THE escalation message SHALL include error details from Execute
6. THE escalation message SHALL include violation timestamp
7. THE escalation message SHALL include violation_id reference
8. THE escalation message SHALL include step_functions_execution_url for navigation to execution details
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
4. THE Execute SHALL use local ephemeral state backend (/tmp/remediation.tfstate)

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
3. THE Monitor SHALL extract detail.requestParameters.bucketName field
4. THE Monitor SHALL extract detail.userIdentity.accountId field
5. THE Monitor SHALL extract detail.awsRegion field
6. IF any required field is missing, THEN THE Monitor SHALL log error and discard event *(revisado)*
7. THE Monitor SHALL construct resource ARN in format "arn:aws:s3:::bucket-name"

### Requirement 16: Garantir Determinismo na Avaliação de Políticas

**User Story:** Como desenvolvedor de políticas, quero que avaliação de políticas seja determinística, para que o mesmo recurso sempre produza a mesma classificação.

#### Acceptance Criteria

1. WHEN Analyze evaluates the same resource configuration multiple times, THE Analyze SHALL return the same classification
2. THE Analyze SHALL not depend on external state beyond Knowledge_Base
3. THE Analyze SHALL use consistent timestamp for exception expiration checks within single evaluation
4. THE OPA evaluation SHALL not use non-deterministic built-in functions

### Requirement 17: Implementar Timeout para Componentes

**User Story:** Como engenheiro de confiabilidade, quero que componentes tenham timeout configurado, para que operações lentas não bloqueiem o loop indefinidamente.

#### Acceptance Criteria

1. THE Analyze SHALL timeout after 15 seconds *(revisado)*
2. THE Execute SHALL timeout after 45 seconds *(revisado)*
3. THE Orchestrator SHALL timeout the entire workflow after 60 seconds *(revisado)*
4. WHEN a component times out, THE Orchestrator SHALL log timeout error and terminate workflow

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

1. WHEN a GitHub Issue is created for a failed remediation, THE Compliance_Loop SHALL capture the issue number returned by GitHub API
2. THE github_issue_id SHALL be saved to the violations_log entry corresponding to the violation
3. THE violations_log entry SHALL link the violation event to the operational resolution workflow
4. WHEN querying violations_log by violation_id, THE result SHALL include github_issue_id if escalation occurred
5. THE github_issue_id field SHALL be absent from every violations_log entry that was not escalated (S_nominal, S_exception, or remediation_status = success)
