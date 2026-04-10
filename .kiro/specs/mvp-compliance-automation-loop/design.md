# Design Document: MVP Compliance Automation Loop

## Overview

O MVP Compliance Automation Loop implementa um sistema de automação de conformidade baseado no padrão arquitetural MAPE-K (Monitor, Analyze, Plan, Execute, Knowledge). O sistema detecta desvios de conformidade em recursos AWS, classifica o estado do recurso, executa remediação automática quando apropriado, e escala violações não resolvidas para operadores humanos.

O MVP foca em um controle específico: bloqueio de acesso público em buckets S3 (ctrl-s3-public-block). O sistema monitora eventos CloudTrail, avalia conformidade usando políticas Rego executadas pelo Open Policy Agent (OPA), e remedia violações usando OpenTofu com infraestrutura como código declarativa.

### Design Goals

- **Automação determinística**: Cada evento deve seguir fluxo previsível baseado em classificação de estado
- **Auditabilidade completa**: Todas as avaliações e ações devem ser registradas permanentemente
- **Idempotência**: Remediação deve ser segura para execução repetida
- **Separação de responsabilidades**: Políticas versionadas em Git, estado em DynamoDB, execução em Lambda
- **Escalação confiável**: Falhas devem ser notificadas sem perda de informação

### MAPE-K Architecture Mapping

- **Monitor**: CloudTrail + EventBridge Rule (filtra PutBucketPublicAccessBlock)
- **Analyze**: Lambda Python + OPA/Rego embedded + consulta DynamoDB exceptions
- **Plan**: Implícito no Step Functions (orquestração de estados via Choice states)
- **Execute**: Lambda container + OpenTofu com state efêmero local
- **Knowledge**: DynamoDB (controls, exceptions, violations_log)

### State Classification Model

O sistema classifica recursos em três estados formais:

- **S_nominal**: Recurso conforme com política vigente
- **S_exception**: Recurso não conforme mas com exceção aprovada e vigente
- **S_violation**: Recurso não conforme sem exceção válida (requer remediação)


## Architecture

### High-Level Architecture Diagram

```mermaid
graph TB
    subgraph "Monitor"
        CT[CloudTrail]
        EB[EventBridge Rule]
    end
    
    subgraph "Orchestrator"
        SF[Step Functions]
    end
    
    subgraph "Analyze"
        AL[Analyze Lambda]
        OPA[OPA/Rego Engine]
    end
    
    subgraph "Execute"
        EL[Execute Lambda]
        TF[OpenTofu]
    end
    
    subgraph "Knowledge Base"
        DB1[(controls)]
        DB2[(exceptions)]
        DB3[(violations_log)]
    end
    
    subgraph "Escalation"
        SQS[SQS FIFO Queue]
        ESC[Escalation Lambda]
        GH[GitHub Issues API]
    end
    
    S3[S3 Bucket] -->|PutBucketPublicAccessBlock| CT
    CT -->|Event| EB
    EB -->|Trigger| SF
    SF -->|Invoke| AL
    AL -->|Query| DB2
    AL -->|Evaluate| OPA
    AL -->|Log| DB3
    SF -->|S_violation| EL
    EL -->|Apply| TF
    TF -->|Remediate| S3
    SF -->|Failure| SQS
    SQS -->|Consume| ESC
    ESC -->|Create Issue| GH
    ESC -->|Update| DB3
```

### Workflow State Machine Diagram

```mermaid
stateDiagram-v2
    [*] --> Monitor
    Monitor --> Analyze
    Analyze --> S_nominal: Compliant
    Analyze --> S_exception: Valid Exception
    Analyze --> S_violation: Non-compliant
    S_nominal --> RegisterNominal
    S_exception --> RegisterException
    S_violation --> Execute
    Execute --> RegisterRemediated: Success
    Execute --> RemediationFailed: Failure
    RemediationFailed --> EnqueueEscalation
    EnqueueEscalation --> CreateGitHubIssue
    RegisterNominal --> [*]
    RegisterException --> [*]
    RegisterRemediated --> [*]
    CreateGitHubIssue --> [*]
```

### Component Interaction Flow

```mermaid
sequenceDiagram
    participant S3
    participant CloudTrail
    participant EventBridge
    participant StepFunctions
    participant AnalyzeLambda
    participant DynamoDB
    participant OPA
    participant ExecuteLambda
    participant OpenTofu
    participant SQS
    participant EscalationLambda
    participant GitHub
    
    S3->>CloudTrail: PutBucketPublicAccessBlock
    CloudTrail->>EventBridge: Event
    EventBridge->>StepFunctions: StartExecution
    StepFunctions->>AnalyzeLambda: Invoke
    AnalyzeLambda->>S3: GetPublicAccessBlock
    AnalyzeLambda->>DynamoDB: Query exceptions
    AnalyzeLambda->>OPA: Evaluate policy
    AnalyzeLambda->>DynamoDB: Write violations_log
    AnalyzeLambda-->>StepFunctions: {state, violation_id}
    
    alt S_violation
        StepFunctions->>ExecuteLambda: Invoke
        ExecuteLambda->>OpenTofu: tofu apply
        OpenTofu->>S3: Update configuration
        ExecuteLambda-->>StepFunctions: {status: success}
        StepFunctions->>DynamoDB: Update violations_log
    else Execute fails
        ExecuteLambda-->>StepFunctions: {status: failed, error}
        StepFunctions->>SQS: SendMessage
        SQS->>EscalationLambda: Trigger
        EscalationLambda->>GitHub: Create Issue
        EscalationLambda->>DynamoDB: Update violations_log
    end
```


## Components and Interfaces

### 1. Monitor (CloudTrail + EventBridge)

**Responsibility**: Detectar eventos de mutação em recursos S3 e extrair metadados relevantes.

**Implementation**:
- CloudTrail trail configurado para capturar eventos de gerenciamento S3
- EventBridge Rule com pattern filter para `PutBucketPublicAccessBlock`
- Target: Step Functions state machine

**Event Pattern**:
```json
{
  "source": ["aws.s3"],
  "detail-type": ["AWS API Call via CloudTrail"],
  "detail": {
    "eventName": ["PutBucketPublicAccessBlock"]
  }
}
```

