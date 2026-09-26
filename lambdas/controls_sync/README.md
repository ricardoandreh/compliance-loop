# Controls Sync Lambda

Atualiza o EventBridge rule dinamicamente quando controles são adicionados/removidos.

## Arquitetura

- **Runtime**: Python 3.14 (arm64)
- **Trigger**: DynamoDB Stream (tabela `controls`)
- **Deployment**: `handler.zip` (~0.5KB)

## Fluxo

1. DynamoDB Stream emite INSERT/MODIFY/DELETE na tabela `controls`
2. Lambda faz scan completo da tabela para coletar todos os `cloudtrail_events`
3. Chama `events:PutRule` com o event pattern atualizado (idempotente)

## Build e Deploy

```bash
zip -j handler.zip handler.py
aws lambda update-function-code --function-name compliance-loop-controls-sync-dev \
  --zip-file fileb://handler.zip --region us-east-1
```
