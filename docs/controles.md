# Controles

Um controle é um diretório em `controls/` com três arquivos: `control.json`
(a definição, os eventos observados, a leitura do recurso, a severidade e a
correspondência com o NIST SP 800-53 e a ISO/IEC 27002), `policy.rego` (a
política que avalia a leitura) e `remediation.tf` (o que a corrige). Um controle
detectivo não tem `remediation.tf`.

## Controles implementados

| Control ID | Serviço | Política | Remediação |
|---|---|---|---|
| `ctrl-s3-public-block` | S3 | Todos os 4 public access block settings habilitados | `aws_s3_bucket_public_access_block` |
| `ctrl-s3-ssl-only` | S3 | Bucket policy com deny para non-SSL requests | `aws_s3_bucket_policy` (merge) |
| `ctrl-s3-versioning` | S3 | Versioning habilitado | `aws_s3_bucket_versioning` |
| `ctrl-s3-encryption` | S3 | Server-side encryption configurada | `aws_s3_bucket_server_side_encryption_configuration` |
| `ctrl-ebs-encryption-default` | EC2 | EBS encryption by default habilitado | `aws_ebs_encryption_by_default` |
| `ctrl-ebs-volume-encrypted` | EC2 | Volume EBS criptografado | **detectivo**: escala, não remedia (L14) |

## Acrescentar um controle

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