**Output Schema**:
```json
{
  "resource_id": "bucket-name",
  "resource_arn": "arn:aws:s3:::bucket-name",
  "account_id": "123456789012",
  "region": "us-east-1",
  "event_time": "2024-01-15T10:30:00Z",
  "control_id": "ctrl-s3-public-block"
}
```

**Error Handling**:
- Missing fields: Log error to CloudWatch, discard event
- Malformed JSON: EventBridge DLQ captures failed invocations



### 2. Orchestrator (Step Functions Standard)

**Responsibility**: Coordenar transições entre componentes do loop MAPE-K baseado em classificação de estado.

**Implementation**:
- Step Functions Standard workflow (não Express) para histórico de 90 dias
- Choice states para decisão baseada em `$.state`
- SDK integrations diretas para DynamoDB (sem Lambda intermediária)
- Timeout global de 60 segundos

**State Machine Definition** (simplified):
```json
{
  "StartAt": "InvokeAnalyze",
  "States": {
    "InvokeAnalyze": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:REGION:ACCOUNT:function:analyze",
      "TimeoutSeconds": 15,
      "Next": "ClassifyState"
    },
    "ClassifyState": {
      "Type": "Choice",
      "Choices": [
        {
          "Variable": "$.state",
          "StringEquals": "S_nominal",
          "Next": "RegisterNominal"
        },
        {
          "Variable": "$.state",
          "StringEquals": "S_exception",
          "Next": "RegisterException"
        },
        {
          "Variable": "$.state",
          "StringEquals": "S_violation",
          "Next": "InvokeExecute"
        }
      ]
    },
    "RegisterNominal": {
      "Type": "Task",
      "Resource": "arn:aws:states:::dynamodb:putItem",
      "Parameters": {
        "TableName": "violations_log",
        "Item": {
          "violation_id": {"S.$": "$.violation_id"},
          "state": {"S": "S_nominal"},
          "timestamp": {"S.$": "$.timestamp"}
        }
      },
      "End": true
    },
    "RegisterException": {
      "Type": "Task",
      "Resource": "arn:aws:states:::dynamodb:putItem",
      "Parameters": {
        "TableName": "violations_log",
        "Item": {
          "violation_id": {"S.$": "$.violation_id"},
          "state": {"S": "S_exception"},
          "exception_id": {"S.$": "$.exception_id"},
          "timestamp": {"S.$": "$.timestamp"}
        }
      },
      "End": true
    },
    "InvokeExecute": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:REGION:ACCOUNT:function:execute",
      "TimeoutSeconds": 45,
      "Catch": [
        {
          "ErrorEquals": ["States.ALL"],
          "ResultPath": "$.error",
          "Next": "UpdateLogFailed"
        }
      ],
      "Next": "CheckRemediationStatus"
    },
    "CheckRemediationStatus": {
      "Type": "Choice",
      "Choices": [
        {
          "Variable": "$.remediation_status",
          "StringEquals": "success",
          "Next": "UpdateLogSuccess"
        }
      ],
      "Default": "UpdateLogFailed"
    },
    "UpdateLogSuccess": {
      "Type": "Task",
      "Resource": "arn:aws:states:::dynamodb:updateItem",
      "Parameters": {
        "TableName": "violations_log",
        "Key": {
          "violation_id": {"S.$": "$.violation_id"}
        },
        "UpdateExpression": "SET remediation_status = :status, remediation_timestamp = :ts",
        "ExpressionAttributeValues": {
          ":status": {"S": "success"},
          ":ts": {"S.$": "$$.State.EnteredTime"}
        }
      },
      "End": true
    },
    "UpdateLogFailed": {
      "Type": "Task",
      "Resource": "arn:aws:states:::dynamodb:updateItem",
      "Parameters": {
        "TableName": "violations_log",
        "Key": {
          "violation_id": {"S.$": "$.violation_id"}
        },
        "UpdateExpression": "SET remediation_status = :status, remediation_error = :error, remediation_timestamp = :ts",
        "ExpressionAttributeValues": {
          ":status": {"S": "failed"},
          ":error": {"S.$": "$.error"},
          ":ts": {"S.$": "$$.State.EnteredTime"}
        }
      },
      "Next": "EnqueueEscalation"
    },
    "EnqueueEscalation": {
      "Type": "Task",
      "Resource": "arn:aws:states:::sqs:sendMessage",
      "Parameters": {
        "QueueUrl": "https://sqs.REGION.amazonaws.com/ACCOUNT/escalation-queue.fifo",
        "MessageBody.$": "States.JsonToString($)",
        "MessageGroupId.$": "$.resource_arn",
        "MessageDeduplicationId.$": "$.violation_id"
      },
      "End": true
    }
  }
}
```

**Input Schema**:
```json
{
  "resource_arn": "arn:aws:s3:::bucket-name",
  "control_id": "ctrl-s3-public-block",
  "account_id": "123456789012",
  "region": "us-east-1",
  "event_time": "2024-01-15T10:30:00Z"
}
```

**Interface Contract**:
- Analyze returns: `{state, violation_id, exception_id?, approved_by?}`
- Execute returns: `{remediation_status: "success"|"failed", error?}`



### 3. Analyze Lambda

**Responsibility**: Classificar estado de conformidade do recurso usando OPA/Rego e consulta de exceções.

**Implementation**:
- Runtime: Python 3.12
- OPA embedded via `opa-python` library ou subprocess call to OPA binary
- Deployment package inclui arquivo `.rego` com política `s3_public_access`
- Timeout: 15 segundos
- Memory: 512 MB

**Algorithm**:
```python
def analyze(event):
    # 1. Extract resource metadata
    resource_arn = event['resource_arn']
    control_id = event['control_id']
    
    # 2. Retrieve current resource configuration
    s3_client = boto3.client('s3')
    bucket_name = extract_bucket_name(resource_arn)
    config = s3_client.get_public_access_block(Bucket=bucket_name)
    
    # 3. Evaluate with OPA
    opa_input = {
        "resource": config['PublicAccessBlockConfiguration'],
        "control_id": control_id
    }
    policy_result = opa.evaluate("data.s3_public_access.allow", opa_input)
    
    # 4. Check for valid exception
    exception = query_exception(control_id, resource_arn)
    
    # 5. Classify state
    if policy_result.allow:
        state = "S_nominal"
    elif exception and exception['valid_until'] > now():
        state = "S_exception"
    else:
        state = "S_violation"
    
    # 6. Log to violations_log
    violation_id = str(uuid.uuid4())
    log_violation(violation_id, resource_arn, control_id, state, exception)
    
    return {
        "state": state,
        "violation_id": violation_id,
        "exception_id": exception['exception_id'] if exception else None
    }
```

