# Implementation Plan: MVP Compliance Automation Loop

## Overview

Este plano implementa o sistema de automação de conformidade baseado no padrão MAPE-K para AWS S3. A implementação segue a ordem lógica: infraestrutura base → componentes individuais → integração → testes. O sistema usa Python 3.12 para Lambdas, OpenTofu para remediação, e DynamoDB para persistência.

## Tasks

- [x] 1. Configurar estrutura do projeto e dependências
  - Criar estrutura de diretórios (infra/, lambdas/, policies/, tests/)
  - Criar requirements.txt para Lambdas Python (boto3, hypothesis, pytest, moto)
  - Criar Dockerfile base para Execute Lambda com OpenTofu 1.6+
  - _Requirements: 4.3, 7.3_

- [x] 2. Implementar infraestrutura DynamoDB com OpenTofu
  - [x] 2.1 Criar tabela controls com schema completo
    - Partition key: control_id (String)
    - Atributos: description, cloudtrail_events, rego_policy_package, resource_type, severity, created_at
    - Billing mode: PAY_PER_REQUEST
    - _Requirements: 5.1_
  
  - [x] 2.2 Criar tabela exceptions com GSI e TTL
    - Partition key: exception_id (String)
    - GSI: control_id-index (control_id como hash key)
    - TTL attribute: valid_until
    - Atributos: resource_arn, justification, approved_by, created_at, approval_ticket
    - _Requirements: 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.10, 5.11, 5.12_
  
  - [x] 2.3 Criar tabela violations_log com GSI
    - Partition key: violation_id (String)
    - GSI: resource_arn-timestamp-index (resource_arn como hash key, timestamp como range key)
    - Atributos: control_id, state, policy_version, exception_id, remediation_status, remediation_error, remediation_timestamp, github_issue_id, account_id, region
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 6.9, 6.10, 6.11_

- [x] 3. Implementar infraestrutura de monitoramento com OpenTofu
  - [x] 3.1 Criar CloudTrail trail para eventos S3
    - Configurar captura de eventos de gerenciamento S3
    - Configurar bucket S3 para logs do CloudTrail
    - _Requirements: 1.1_
  
  - [x] 3.2 Criar EventBridge Rule para PutBucketPublicAccessBlock
    - Event pattern: source=aws.s3, detail-type=AWS API Call via CloudTrail, eventName=PutBucketPublicAccessBlock
    - Target: Step Functions state machine
    - _Requirements: 1.1, 1.6, 15.2_
  
  - [x] 3.3 Configurar Input Transformer na EventBridge Rule
    - Mapear detail.requestParameters.bucketName para resource_id
    - Construir resource_arn no formato arn:aws:s3:::bucket-name usando template syntax
    - Nota técnica: EventBridge Input Transformer usa template "arn:aws:s3:::<bucket>" onde <bucket> é JSONPath $.detail.requestParameters.bucketName. Testar com bucket names contendo pontos.
    - Mapear detail.userIdentity.accountId para account_id
    - Mapear detail.awsRegion para region
    - Mapear detail.eventTime para event_time
    - Adicionar control_id fixo: ctrl-s3-public-block
    - Output schema: {resource_arn, control_id, account_id, region, event_time}
    - _Requirements: 1.2, 1.3, 1.4, 1.5, 15.7_

- [x] 4. Implementar política Rego para S3 public access
  - Criar arquivo policies/s3_public_access.rego
  - Implementar regra allow com verificação dos 4 settings (BlockPublicAcls, IgnorePublicAcls, BlockPublicPolicy, RestrictPublicBuckets)
  - Usar sintaxe moderna Rego com := e if
  - _Requirements: 4.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7_

- [ ]* 4.1 Escrever property test para política Rego
  - **Property 7: S3 Public Access Policy Evaluation**
  - **Valida: Requirements 10.2, 10.3, 10.4, 10.5, 10.6**
  - Gerar todas as 16 combinações de 4 booleans
  - Verificar que policy retorna violation quando qualquer setting é false
  - Verificar que policy retorna compliant quando todos são true
  - Mínimo 100 iterações com Hypothesis

