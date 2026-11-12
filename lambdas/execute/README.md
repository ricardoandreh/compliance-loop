# Execute Lambda

Remedia recursos em violação usando OpenTofu com templates HCL declarativos.

## Arquitetura

- **Runtime**: Python 3.14 (arm64)
- **Layer**: OpenTofu 1.12.6 (`/opt/bin/tofu`, ~29MB), gerada por `scripts/build_execute.sh`
- **Provider cache**: S3 (`provider-cache/tf-dot-terraform.tar.zst`, ~123MB, provider AWS 6.66.0), baixado e descompactado na fase de init
- **Deployment**: `execute.zip` (~4KB)
- **Ephemeral storage**: 2048MB — o provider extraído tem 780MB, e durante a extração o arquivo de 156MB ainda está no `/tmp`
- **Memory**: 3008MB — medido: carregar o provider é CPU, e a CPU acompanha a memória (design.md, *Otimização do Execute*)

## Fluxo

1. Init (cold start): baixa e descompacta o cache do provider (zstd) em `/tmp`, com a CPU cheia da fase de init
2. Assume a role de remediação com a session policy da violação
3. Baixa o `remediation.tf` do S3 e cria o symlink para o provider pré-inicializado
4. Só no ssl-only: lê a policy existente com um cliente assinado pelas credenciais da violação
5. Executa `tofu apply -refresh=false -auto-approve` (os `skip_*` do provider evitam as chamadas ao STS)
6. Retorna `{remediation_status, output/error}` e registra o tempo de cada fase numa linha JSON

## Build e Deploy

```bash
bash scripts/build_execute.sh   # baixa o OpenTofu fixado, confere o SHA256SUMS, gera tofu_layer.zip e execute.zip
# o apply publica a layer e o handler
```

Para trocar a versão do OpenTofu: `TOFU_VERSION=x.y.z bash scripts/build_execute.sh`,
e fixe a nova versão no script.

## Provider Cache

O pacote oficial `linux_arm64` do provider é baixado e extraído na estrutura de
um `.terraform/` já inicializado (não é preciso emular arm64), empacotado em
zstd e publicado no bucket de artefatos:

```bash
ARTIFACT_BUCKET=<bucket> bash scripts/build_provider_cache.sh
# -> s3://<bucket>/provider-cache/tf-dot-terraform.tar.zst (~123 MB, provider AWS 6.66.0)
```

O `scripts/bootstrap.sh` chama esse script numa conta nova. A versão do provider
é a que `provider.tf` resolve (`~> 6.0`, fixada pelo lock que o script gera) e
precisa aceitar a restrição dos `controls/*/remediation.tf`. Para trocar de
versão, ajuste `provider.tf`, rode o script de novo e meça uma nova série: o
cache está no caminho medido (a atualização 5.100.0 -> 6.66.0 custou +1,06 s na
remediação, ver `results/upgrade/`). O cache anterior (5.100.0, usado nas séries
até `results/final/`) fica em `provider-cache/tf-dot-terraform-aws-5.100.0.tar.gz`.

## Arquivo auxiliar

- `provider.tf`: declaração do provider AWS (usado apenas para gerar o cache)