**DynamoDB Query for Exceptions**:
```python
def query_exception(control_id, resource_arn):
    response = dynamodb.query(
        TableName='exceptions',
        IndexName='control_id-index',
        KeyConditionExpression='control_id = :cid',
        FilterExpression='resource_arn = :arn AND valid_until > :now',
        ExpressionAttributeValues={
            ':cid': control_id,
            ':arn': resource_arn,
            ':now': int(time.time())
        }
    )
    return response['Items'][0] if response['Items'] else None
```

**Rego Policy** (`s3_public_access.rego`):
```rego
package s3_public_access

default allow := false

allow if {
    input.resource.BlockPublicAcls == true
    input.resource.IgnorePublicAcls == true
    input.resource.BlockPublicPolicy == true
    input.resource.RestrictPublicBuckets == true
}
```

**Output Schema**:
```json
{
  "state": "S_violation",
  "violation_id": "550e8400-e29b-41d4-a716-446655440000",
  "exception_id": null,
  "approved_by": null
}
```

**Error Handling**:
- S3 GetPublicAccessBlock fails: Return error to Step Functions, trigger escalation
- OPA evaluation error: Log to CloudWatch, return S_violation (fail-secure)
- DynamoDB query timeout: Proceed without exception (fail-secure)



### 4. Execute Lambda

**Responsibility**: Remediar recursos em violação usando OpenTofu com infraestrutura como código declarativa.

**Implementation**:
- Runtime: Lambda container image (base: `public.ecr.aws/lambda/python:3.12`)
- OpenTofu binary incluído na imagem (versão 1.6+)
- HCL configuration pré-inicializado em build time
- Providers AWS em `/opt/providers` (cache de `tofu init`)
- Timeout: 45 segundos
- Memory: 1024 MB
- Ephemeral storage: 1024 MB (para `/tmp/remediation.tfstate`)

**Dockerfile**:
```dockerfile
FROM public.ecr.aws/lambda/python:3.12

# Install OpenTofu
RUN curl -Lo /tmp/tofu.zip https://github.com/opentofu/opentofu/releases/download/v1.6.0/tofu_1.6.0_linux_amd64.zip && \
    unzip /tmp/tofu.zip -d /usr/local/bin && \
    chmod +x /usr/local/bin/tofu

# Copy HCL configuration
COPY remediation.tf /opt/remediation/
WORKDIR /opt/remediation

# Pre-cache providers and initialize
RUN tofu providers mirror /opt/providers
RUN tofu init -plugin-dir=/opt/providers

# Copy Lambda handler
COPY handler.py ${LAMBDA_TASK_ROOT}/

CMD ["handler.lambda_handler"]
```

**HCL Configuration** (`remediation.tf`):
```hcl
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

variable "bucket_name" {
  type = string
}

variable "region" {
  type = string
}

resource "aws_s3_bucket_public_access_block" "remediation" {
  bucket = var.bucket_name

  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = true
  restrict_public_buckets = true
}
```

**Lambda Handler**:
```python
import subprocess
import json
import os
import shutil

def lambda_handler(event, context):
    resource_arn = event['resource_arn']
    bucket_name = extract_bucket_name(resource_arn)
    region = event['region']
    
    # Copy HCL and .terraform to writable /tmp directory
    os.makedirs('/tmp/remediation', exist_ok=True)
    shutil.copy('/opt/remediation/remediation.tf', '/tmp/remediation/')
    shutil.copytree('/opt/remediation/.terraform', '/tmp/remediation/.terraform')
    
    # Write tfvars
    tfvars = {
        "bucket_name": bucket_name,
        "region": region
    }
    with open('/tmp/terraform.tfvars.json', 'w') as f:
        json.dump(tfvars, f)
    
    # Execute tofu apply
    result = subprocess.run(
        ['tofu', 'apply', '-refresh=false', '-auto-approve', 
         '-var-file=/tmp/terraform.tfvars.json',
         '-state=/tmp/remediation.tfstate',
         '-plugin-dir=/opt/providers'],
        cwd='/tmp/remediation',
        capture_output=True,
        text=True,
        timeout=40
    )
    
    if result.returncode == 0:
        return {
            "remediation_status": "success",
            "output": result.stdout
        }
    else:
        return {
            "remediation_status": "failed",
            "error": result.stderr
        }
```

**Design Rationale**:
- `-refresh=false`: Analyze já verificou estado atual, evita latência adicional
- State local efêmero: Remediação é idempotente, não precisa rastrear drift
- Providers pré-inicializados: Reduz cold start de ~10s para ~2s
- Container image: Permite incluir binário OpenTofu sem Lambda Layer

**Output Schema**:
```json
{
  "remediation_status": "success",
  "output": "aws_s3_bucket_public_access_block.remediation: Creation complete"
}
```

**Error Handling**:
- OpenTofu apply timeout: Return failure status with partial output
- IAM permission denied: Return failure, escalate to GitHub Issue
- Bucket not found: Return failure (bucket pode ter sido deletado)



### 5. Knowledge Base (DynamoDB)

**Responsibility**: Armazenar políticas, exceções aprovadas, e log de violações.

#### Table: controls

**Purpose**: Metadados de controles de conformidade.

**Schema**:
```json
{
  "TableName": "controls",
  "KeySchema": [
    {"AttributeName": "control_id", "KeyType": "HASH"}
  ],
  "AttributeDefinitions": [
    {"AttributeName": "control_id", "AttributeType": "S"}
  ],
  "BillingMode": "PAY_PER_REQUEST"
}
```

**Sample Item**:
```json
{
  "control_id": "ctrl-s3-public-block",
  "description": "Enforce S3 bucket public access block settings",
  "cloudtrail_events": ["PutBucketPublicAccessBlock"],
  "rego_policy_package": "s3_public_access",
  "resource_type": "AWS::S3::Bucket",
  "severity": "HIGH",
  "created_at": "2024-01-01T00:00:00Z"
}
```