- [ ] 5. Implementar Analyze Lambda
  - [x] 5.1 Criar handler Python 3.12 para Analyze Lambda
    - Extrair resource_arn e control_id do evento
    - Chamar S3 GetPublicAccessBlock para obter configuração atual
    - Integrar OPA/Rego para avaliação de política (subprocess ou opa-python)
    - Consultar DynamoDB exceptions via GSI control_id-index
    - Filtrar exceções por resource_arn e valid_until > now()
    - Classificar estado (S_nominal, S_exception, S_violation)
    - Gerar violation_id (UUID)
    - Escrever entrada em violations_log
    - Retornar {state, violation_id, exception_id, approved_by}
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 5.8, 5.9, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8_
  
  - [x] 5.2 Escrever unit tests para Analyze Lambda
    - Test: recurso compliant retorna S_nominal
    - Test: recurso não-compliant com exceção válida retorna S_exception
    - Test: recurso não-compliant sem exceção retorna S_violation
    - Test: exceção expirada é ignorada
    - Test: erro em GetPublicAccessBlock propaga exceção
    - Test: erro em OPA retorna S_violation (fail-secure)
    - Test: timeout em DynamoDB query procede sem exceção
    - _Requirements: 3.3, 3.4, 3.5, 3.7, 3.8_
  
  - [x] 5.3 Escrever property tests para Analyze Lambda
    - **Property 3: Analyze Classification Determinism**
    - **Valida: Requirements 3.9, 16.3**
    - **Property 4: Compliant Resource Classification**
    - **Valida: Requirements 3.3, 10.7**
    - **Property 5: Valid Exception Classification**
    - **Valida: Requirements 3.4, 3.7**
    - **Property 6: Violation Classification**
    - **Valida: Requirements 3.5, 3.8**
    - **Property 8: Exception Query Correctness**
    - **Valida: Requirements 3.6, 5.8**
    - Gerar configurações S3 aleatórias, exceções com timestamps variados, resource ARNs
    - Mínimo 100 iterações por property com Hypothesis

- [ ] 6. Implementar Execute Lambda com container image
  - [x] 6.1 Criar Dockerfile para Execute Lambda
    - Base image: public.ecr.aws/lambda/python:3.12
    - Instalar OpenTofu 1.6+ binary
    - Copiar remediation.tf para /opt/remediation/
    - Executar tofu providers mirror /opt/providers
    - Executar tofu init -plugin-dir=/opt/providers
    - Copiar handler.py para ${LAMBDA_TASK_ROOT}/
    - _Requirements: 7.3_
  
  - [x] 6.2 Criar configuração HCL remediation.tf
    - Provider AWS com variável region
    - Variável bucket_name
    - Resource aws_s3_bucket_public_access_block com os 4 settings = true
    - _Requirements: 7.4, 7.9_
  
  - [x] 6.3 Criar handler Python para Execute Lambda
    - Extrair resource_arn e region do evento
    - Remover /tmp/remediation/ se existir (warm container cleanup)
    - Criar /tmp/remediation/ com makedirs
    - Copiar /opt/remediation/remediation.tf para /tmp/remediation/
    - Copiar /opt/remediation/.terraform/ para /tmp/remediation/.terraform/
    - Escrever terraform.tfvars.json com bucket_name e region
    - Executar subprocess: tofu apply -refresh=false -auto-approve -var-file=/tmp/terraform.tfvars.json -state=/tmp/remediation.tfstate -plugin-dir=/opt/providers
    - Timeout: 40 segundos
    - Retornar {remediation_status: "success", output} se returncode == 0
    - Retornar {remediation_status: "failed", error: stderr} se returncode != 0
    - _Requirements: 7.1, 7.2, 7.3, 7.5, 7.6, 7.8, 9.4_
  
  - [x] 6.4 Escrever unit tests para Execute Lambda
    - Test: bucket não-compliant é remediado com sucesso
    - Test: bucket já compliant retorna success (idempotência)
    - Test: bucket inexistente retorna failure
    - Test: erro de permissão IAM retorna failure com erro
    - Test: timeout de OpenTofu retorna failure
    - Mock subprocess.run para simular tofu apply
    - _Requirements: 7.5, 7.6, 7.7, 9.1_
  
  - [x] 6.5 Escrever property tests para Execute Lambda
    - **Property 15: Execute Idempotency**
    - **Valida: Requirements 7.7, 9.1, 11.5**
    - **Property 16: Execute OpenTofu Invocation**
    - **Valida: Requirements 7.1, 7.2, 7.3**
    - **Property 17: Execute Success Response**
    - **Valida: Requirements 7.5**
    - **Property 18: Execute Failure Response**
    - **Valida: Requirements 7.6**
    - Gerar bucket names aleatórios, regiões, outputs de OpenTofu
    - Mínimo 100 iterações por property com Hypothesis

