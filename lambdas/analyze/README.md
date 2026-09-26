# Analyze Lambda

Classifica o estado de conformidade de recursos AWS usando OPA/Rego.

## Arquitetura

- **Runtime**: Python 3.14 (arm64)
- **Layer**: OPA binary (`/opt/bin/opa`, ~20MB)
- **Deployment**: `analyze.zip` (~2.5KB)
- **Permissões**: Leitura de recursos AWS (ABAC) + Query na tabela `exceptions`

## Fluxo

1. Recebe `controls` (items completos da tabela) da Step Function
2. Para cada controle, usa `fetch_config` para buscar configuração do recurso via boto3
3. Avalia conformidade com OPA/Rego (policy baixada do S3)
4. Consulta exceções na tabela `exceptions`
5. Classifica: `S_nominal`, `S_exception`, ou `S_violation`

## Build e Deploy

```bash
zip -j analyze.zip handler.py
aws lambda update-function-code --function-name compliance-loop-analyze-dev \
  --zip-file fileb://analyze.zip --region us-east-1
```

## Layer OPA

```bash
# Rebuild (se atualizar versão do OPA):
mkdir -p /tmp/opa_layer/bin
cp opa /tmp/opa_layer/bin/opa
cd /tmp/opa_layer && zip -r opa_layer.zip bin/
```