**Access Pattern**:
- Query by control_id (primary key)
- Used by Analyze to retrieve policy metadata



#### Table: exceptions

**Purpose**: Armazenar exceções aprovadas com TTL nativo.

**Schema**:
```json
{
  "TableName": "exceptions",
  "KeySchema": [
    {"AttributeName": "exception_id", "KeyType": "HASH"}
  ],
  "AttributeDefinitions": [
    {"AttributeName": "exception_id", "AttributeType": "S"},
    {"AttributeName": "control_id", "AttributeType": "S"}
  ],
  "GlobalSecondaryIndexes": [
    {
      "IndexName": "control_id-index",
      "KeySchema": [
        {"AttributeName": "control_id", "KeyType": "HASH"}
      ],
      "Projection": {"ProjectionType": "ALL"}
    }
  ],
  "TimeToLiveSpecification": {
    "AttributeName": "valid_until",
    "Enabled": true
  },
  "BillingMode": "PAY_PER_REQUEST"
}
```

**Sample Item**:
```json
{
  "exception_id": "exc-a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "control_id": "ctrl-s3-public-block",
  "resource_arn": "arn:aws:s3:::public-website-bucket",
  "valid_until": 1735689600,
  "justification": "Public website hosting approved by security team",
  "approved_by": "security-lead@example.com",
  "created_at": "2024-01-15T10:00:00Z",
  "approval_ticket": "JIRA-12345"
}
```

**Access Pattern**:
- Query by control_id via GSI
- Filter by resource_arn and valid_until > now()
- TTL deletes expired items within 48h (behavior does NOT depend on physical deletion)

**Design Rationale**:
- TTL on valid_until: Automatic cleanup, but Analyze filters explicitly
- GSI on control_id: Efficient query for all exceptions of a control
- UUID partition key: Uniform distribution, no hot partitions



#### Table: violations_log

**Purpose**: Registro permanente de todas as avaliações de conformidade.

**Schema**:
```json
{
  "TableName": "violations_log",
  "KeySchema": [
    {"AttributeName": "violation_id", "KeyType": "HASH"}
  ],
  "AttributeDefinitions": [
    {"AttributeName": "violation_id", "AttributeType": "S"},
    {"AttributeName": "resource_arn", "AttributeType": "S"},
    {"AttributeName": "timestamp", "AttributeType": "S"}
  ],
  "GlobalSecondaryIndexes": [
    {
      "IndexName": "resource_arn-timestamp-index",
      "KeySchema": [
        {"AttributeName": "resource_arn", "KeyType": "HASH"},
        {"AttributeName": "timestamp", "KeyType": "RANGE"}
      ],
      "Projection": {"ProjectionType": "ALL"}
    }
  ],
  "BillingMode": "PAY_PER_REQUEST"
}
```

**Sample Item (S_violation with successful remediation)**:
```json
{
  "violation_id": "vio-550e8400-e29b-41d4-a716-446655440000",
  "resource_arn": "arn:aws:s3:::non-compliant-bucket",
  "control_id": "ctrl-s3-public-block",
  "state": "S_violation",
  "timestamp": "2024-01-15T10:30:15Z",
  "policy_version": "s3_public_access:v1.0",
  "exception_id": null,
  "remediation_status": "success",
  "remediation_timestamp": "2024-01-15T10:30:45Z",
  "github_issue_id": null,
  "account_id": "123456789012",
  "region": "us-east-1"
}
```

**Sample Item (S_exception)**:
```json
{
  "violation_id": "vio-660f9511-f3ac-52e5-b827-557766551111",
  "resource_arn": "arn:aws:s3:::public-website-bucket",
  "control_id": "ctrl-s3-public-block",
  "state": "S_exception",
  "timestamp": "2024-01-15T11:00:00Z",
  "policy_version": "s3_public_access:v1.0",
  "exception_id": "exc-a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "approved_by": "security-lead@example.com",
  "remediation_status": null,
  "github_issue_id": null,
  "account_id": "123456789012",
  "region": "us-east-1"
}
```

**Sample Item (S_violation with failed remediation)**:
```json
{
  "violation_id": "vio-770g0622-g4bd-63f6-c938-668877662222",
  "resource_arn": "arn:aws:s3:::protected-bucket",
  "control_id": "ctrl-s3-public-block",
  "state": "S_violation",
  "timestamp": "2024-01-15T12:00:00Z",
  "policy_version": "s3_public_access:v1.0",
  "exception_id": null,
  "remediation_status": "failed",
  "remediation_error": "AccessDenied: Insufficient permissions",
  "remediation_timestamp": "2024-01-15T12:00:30Z",
  "github_issue_id": "12345",
  "account_id": "123456789012",
  "region": "us-east-1"
}
```

**Access Pattern**:
- Query by violation_id (primary key) for audit trail
- Query by resource_arn + timestamp via GSI for resource history
- No TTL: Permanent audit log

**Design Rationale**:
- State field represents actual compliance state, not process state
- remediation_status indicates outcome of Execute, not a new state
- github_issue_id links violation to operational resolution workflow



### 6. Escalation (SQS FIFO + Lambda + GitHub Issues)

**Responsibility**: Notificar operadores humanos quando remediação automática falha.

#### SQS FIFO Queue

**Configuration**:
```json
{
  "QueueName": "compliance-escalation.fifo",
  "FifoQueue": true,
  "ContentBasedDeduplication": false,
  "MessageRetentionPeriod": 1209600,
  "VisibilityTimeout": 300,
  "DeduplicationScope": "messageGroup",
  "FifoThroughputLimit": "perMessageGroupId"
}
```

**Message Schema**:
```json
{
  "violation_id": "vio-770g0622-g4bd-63f6-c938-668877662222",
  "resource_arn": "arn:aws:s3:::protected-bucket",
  "control_id": "ctrl-s3-public-block",
  "error": "AccessDenied: Insufficient permissions",
  "timestamp": "2024-01-15T12:00:30Z",
  "step_functions_execution_arn": "arn:aws:states:us-east-1:123456789012:execution:compliance-loop:exec-abc123",
  "account_id": "123456789012",
  "region": "us-east-1"
}
```