- [x] 6.6 Criar repositório ECR e pipeline de build da container image
  - Criar repositório ECR para Execute Lambda
  - Configurar docker build com tag apropriada
  - Configurar docker push para ECR
  - Documentar processo de build e deploy da imagem
  - _Requirements: 7.3_

- [ ] 7. Implementar Step Functions state machine
  - [x] 7.1 Criar definição ASL completa da state machine
    - State InvokeAnalyze: Task Lambda com timeout 15s
    - State ClassifyState: Choice com 3 branches (S_nominal, S_exception, S_violation)
    - State RegisterNominal: Task DynamoDB PutItem para violations_log
    - State RegisterException: Task DynamoDB PutItem para violations_log com exception_id
    - State InvokeExecute: Task Lambda com timeout 45s e Catch para States.ALL
    - State CheckRemediationStatus: Choice baseado em remediation_status
    - State UpdateLogSuccess: Task DynamoDB UpdateItem com remediation_status=success
    - State UpdateLogFailed: Task DynamoDB UpdateItem com remediation_status=failed e remediation_error
    - State EnqueueEscalation: Task SQS SendMessage com MessageGroupId=resource_arn e MessageDeduplicationId=violation_id
    - Timeout global: 60 segundos
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 17.3_
  
  - [ ]* 7.2 Escrever unit tests para validação ASL
    - Test: ASL é JSON válido
    - Test: todos os estados requeridos estão presentes
    - Test: Choice states têm branches corretos
    - Test: Timeouts estão configurados corretamente
    - Test: Catch handlers estão configurados
    - _Requirements: 2.2, 2.3, 2.4_
  
  - [ ]* 7.3 Escrever property tests para orquestração
    - **Property 2: Orchestrator State-Based Routing**
    - **Valida: Requirements 2.2, 2.3, 2.4**
    - Gerar outputs de Analyze com diferentes states
    - Verificar que S_nominal e S_exception não invocam Execute
    - Verificar que S_violation invoca Execute
    - Usar LocalStack para simular Step Functions

- [ ] 8. Implementar infraestrutura de escalação com OpenTofu
  - [ ] 8.1 Criar SQS FIFO queue para escalação
    - QueueName: compliance-escalation.fifo
    - FifoQueue: true
    - ContentBasedDeduplication: false
    - MessageRetentionPeriod: 1209600 (14 dias)
    - VisibilityTimeout: 300
    - DeduplicationScope: messageGroup
    - FifoThroughputLimit: perMessageGroupId
    - Configurar DLQ para mensagens falhadas
    - _Requirements: 8.2_
  
  - [ ] 8.2 Criar IAM role para Step Functions com permissões SQS SendMessage
    - _Requirements: 8.1_

- [ ] 9. Implementar Escalation Lambda
  - [ ] 9.1 Criar handler Python 3.12 para Escalation Lambda
    - Trigger: SQS FIFO queue (batch size: 1)
    - Parsear mensagem do SQS (violation_id, resource_arn, control_id, error, timestamp, step_functions_execution_arn)
    - Construir URL do console AWS para execution details
    - Criar GitHub Issue via API (requests library)
    - Issue title: [Compliance] {control_id} - {resource_arn}
    - Issue body: incluir todos os detalhes da violação e link para execution
    - Issue labels: compliance-violation, auto-escalation
    - Capturar issue number do response
    - Atualizar violations_log com github_issue_id
    - Environment variables: GITHUB_TOKEN, GITHUB_REPO
    - _Requirements: 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 8.9, 8.10, 8.11, 8.12, 19.1, 19.2_
  
  - [ ]* 9.2 Escrever unit tests para Escalation Lambda
    - Test: mensagem SQS válida cria GitHub Issue
    - Test: GitHub Issue contém todos os campos requeridos
    - Test: github_issue_id é salvo em violations_log
    - Test: erro de GitHub API retrigger via SQS visibility timeout
    - Test: erro de DynamoDB update loga erro mas continua
    - Mock requests.post para GitHub API
    - Mock boto3 DynamoDB update_item
    - _Requirements: 8.9, 8.10, 8.11, 8.12_
  
  - [ ]* 9.3 Escrever property tests para Escalation Lambda
    - **Property 20: Escalation Trigger on Failure**
    - **Valida: Requirements 2.6, 8.1, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8**
    - **Property 21: GitHub Issue Creation**
    - **Valida: Requirements 8.9, 8.10, 8.11**
    - **Property 22: GitHub Issue ID Persistence**
    - **Valida: Requirements 8.12, 19.1, 19.4**
    - Gerar mensagens de escalação aleatórias com erros variados
    - Mínimo 100 iterações por property com Hypothesis

