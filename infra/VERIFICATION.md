# DynamoDB Infrastructure Verification

## Task 2: Implementar infraestrutura DynamoDB com OpenTofu

### Status: ✅ COMPLETED

All three DynamoDB tables have been successfully implemented with OpenTofu configuration files in the `infra/` directory.

---

## Subtask 2.1: Criar tabela controls com schema completo ✅

**File**: `infra/dynamodb_controls.tf`

**Configuration**:
- ✅ Partition key: `control_id` (String)
- ✅ Billing mode: `PAY_PER_REQUEST`
- ✅ Attributes supported (non-key attributes don't need to be declared in DynamoDB):
  - `description`
  - `cloudtrail_events`
  - `rego_policy_package`
  - `resource_type`
  - `severity`
  - `created_at`

**Requirements Validated**: 5.1

**Notes**: 
- DynamoDB is schema-less for non-key attributes, so only the partition key needs to be declared
- All other attributes can be added when items are inserted
- Table uses PAY_PER_REQUEST billing for cost optimization

---

## Subtask 2.2: Criar tabela exceptions com GSI e TTL ✅

**File**: `infra/dynamodb_exceptions.tf`

**Configuration**:
- ✅ Partition key: `exception_id` (String)
- ✅ GSI: `control_id-index` with `control_id` as hash key
- ✅ GSI projection type: `ALL` (includes all attributes)
- ✅ TTL attribute: `valid_until` (enabled)
- ✅ Billing mode: `PAY_PER_REQUEST`
- ✅ Attributes supported:
  - `resource_arn`
  - `justification`
  - `approved_by`
  - `created_at`
  - `approval_ticket`

**Requirements Validated**: 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.10, 5.11, 5.12

**Notes**:
- GSI allows efficient querying of exceptions by control_id
- TTL automatically expires items based on `valid_until` timestamp
- Analyze Lambda will filter by `valid_until > now()` in queries (doesn't depend on physical deletion)

---

## Subtask 2.3: Criar tabela violations_log com GSI ✅

**File**: `infra/dynamodb_violations_log.tf`

**Configuration**:
- ✅ Partition key: `violation_id` (String)
- ✅ GSI: `resource_arn-timestamp-index`
  - Hash key: `resource_arn`
  - Range key: `timestamp`
- ✅ GSI projection type: `ALL`
- ✅ Billing mode: `PAY_PER_REQUEST`
- ✅ Attributes supported:
  - `control_id`
  - `state`
  - `policy_version`
  - `exception_id`
  - `remediation_status`
  - `remediation_error`
  - `remediation_timestamp`
  - `github_issue_id`
  - `account_id`
  - `region`

**Requirements Validated**: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 6.9, 6.10, 6.11

**Notes**:
- GSI enables querying violation history by resource ARN and time range
- No TTL configured - this is a permanent audit log
- Supports all fields required for compliance tracking and escalation

---

## OpenTofu Validation Results

### Initialization ✅
```bash
docker run --rm -v "${PWD}/infra:/workspace" -w /workspace ghcr.io/opentofu/opentofu:latest init
```
**Result**: SUCCESS - Providers downloaded and initialized

### Validation ✅
```bash
docker run --rm -v "${PWD}/infra:/workspace" -w /workspace ghcr.io/opentofu/opentofu:latest validate
```
**Result**: SUCCESS - Configuration is valid

### Formatting ✅
```bash
docker run --rm -v "${PWD}/infra:/workspace" -w /workspace ghcr.io/opentofu/opentofu:latest fmt
```
**Result**: SUCCESS - All files properly formatted

---

## Design Document Compliance

### Table Schemas Match Design ✅

All three tables match the schemas defined in the design document:

1. **controls table**: Simple table with control_id partition key
2. **exceptions table**: Includes GSI on control_id and TTL on valid_until
3. **violations_log table**: Includes composite GSI on resource_arn (hash) and timestamp (range)

### Billing Mode ✅

All tables use `PAY_PER_REQUEST` billing mode as specified in the design document.

### Attribute Definitions ✅

Only key attributes (partition keys, sort keys, and GSI keys) are defined in the schema, which is correct for DynamoDB. Non-key attributes are added when items are inserted.

---

## Usage Instructions

### Deploy Infrastructure

Using Docker (recommended - no local OpenTofu installation required):

```bash
# Initialize
docker run --rm \
  -v "${PWD}/infra:/workspace" \
  -w /workspace \
  -e AWS_ACCESS_KEY_ID \
  -e AWS_SECRET_ACCESS_KEY \
  -e AWS_SESSION_TOKEN \
  -e AWS_REGION \
  ghcr.io/opentofu/opentofu:latest init

# Plan
docker run --rm \
  -v "${PWD}/infra:/workspace" \
  -w /workspace \
  -e AWS_ACCESS_KEY_ID \
  -e AWS_SECRET_ACCESS_KEY \
  -e AWS_SESSION_TOKEN \
  -e AWS_REGION \
  ghcr.io/opentofu/opentofu:latest plan

# Apply
docker run --rm \
  -v "${PWD}/infra:/workspace" \
  -w /workspace \
  -e AWS_ACCESS_KEY_ID \
  -e AWS_SECRET_ACCESS_KEY \
  -e AWS_SESSION_TOKEN \
  -e AWS_REGION \
  ghcr.io/opentofu/opentofu:latest apply -auto-approve
```

### Custom Variables

Override default variables:

```bash
docker run --rm \
  -v "${PWD}/infra:/workspace" \
  -w /workspace \
  -e AWS_ACCESS_KEY_ID \
  -e AWS_SECRET_ACCESS_KEY \
  -e AWS_SESSION_TOKEN \
  -e AWS_REGION \
  ghcr.io/opentofu/opentofu:latest apply \
  -var="region=us-west-2" \
  -var="environment=prod" \
  -var="project_name=my-compliance-loop"
```

Or create `infra/terraform.tfvars`:

```hcl
region       = "us-west-2"
environment  = "prod"
project_name = "my-compliance-loop"
```

---

## Next Steps

With the DynamoDB infrastructure complete, the next tasks in the implementation plan are:

- **Task 3**: Implement monitoring infrastructure (CloudTrail, EventBridge)
- **Task 4**: Implement Rego policy for S3 public access
- **Task 5**: Implement Analyze Lambda
- **Task 6**: Implement Execute Lambda with container image

---

## References

- Design Document: `.kiro/specs/mvp-compliance-automation-loop/design.md`
- Requirements Document: `.kiro/specs/mvp-compliance-automation-loop/requirements.md`
- Tasks Document: `.kiro/specs/mvp-compliance-automation-loop/tasks.md`
- OpenTofu Docker Documentation: https://opentofu.org/docs/intro/install/docker/