**Design Rationale**:
- FIFO: Preserves order of violations for same resource
- MessageDeduplicationId = violation_id: Prevents duplicate escalations
- MessageGroupId = resource_arn: Parallel processing for different resources, ordered processing for same resource



#### Escalation Lambda

**Implementation**:
- Runtime: Python 3.12
- Trigger: SQS FIFO queue (batch size: 1)
- Timeout: 30 seconds
- Environment variables: GITHUB_TOKEN, GITHUB_REPO

**Algorithm**:
```python
import boto3
import requests
import json
import os

def lambda_handler(event, context):
    for record in event['Records']:
        message = json.loads(record['body'])
        
        # Create GitHub Issue
        issue = create_github_issue(message)
        
        # Update violations_log with github_issue_id
        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table('violations_log')
        table.update_item(
            Key={'violation_id': message['violation_id']},
            UpdateExpression='SET github_issue_id = :issue_id',
            ExpressionAttributeValues={':issue_id': str(issue['number'])}
        )
        
    return {'statusCode': 200}

def create_github_issue(message):
    url = f"https://api.github.com/repos/{os.environ['GITHUB_REPO']}/issues"
    headers = {
        'Authorization': f"token {os.environ['GITHUB_TOKEN']}",
        'Accept': 'application/vnd.github.v3+json'
    }
    
    execution_url = f"https://console.aws.amazon.com/states/home?region={message['region']}#/executions/details/{message['step_functions_execution_arn']}"
    
    body = f"""
## Compliance Violation - Remediation Failed

**Resource**: `{message['resource_arn']}`
**Control**: `{message['control_id']}`
**Violation ID**: `{message['violation_id']}`
**Timestamp**: {message['timestamp']}

### Error Details
```
{message['error']}
```

### Step Functions Execution
[View execution details]({execution_url})

### Action Required
Manual remediation required. Review the error details and apply the necessary configuration changes.
"""
    
    payload = {
        'title': f"[Compliance] {message['control_id']} - {message['resource_arn']}",
        'body': body,
        'labels': ['compliance-violation', 'auto-escalation']
    }
    
    response = requests.post(url, headers=headers, json=payload)
    response.raise_for_status()
    return response.json()
```

**GitHub Issue Example**:
```markdown
Title: [Compliance] ctrl-s3-public-block - arn:aws:s3:::protected-bucket

## Compliance Violation - Remediation Failed

**Resource**: `arn:aws:s3:::protected-bucket`
**Control**: `ctrl-s3-public-block`
**Violation ID**: `vio-770g0622-g4bd-63f6-c938-668877662222`
**Timestamp**: 2024-01-15T12:00:30Z

### Error Details
```
AccessDenied: Insufficient permissions
```

### Step Functions Execution
[View execution details](https://console.aws.amazon.com/states/...)

### Action Required
Manual remediation required. Review the error details and apply the necessary configuration changes.

Labels: compliance-violation, auto-escalation
```

**Error Handling**:
- GitHub API failure: Lambda retries via SQS visibility timeout
- DynamoDB update failure: Log error, continue (issue already created)
- Invalid message format: Send to DLQ



## Data Models

### Event Flow Data Model

```mermaid
erDiagram
    CloudTrailEvent ||--|| MonitorOutput : transforms
    MonitorOutput ||--|| AnalyzeInput : passes
    AnalyzeInput ||--|| AnalyzeOutput : produces
    AnalyzeOutput ||--o| ExecuteInput : triggers_if_violation
    ExecuteInput ||--|| ExecuteOutput : produces
    AnalyzeOutput ||--|| ViolationsLogEntry : creates
    ExecuteOutput ||--o| EscalationMessage : creates_if_failed
    EscalationMessage ||--o| GitHubIssue : creates
    
    CloudTrailEvent {
        string eventName
        string bucketName
        string accountId
        string region
        string eventTime
    }
    
    MonitorOutput {
        string resource_id
        string resource_arn
        string account_id
        string region
        string event_time
        string control_id
    }
    
    AnalyzeOutput {
        string state
        string violation_id
        string exception_id
        string approved_by
    }
    
    ExecuteOutput {
        string remediation_status
        string error
        string output
    }
    
    ViolationsLogEntry {
        string violation_id PK
        string resource_arn
        string control_id
        string state
        string timestamp
        string policy_version
        string exception_id
        string remediation_status
        string github_issue_id
    }
    
    EscalationMessage {
        string violation_id
        string resource_arn
        string control_id
        string error
        string timestamp
        string step_functions_execution_arn
    }
    
    GitHubIssue {
        int number
        string title
        string body
        array labels
    }
```



### DynamoDB Schema Relationships

```mermaid
erDiagram
    controls ||--o{ exceptions : "has_exceptions_for"
    controls ||--o{ violations_log : "generates_violations"
    exceptions ||--o{ violations_log : "referenced_by"
    violations_log ||--o| github_issues : "escalates_to"
    
    controls {
        string control_id PK
        string description
        array cloudtrail_events
        string rego_policy_package
        string resource_type
        string severity
        string created_at
    }
    
    exceptions {
        string exception_id PK
        string control_id GSI
        string resource_arn
        int valid_until TTL
        string justification
        string approved_by
        string created_at
        string approval_ticket
    }
    
    violations_log {
        string violation_id PK
        string resource_arn GSI
        string timestamp GSI_RANGE
        string control_id
        string state
        string policy_version
        string exception_id FK
        string remediation_status
        string remediation_error
        string remediation_timestamp
        string github_issue_id FK
        string account_id
        string region
    }
    
    github_issues {
        int number PK
        string title
        string body
        array labels
        string state
        string created_at
    }
```

### State Transition Model

```mermaid
stateDiagram-v2
    [*] --> ResourceMutation: CloudTrail Event
    ResourceMutation --> Analyzing: Monitor triggers
    Analyzing --> S_nominal: Compliant
    Analyzing --> S_exception: Valid Exception
    Analyzing --> S_violation: Non-compliant
    
    S_nominal --> [*]: Log only
    S_exception --> [*]: Log only
    S_violation --> Remediating: Execute invoked
    
    Remediating --> Remediated: Success
    Remediating --> RemediationFailed: Failure
    
    Remediated --> [*]: Log success
    RemediationFailed --> Escalating: Enqueue
    Escalating --> IssueCreated: GitHub API
    IssueCreated --> [*]: Log issue_id
```