- [x] 10. Implementar IAM roles e policies com OpenTofu
  - [x] 10.1 Criar IAM role para Analyze Lambda
    - Permissões: s3:GetPublicAccessBlock, dynamodb:Query (exceptions), dynamodb:PutItem (violations_log), logs:CreateLogGroup, logs:CreateLogStream, logs:PutLogEvents
    - _Requirements: 3.1, 3.6, 6.1_
  
  - [x] 10.2 Criar IAM role para Execute Lambda
    - Permissões: s3:PutBucketPublicAccessBlock, logs:CreateLogGroup, logs:CreateLogStream, logs:PutLogEvents
    - _Requirements: 7.1_
  
  - [x] 10.3 Criar IAM role para Escalation Lambda
    - Permissões: sqs:ReceiveMessage, sqs:DeleteMessage, sqs:GetQueueAttributes, dynamodb:UpdateItem (violations_log), logs:CreateLogGroup, logs:CreateLogStream, logs:PutLogEvents
    - _Requirements: 9.1_
  
  - [x] 10.4 Criar IAM role para Step Functions
    - Permissões: lambda:InvokeFunction, dynamodb:PutItem, dynamodb:UpdateItem, sqs:SendMessage, logs:CreateLogGroup, logs:CreateLogStream, logs:PutLogEvents
    - _Requirements: 2.1, 2.6_

- [x] 11. Implementar validação de input no Analyze Lambda
  - [x] 11.1 Criar validação de input transformado pela EventBridge
    - Validar presença de resource_arn, control_id, account_id, region, event_time
    - Validar formato de resource_arn (arn:aws:s3:::bucket-name)
    - Retornar erro se campos obrigatórios ausentes
    - Nota: EventBridge Input Transformer já faz parsing do CloudTrail; Analyze valida o schema transformado
    - _Requirements: 1.2, 1.3, 1.4, 15.1, 15.2, 15.3, 15.4, 15.5, 15.6, 15.7_
  
  - [ ]* 11.2 Escrever unit tests para validação de input
    - Test: input válido passa validação
    - Test: input com campo faltando retorna erro
    - Test: ARN com formato inválido retorna erro
    - _Requirements: 15.6, 15.7_
  
  - [ ]* 11.3 Escrever property tests para validação de input
    - **Property 1: CloudTrail Event Parsing Completeness**
    - **Valida: Requirements 1.2, 1.3, 1.4, 1.5, 15.1, 15.7**
    - **Property 25: EventBridge Rule Event Filtering**
    - **Valida: Requirements 1.6**
    - **Property 26: Monitor Error Handling**
    - **Valida: Requirements 15.6**
    - **Property 27: ARN Construction Format**
    - **Valida: Requirements 15.7**
    - Gerar inputs transformados aleatórios com campos válidos e inválidos
    - Mínimo 100 iterações por property com Hypothesis

- [x] 12. Checkpoint - Garantir que todos os componentes individuais estão funcionando
  - Executar todos os unit tests
  - Executar todos os property tests
  - Verificar que não há erros de sintaxe ou imports
  - Perguntar ao usuário se há dúvidas ou ajustes necessários

- [ ] 13. Implementar dados de teste para os 4 cenários
  - [ ] 13.1 Criar fixture de controle ctrl-s3-public-block na tabela controls
    - Nota: Para o MVP, o Analyze não consulta a tabela controls em runtime (control_id vem do evento, política Rego é hardcoded no deployment package). A tabela é populada para futura extensibilidade quando múltiplos controles forem suportados.
    - _Requirements: 10.1_
  
  - [ ] 13.2 Criar fixture de exceção válida para C2
    - exception_id, control_id=ctrl-s3-public-block, resource_arn de teste, valid_until no futuro
    - _Requirements: 12.1, 12.2_
  
  - [ ] 13.3 Criar fixture de exceção expirada para C3
    - exception_id, control_id=ctrl-s3-public-block, resource_arn de teste, valid_until no passado
    - _Requirements: 13.1, 13.2_
  
  - [ ] 13.4 Criar fixtures de eventos CloudTrail para os 4 cenários
    - _Requirements: 11.1, 12.1, 13.1, 14.1_

