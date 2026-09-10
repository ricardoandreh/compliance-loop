# Exception Scheduler Sync Lambda

Cria EventBridge Schedulers para re-avaliação automática após expiração de exceções.

## Arquitetura

- **Runtime**: Python 3.13 (arm64)
- **Trigger**: DynamoDB Stream (tabela `exceptions`, INSERT/MODIFY)
- **Deployment**: `handler.zip` (~1.4KB)

## Fluxo

1. DynamoDB Stream emite INSERT/MODIFY na tabela `exceptions`
2. Lambda lê o item atual da tabela
3. Se `status=active` e `valid_until > now`: cria Scheduler one-time para `valid_until + 30s`
4. Se `status=revoked`: deleta Scheduler e dispara re-avaliação imediata
5. Scheduler dispara Step Function com o `control_id` da exceção — o estado `RouteControlLookup` resolve o controle exato via `GetControlById`, sem re-avaliar controles alheios
6. Scheduler se auto-deleta após disparo (`ActionAfterCompletion=DELETE`)

## Build e Deploy

```bash
zip -j handler.zip handler.py
aws lambda update-function-code --function-name compliance-loop-exception-scheduler-sync-dev \
  --zip-file fileb://handler.zip --region us-east-1
```