## Correctness Properties

A property is a characteristic or behavior that should hold true across all valid executions of a system—essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.

### Property 1: CloudTrail Event Parsing Completeness

For any valid CloudTrail PutBucketPublicAccessBlock event, the Monitor SHALL successfully extract all required metadata fields (bucket ARN, account ID, region) and construct a valid output schema.

**Validates: Requirements 1.2, 1.3, 1.4, 1.5, 15.1, 15.7**

### Property 2: Orchestrator State-Based Routing

For any Analyze output with state S_nominal or S_exception, the Orchestrator SHALL terminate the workflow without invoking Execute; for any Analyze output with state S_violation, the Orchestrator SHALL invoke Execute.

**Validates: Requirements 2.2, 2.3, 2.4**

### Property 3: Analyze Classification Determinism

For any resource configuration and exception set, when Analyze evaluates the same inputs multiple times with the same timestamp, it SHALL return identical classification results.

**Validates: Requirements 3.9, 16.3**

### Property 4: Compliant Resource Classification

For any S3 bucket configuration where BlockPublicAcls, IgnorePublicAcls, BlockPublicPolicy, and RestrictPublicBuckets are all true, the Analyze SHALL return S_nominal.

**Validates: Requirements 3.3, 10.7**

### Property 5: Valid Exception Classification

For any non-compliant resource configuration with an exception where valid_until > current_timestamp, the Analyze SHALL return S_exception.

**Validates: Requirements 3.4, 3.7**

### Property 6: Violation Classification

For any non-compliant resource configuration without a valid exception, the Analyze SHALL return S_violation.

**Validates: Requirements 3.5, 3.8**

### Property 7: S3 Public Access Policy Evaluation

For any S3 bucket configuration, the s3_public_access Rego policy SHALL return violation if any of the four public access block settings (BlockPublicAcls, IgnorePublicAcls, BlockPublicPolicy, RestrictPublicBuckets) is false.

**Validates: Requirements 10.2, 10.3, 10.4, 10.5, 10.6**

### Property 8: Exception Query Correctness

For any control_id and resource_arn, the Analyze SHALL query the exceptions table using the control_id GSI and filter by resource_arn match and valid_until > current_timestamp.

**Validates: Requirements 3.6, 5.8**

### Property 9: Exception Data Model Completeness

For any exception created in the Knowledge_Base, it SHALL contain all required fields: exception_id (UUID), control_id, resource_arn, valid_until (Unix timestamp), justification, approved_by, and created_at.

**Validates: Requirements 5.4, 5.5, 5.6, 5.10, 5.11, 5.12**

### Property 10: Exception Filtering Independence from TTL

For any exception query, the Analyze SHALL filter by valid_until > now() in the query expression, ensuring behavior does not depend on DynamoDB TTL physical deletion.

**Validates: Requirements 5.9**

### Property 11: Violations Log Entry Uniqueness

For any Analyze evaluation, exactly one entry SHALL be created in the violations_log table with a unique violation_id (UUID).

**Validates: Requirements 6.1, 6.2**

### Property 12: Violations Log Data Model Completeness

For any violations_log entry, it SHALL contain all required fields: violation_id, resource_arn, control_id, state (one of S_nominal, S_exception, S_violation), timestamp (ISO 8601), and policy_version.

**Validates: Requirements 6.3, 6.4, 6.5, 6.6, 6.7**

### Property 13: Exception Reference in Log

For any violations_log entry with state S_exception, it SHALL include a non-null exception_id reference.

**Validates: Requirements 6.8**

### Property 14: Remediation Status Logging

For any violations_log entry where Execute was invoked, it SHALL include a remediation_status field with value "success" if Execute succeeded or "failed" if Execute failed.

**Validates: Requirements 6.9, 6.10, 6.11**

### Property 15: Execute Idempotency

For any resource in S_violation state, invoking Execute multiple times SHALL produce the same final resource configuration (all four public access block settings true).

**Validates: Requirements 7.7, 9.1, 11.5**

### Property 16: Execute OpenTofu Invocation

For any S_violation input, Execute SHALL invoke OpenTofu with the -refresh=false flag and the resource_id correctly mapped to OpenTofu variables.

**Validates: Requirements 7.1, 7.2, 7.3**

### Property 17: Execute Success Response

For any successful OpenTofu apply (return code 0), Execute SHALL return remediation_status "success" to the Orchestrator.

**Validates: Requirements 7.5**

### Property 18: Execute Failure Response

For any failed OpenTofu apply (return code != 0), Execute SHALL return remediation_status "failed" with error details to the Orchestrator.

**Validates: Requirements 7.6**

### Property 19: Remediation Outcome Compliance

For any resource after successful Execute, querying the S3 public access block configuration SHALL show BlockPublicAcls, IgnorePublicAcls, BlockPublicPolicy, and RestrictPublicBuckets all set to true.

**Validates: Requirements 7.9**

### Property 20: Escalation Trigger on Failure

For any Execute failure, the Orchestrator SHALL send a message to the Escalation_Queue containing all required fields: violation_id, resource_arn, control_id, error details, timestamp, and step_functions_execution_arn.

**Validates: Requirements 2.6, 8.1, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8**

### Property 21: GitHub Issue Creation

For any message in the Escalation_Queue, the Escalation Lambda SHALL create a GitHub Issue containing all escalation message details and labeled with "compliance-violation" and "auto-escalation".

**Validates: Requirements 8.9, 8.10, 8.11**

### Property 22: GitHub Issue ID Persistence

For any GitHub Issue created for a failed remediation, the issue number returned by the GitHub API SHALL be saved to the violations_log entry corresponding to the violation_id.

**Validates: Requirements 8.12, 19.1, 19.4**

### Property 23: GitHub Issue ID Null for Non-Escalated Violations

For any violations_log entry where Execute was not invoked or Execute succeeded, the github_issue_id field SHALL be null.

**Validates: Requirements 19.5**

### Property 24: State Persistence After Failed Remediation

For any Execute failure, the violations_log entry SHALL maintain state as S_violation (not create a new state like S_remediation_failed), with the remediation_status field indicating "failed".

**Validates: Requirements 18.1, 18.2, 18.5**

### Property 25: EventBridge Rule Event Filtering