- [ ]* 14. Implementar integration tests para os 4 cenários end-to-end
  - Nota: LocalStack suporta Step Functions, mas SDK integrations diretas com DynamoDB e SQS podem ter limitações. Se SDK integrations não funcionarem no LocalStack, considere testes contra AWS real ou mocking adicional das integrações.
  
  - [ ]* 14.1 Test C1: Remediação automática
    - Criar bucket S3 com public access enabled no LocalStack
    - Inserir controle ctrl-s3-public-block em DynamoDB
    - Simular evento CloudTrail PutBucketPublicAccessBlock
    - Verificar que Analyze classifica como S_violation
    - Verificar que Execute remedia o bucket
    - Verificar que violations_log contém entry com remediation_status=success
    - Verificar que bucket tem os 4 public access blocks enabled
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5_
  
  - [ ]* 14.2 Test C2: Exceção válida
    - Criar bucket S3 com public access enabled no LocalStack
    - Inserir controle e exceção válida em DynamoDB
    - Simular evento CloudTrail
    - Verificar que Analyze classifica como S_exception
    - Verificar que Execute não é invocado
    - Verificar que violations_log contém entry com state=S_exception e exception_id
    - Verificar que bucket permanece inalterado
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5_
  
  - [ ]* 14.3 Test C3: Exceção expirada
    - Criar bucket S3 com public access enabled no LocalStack
    - Inserir controle e exceção expirada em DynamoDB
    - Simular evento CloudTrail
    - Verificar que Analyze classifica como S_violation
    - Verificar que Execute remedia o bucket
    - Verificar que violations_log contém entry com state=S_violation e remediation_status=success
    - _Requirements: 13.1, 13.2, 13.3, 13.4_
  
  - [ ]* 14.4 Test C4: Falha na remediação
    - Criar bucket S3 com public access enabled no LocalStack
    - Inserir controle em DynamoDB
    - Remover permissões IAM do Execute Lambda (simular AccessDenied)
    - Simular evento CloudTrail
    - Verificar que Analyze classifica como S_violation
    - Verificar que Execute falha com erro de permissão
    - Verificar que mensagem é enviada para Escalation_Queue
    - Verificar que GitHub Issue é criado
    - Verificar que violations_log contém entry com state=S_violation, remediation_status=failed, e github_issue_id
    - Verificar que bucket permanece não-compliant
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.5_

- [ ]* 15. Implementar property tests adicionais para data models
  - **Property 9: Exception Data Model Completeness**
  - **Valida: Requirements 5.4, 5.5, 5.6, 5.10, 5.11, 5.12**
  - **Property 10: Exception Filtering Independence from TTL**
  - **Valida: Requirements 5.9**
  - **Property 11: Violations Log Entry Uniqueness**
  - **Valida: Requirements 6.1, 6.2**
  - **Property 12: Violations Log Data Model Completeness**
  - **Valida: Requirements 6.3, 6.4, 6.5, 6.6, 6.7**
  - **Property 13: Exception Reference in Log**
  - **Valida: Requirements 6.8**
  - **Property 14: Remediation Status Logging**
  - **Valida: Requirements 6.9, 6.10, 6.11**
  - **Property 19: Remediation Outcome Compliance**
  - **Valida: Requirements 7.9**
  - **Property 23: GitHub Issue ID Null for Non-Escalated Violations**
  - **Valida: Requirements 19.5**
  - **Property 24: State Persistence After Failed Remediation**
  - **Valida: Requirements 18.1, 18.2, 18.5**
  - **Property 28: Timeout Error Handling**
  - **Valida: Requirements 17.4**
  - **Property 29: Resource State Unchanged for S_exception**
  - **Valida: Requirements 12.5**
  - **Property 30: Resource State Unchanged After Failed Remediation**
  - **Valida: Requirements 14.5**
  - Mínimo 100 iterações por property com Hypothesis

- [ ] 16. Checkpoint final - Garantir que todos os testes passam
  - Executar todos os unit tests
  - Executar todos os property tests (mínimo 100 iterações cada)
  - Executar todos os integration tests
  - Verificar cobertura de código (target: >80%)
  - Perguntar ao usuário se há dúvidas ou ajustes necessários

## Notes

- Tasks marcadas com `*` são opcionais e podem ser puladas para MVP mais rápido
- Cada task referencia requirements específicos para rastreabilidade
- Checkpoints garantem validação incremental
- Property tests validam propriedades universais de corretude (mínimo 100 iterações)
- Unit tests validam exemplos específicos e edge cases
- Integration tests validam os 4 cenários end-to-end (C1-C4)
- LocalStack é usado para mocking de serviços AWS em testes
- Hypothesis é usado para property-based testing em Python
