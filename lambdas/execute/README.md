# Execute Lambda

Remedia recursos em violação usando OpenTofu com templates HCL declarativos.

## Arquitetura

- **Runtime**: Python 3.13 (arm64)
- **Layer**: OpenTofu binary (`/opt/bin/tofu`, ~29MB)
- **Provider cache**: S3 (`provider-cache/tf-dot-terraform.tar.gz`, ~128MB), baixado no cold start
- **Deployment**: `execute.zip` (~1.3KB)
- **Ephemeral storage**: 1024MB (provider extraído + tfstate)
- **Memory**: 2048MB

## Fluxo

1. Cold start: baixa e extrai provider cache do S3 para `/tmp`
2. Baixa `remediation.tf` do S3 (cacheado em `/tmp` entre invocações)
3. Cria symlink para provider pré-inicializado
4. Executa `tofu apply -refresh=false -auto-approve`
5. Retorna `{remediation_status, output/error}`

## Build e Deploy

```bash
zip -j execute.zip handler.py
aws lambda update-function-code --function-name compliance-loop-execute-dev \
  --zip-file fileb://execute.zip --region us-east-1
```

## Layer OpenTofu

```bash
# Rebuild (se atualizar versão do tofu):
mkdir -p /tmp/tofu_layer/bin
cp tofu /tmp/tofu_layer/bin/tofu
cd /tmp/tofu_layer && zip -r tofu_layer.zip bin/
```

## Provider Cache

```bash
# Rebuild (se atualizar versão do provider AWS):
# Usar Dockerfile auxiliar para gerar .terraform/ em arm64
docker buildx build --platform linux/arm64 ...
tar czf tf-dot-terraform.tar.gz tf-dot-terraform tf-lock.hcl
aws s3 cp tf-dot-terraform.tar.gz s3://compliance-mvp-terraform-state/provider-cache/
```

## Arquivo auxiliar

- `provider.tf`: declaração do provider AWS (usado apenas para gerar o cache)