For any CloudTrail event with eventName != "PutBucketPublicAccessBlock", the EventBridge Rule SHALL NOT match the event and SHALL NOT trigger the Orchestrator.

**Validates: Requirements 1.6**

### Property 26: Monitor Error Handling

For any CloudTrail event with missing required fields (bucketName, accountId, or region), the Monitor SHALL log an error and discard the event.

**Validates: Requirements 15.6**

### Property 27: ARN Construction Format

For any bucket name extracted from a CloudTrail event, the Monitor SHALL construct the resource ARN in the format "arn:aws:s3:::bucket-name".

**Validates: Requirements 15.7**

### Property 28: Timeout Error Handling

For any component timeout (Analyze > 15s, Execute > 45s, Orchestrator > 60s), the Orchestrator SHALL log a timeout error and terminate the workflow.

**Validates: Requirements 17.4**

### Property 29: Resource State Unchanged for S_exception

For any resource classified as S_exception, the resource configuration SHALL remain unchanged after the workflow completes.

**Validates: Requirements 12.5**

### Property 30: Resource State Unchanged After Failed Remediation

For any resource where Execute fails, the resource configuration SHALL remain in its non-compliant state.

**Validates: Requirements 14.5**


## Error Handling

### Monitor Error Handling

**Malformed CloudTrail Events**:
- Missing required fields (bucketName, accountId, region): Log error to CloudWatch Logs, discard event
- Invalid JSON structure: EventBridge DLQ captures failed invocations
- Unmatched event pattern: EventBridge filters prevent invocation

**Error Recovery**: No retry - events are discarded. CloudTrail will generate new events for subsequent mutations.

### Analyze Error Handling

**AWS API Failures**:
- S3 GetPublicAccessBlock fails (NoSuchBucket, AccessDenied): Return error to Step Functions, trigger escalation
- DynamoDB query timeout: Proceed without exception data (fail-secure: treat as no exception)
- DynamoDB write failure: Retry with exponential backoff (Lambda default), escalate if persistent

**OPA Evaluation Errors**:
- Policy file missing: Lambda initialization fails, CloudWatch alarm triggers
- Rego syntax error: Lambda initialization fails, CloudWatch alarm triggers
- OPA runtime error: Log to CloudWatch, return S_violation (fail-secure)

**Error Recovery**: Step Functions retry policy with exponential backoff (3 attempts, 2s initial interval, 2x multiplier). After exhaustion, trigger escalation.

### Execute Error Handling

**OpenTofu Failures**:
- IAM permission denied: Return failure status with error details, escalate to GitHub Issue
- Bucket not found: Return failure status (bucket may have been deleted), escalate
- OpenTofu timeout (>40s): Kill process, return failure status, escalate
- State file corruption: Use fresh ephemeral state (no impact due to declarative config)

**Container Failures**:
- OOM (>1024MB): Lambda fails, Step Functions retries, escalate if persistent
- Disk full (>/tmp 1024MB): Clean /tmp before execution, fail if insufficient space

**Error Recovery**: Step Functions retry policy (2 attempts, 5s interval). After exhaustion, send to Escalation_Queue.

### Orchestrator Error Handling

**State Machine Errors**:
- Lambda invocation failure: Retry per Lambda error handling policy
- DynamoDB SDK integration failure: Retry with exponential backoff (3 attempts)
- SQS SendMessage failure: Retry with exponential backoff (3 attempts)
- Global timeout (60s): Terminate workflow, log timeout error, send to Escalation_Queue

**Error Recovery**: Built-in Step Functions retry and catch mechanisms. All unhandled errors result in escalation.

### Escalation Error Handling

**SQS Consumer Failures**:
- GitHub API rate limit: Lambda retries via SQS visibility timeout (300s), exponential backoff
- GitHub API authentication failure: Send to DLQ, trigger CloudWatch alarm
- Network timeout: Lambda retries via SQS visibility timeout

**DynamoDB Update Failures**:
- Update violations_log with github_issue_id fails: Log error to CloudWatch, continue (issue already created)
- Conditional check failure: Log warning, continue (idempotent operation)

**Error Recovery**: SQS visibility timeout triggers automatic retry. After 3 receive attempts, message moves to DLQ. CloudWatch alarm notifies operators.

### Fail-Secure Principles

- **Unknown exception state**: Treat as no exception (classify as S_violation if non-compliant)
- **OPA evaluation error**: Classify as S_violation (enforce remediation)
- **Timeout in Analyze**: Escalate (do not assume compliant)
- **Timeout in Execute**: Escalate (do not assume remediated)


## Testing Strategy

### Dual Testing Approach

The MVP Compliance Automation Loop requires both unit testing and property-based testing for comprehensive coverage:

- **Unit tests**: Verify specific examples, edge cases, error conditions, and integration points
- **Property tests**: Verify universal properties across all inputs through randomization

Both approaches are complementary and necessary. Unit tests catch concrete bugs in specific scenarios, while property tests verify general correctness across the input space.

### Unit Testing Strategy

**Focus Areas**:
- Specific examples demonstrating correct behavior (e.g., C1-C4 scenarios from requirements)
- Edge cases (empty events, expired exceptions, already-compliant resources)
- Error conditions (missing fields, API failures, timeouts)
- Integration points between components (Monitor→Orchestrator, Analyze→DynamoDB, Execute→S3)

**Avoid Over-Testing**:
- Do not write exhaustive unit tests for all input combinations
- Property-based tests handle comprehensive input coverage
- Unit tests should focus on concrete, meaningful scenarios

**Test Organization**:
```
tests/
├── unit/
│   ├── test_monitor.py          # CloudTrail parsing, ARN construction
│   ├── test_analyze.py           # Classification logic, OPA integration
│   ├── test_execute.py           # OpenTofu invocation, idempotency
│   ├── test_escalation.py        # GitHub issue creation, DynamoDB updates
│   └── test_orchestrator.py     # State machine definition validation
├── integration/
│   ├── test_end_to_end.py        # C1-C4 scenarios
│   └── test_error_paths.py       # Failure scenarios, escalation flow
└── property/
    ├── test_monitor_properties.py
    ├── test_analyze_properties.py
    ├── test_execute_properties.py
    └── test_escalation_properties.py
```

### Property-Based Testing Strategy

