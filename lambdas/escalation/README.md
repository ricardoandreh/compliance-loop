# Escalation Lambda

Cria GitHub Issues para violações que falharam na remediação automática.

## Arquitetura

- **Runtime**: Python 3.14 (arm64)
- **Trigger**: SQS FIFO queue (batch size: 1)
- **Deployment**: `escalation.zip` (~1.2KB)

## Fluxo

1. Recebe mensagem da SQS FIFO (violation details)
2. Busca credenciais GitHub do SSM Parameter Store
3. Cria GitHub Issue com detalhes da violação
4. Atualiza `violations_log` com `github_issue_id`

## Build e Deploy

```bash
zip -j escalation.zip handler.py
aws lambda update-function-code --function-name compliance-loop-escalation-dev \
  --zip-file fileb://escalation.zip --region us-east-1
```
