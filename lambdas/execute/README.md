# Execute Lambda

Remedia recursos em violação usando OpenTofu com templates HCL declarativos.

## Arquitetura

- **Runtime**: Python 3.14 (arm64)
- **Layer**: OpenTofu 1.12.6 (`/opt/bin/tofu`, ~29MB), gerada por `scripts/build_execute.sh`
- **Provider cache**: S3 (`provider-cache/tf-dot-terraform.tar.gz`, ~156MB, provider AWS 6.66.0), baixado no cold start
- **Deployment**: `execute.zip` (~4KB)
- **Ephemeral storage**: 2048MB — o provider extraído tem 780MB, e durante a extração o arquivo de 156MB ainda está no `/tmp`
- **Memory**: 2048MB

## Fluxo

1. Cold start: baixa e extrai provider cache do S3 para `/tmp`
2. Baixa `remediation.tf` do S3 (cacheado em `/tmp` entre invocações)
3. Cria symlink para provider pré-inicializado
4. Executa `tofu apply -refresh=false -auto-approve`
5. Retorna `{remediation_status, output/error}`

## Build e Deploy

```bash
bash scripts/build_execute.sh   # baixa o OpenTofu fixado, confere o SHA256SUMS, gera tofu_layer.zip e execute.zip
# o apply publica a layer e o handler
```

Para trocar a versão do OpenTofu: `TOFU_VERSION=x.y.z bash scripts/build_execute.sh`,
e fixe a nova versão no script.

## Provider Cache

O pacote oficial `linux_arm64` do provider é baixado e extraído na estrutura de
um `.terraform/` já inicializado — não é preciso emular arm64:

```bash
mkdir gen && cd gen
cat > provider.tf <<'TF'
terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}
TF
TOFU="docker run --rm -u $(id -u):$(id -g) -v $PWD:/w -w /w ghcr.io/opentofu/opentofu:1.12.6"
$TOFU providers lock -platform=linux_arm64 -platform=linux_amd64   # lock com os hashes do arm64
$TOFU providers mirror -platform=linux_arm64 /w/mirror              # pacote assinado
V=6.66.0; D=build/tf-dot-terraform/providers/registry.opentofu.org/hashicorp/aws/$V
mkdir -p $D/linux_arm64 && : > $D/linux_arm64.lock
unzip -q mirror/registry.opentofu.org/hashicorp/aws/terraform-provider-aws_${V}_linux_arm64.zip -d $D/linux_arm64
echo '["/opt/providers"]' > build/tf-dot-terraform/plugin_path
cp .terraform.lock.hcl build/tf-lock.hcl
(cd build && tar czf ../tf-dot-terraform.tar.gz tf-dot-terraform tf-lock.hcl)
aws s3 cp tf-dot-terraform.tar.gz s3://compliance-mvp-terraform-state/provider-cache/
```

A restrição do lock (`~> 6.0`) precisa aceitar a dos `controls/*/remediation.tf`.
O cache anterior (provider 5.100.0, usado nas séries publicadas) ficou em
`provider-cache/tf-dot-terraform-aws-5.100.0.tar.gz`.

## Arquivo auxiliar

- `provider.tf`: declaração do provider AWS (usado apenas para gerar o cache)