**Library Selection**:
- Python: Hypothesis (https://hypothesis.readthedocs.io/)
- Configuration: Minimum 100 iterations per property test (due to randomization)

**Property Test Requirements**:
- Each correctness property from the design document MUST be implemented by a SINGLE property-based test
- Each test MUST be tagged with a comment referencing the design property
- Tag format: `# Feature: mvp-compliance-automation-loop, Property {number}: {property_text}`

**Generator Strategy**:

For Monitor properties:
- Generate random CloudTrail events with valid and invalid structures
- Generate random bucket names, account IDs, regions
- Include edge cases: empty strings, special characters, very long names

For Analyze properties:
- Generate random S3 public access block configurations (all combinations of 4 boolean settings)
- Generate random exceptions with various valid_until timestamps (past, present, future)
- Generate random resource ARNs and control IDs
- Include edge cases: boundary timestamps, null fields, malformed UUIDs

For Execute properties:
- Generate random bucket names and regions
- Generate random OpenTofu outputs (success and failure scenarios)
- Include edge cases: already-compliant resources, non-existent buckets

For Escalation properties:
- Generate random escalation messages with various error details
- Generate random GitHub API responses
- Include edge cases: rate limits, authentication failures

**Example Property Test**:
```python
from hypothesis import given, strategies as st, assume
import hypothesis

# Feature: mvp-compliance-automation-loop, Property 4: Compliant Resource Classification
@given(
    block_public_acls=st.booleans(),
    ignore_public_acls=st.booleans(),
    block_public_policy=st.booleans(),
    restrict_public_buckets=st.booleans()
)
@hypothesis.settings(max_examples=100)
def test_compliant_resource_classification(
    block_public_acls, ignore_public_acls, block_public_policy, restrict_public_buckets
):
    """For any S3 bucket configuration where all four public access block settings are true,
    the Analyze SHALL return S_nominal."""
    
    # Only test cases where all four settings are true
    assume(block_public_acls and ignore_public_acls and block_public_policy and restrict_public_buckets)
    
    config = {
        'BlockPublicAcls': block_public_acls,
        'IgnorePublicAcls': ignore_public_acls,
        'BlockPublicPolicy': block_public_policy,
        'RestrictPublicBuckets': restrict_public_buckets
    }
    
    result = analyze_compliance(config, exceptions=[])
    
    assert result['state'] == 'S_nominal'
```

**Note on Property 4 Test Efficiency**: For Property 4 specifically, where all four booleans must be True by definition, using `assume()` with `st.booleans()` will discard 15 of every 16 generated cases (6.25% efficiency). Hypothesis may warn about `HealthCheck.filter_too_much`. For this property, using `st.just(True)` for all four fields is actually more appropriate, as the relevant variability for testing lies in other input fields (exceptions, timestamps, resource ARNs) rather than in the boolean settings themselves. The `assume()` pattern is better suited for properties where the condition represents a constraint over a larger input space, not where the valid space is a single point.

**Property Test Coverage**:
- All 30 correctness properties MUST have corresponding property-based tests
- Each test MUST run minimum 100 iterations
- Each test MUST include edge case generators (empty, null, boundary values)
- Each test MUST be tagged with the property reference

### Integration Testing Strategy

**End-to-End Scenarios** (C1-C4 from requirements):

**C1: Automatic Remediation**:
1. Create S3 bucket with public access enabled
2. Trigger CloudTrail event
3. Verify Analyze classifies as S_violation
4. Verify Execute remediates to compliant state
5. Verify violations_log contains entry with remediation_status "success"
6. Verify bucket has all four public access blocks enabled

**C2: Valid Exception**:
1. Create S3 bucket with public access enabled
2. Create exception with valid_until in future
3. Trigger CloudTrail event
4. Verify Analyze classifies as S_exception
5. Verify Execute is not invoked
6. Verify violations_log contains entry with state S_exception and exception_id
7. Verify bucket configuration remains unchanged

**C3: Expired Exception**:
1. Create S3 bucket with public access enabled
2. Create exception with valid_until in past
3. Trigger CloudTrail event
4. Verify Analyze classifies as S_violation
5. Verify Execute remediates to compliant state
6. Verify violations_log contains entry with state S_violation and remediation_status "success"

**C4: Failed Remediation**:
1. Create S3 bucket with public access enabled
2. Remove IAM permissions for Execute Lambda
3. Trigger CloudTrail event
4. Verify Analyze classifies as S_violation
5. Verify Execute fails with permission error
6. Verify message sent to Escalation_Queue
7. Verify GitHub Issue created with error details
8. Verify violations_log contains entry with state S_violation, remediation_status "failed", and github_issue_id
9. Verify bucket configuration remains non-compliant

**Test Environment**:
- LocalStack for AWS service mocking (S3, DynamoDB, SQS, Step Functions)
- GitHub API mocking via responses library
- OpenTofu execution in test mode (tofu plan -detailed-exitcode)

### Performance Testing Strategy

**Latency Requirements**:
- Monitor to Orchestrator: <5 seconds (requirement 1.1)
- Analyze execution: <15 seconds (requirement 17.1)
- Execute execution: <45 seconds (requirement 17.2)
- End-to-end loop: <60 seconds (requirement 2.7)

**Performance Tests**:
- Measure cold start latency for Lambda functions
- Measure OpenTofu apply duration for various bucket configurations
- Measure DynamoDB query latency for exceptions table
- Measure Step Functions execution duration for all state paths

**Load Testing**:
- Simulate 100 concurrent CloudTrail events
- Verify no throttling on DynamoDB, Lambda, or Step Functions
- Verify SQS FIFO queue handles burst traffic
- Verify GitHub API rate limits are respected

### Test Data Management

**Fixtures**:
- Sample CloudTrail events (valid, invalid, edge cases)
- Sample S3 configurations (compliant, non-compliant, all combinations)
- Sample exceptions (valid, expired, various timestamps)
- Sample OpenTofu outputs (success, failure, various errors)

**Test Data Generation**:
- Use Hypothesis strategies for property-based tests
- Use factory pattern for unit test fixtures
- Use LocalStack for integration test data

**Test Data Cleanup**:
- Tear down LocalStack resources after each test
- Clear DynamoDB tables between tests
- Reset GitHub API mocks between tests

