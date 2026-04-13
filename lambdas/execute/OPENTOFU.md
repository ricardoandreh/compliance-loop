# OpenTofu via Docker - Execute Lambda

Este documento explica como o Execute Lambda usa OpenTofu através da imagem oficial do Docker.

## Visão Geral

O Execute Lambda usa um **multi-stage Docker build** para incluir o binário OpenTofu da imagem oficial, seguindo as melhores práticas recomendadas pela documentação do OpenTofu.

## Arquitetura do Build

### Stage 1: OpenTofu Binary

```dockerfile
FROM ghcr.io/opentofu/opentofu:1.6-minimal AS tofu
```

Esta stage usa a imagem minimal do OpenTofu que contém apenas o binário `tofu` em `/usr/local/bin/tofu`.

**Por que usar a imagem minimal?**
- Contém apenas o binário (sem dependências extras)
- Imagem oficial mantida pelo projeto OpenTofu
- Versão específica garantida (1.6-minimal)
- Tamanho reduzido (~50MB)

### Stage 2: Lambda Runtime

```dockerfile
FROM public.ecr.aws/lambda/python:3.12
COPY --from=tofu /usr/local/bin/tofu /usr/local/bin/tofu
```

Esta stage:
1. Usa a imagem base do AWS Lambda para Python 3.12
2. Copia o binário `tofu` da Stage 1
3. Adiciona a configuração HCL e handler Python
4. Pré-inicializa os providers Terraform

## Imagens Disponíveis

O OpenTofu oferece várias tags para a imagem minimal:

| Tag | Descrição | Uso |
|-----|-----------|-----|
| `minimal` | Última versão disponível | Desenvolvimento |
| `1-minimal` | Última versão 1.x | Produção (major version lock) |
| `1.6-minimal` | Última versão 1.6.x | **Usado neste projeto** |
| `1.6.1-minimal` | Versão específica 1.6.1 | Produção (version pinning) |

## Vantagens do Multi-Stage Build

### 1. Sem Downloads Manuais
```dockerfile
# ❌ Abordagem antiga (manual)
RUN curl -Lo /tmp/tofu.zip https://github.com/opentofu/opentofu/releases/download/v1.6.0/tofu_1.6.0_linux_arm64.zip && \
    unzip /tmp/tofu.zip -d /usr/local/bin

# ✅ Abordagem atual (Docker)
FROM ghcr.io/opentofu/opentofu:1.6-minimal AS tofu
COPY --from=tofu /usr/local/bin/tofu /usr/local/bin/tofu
```

### 2. Imagem Final Menor
- Não inclui ferramentas de download (curl, unzip)
- Não inclui arquivos temporários (.zip)
- Apenas o binário necessário

### 3. Consistência
- Mesma versão em todos os ambientes
- Binário oficial verificado
- Reproduzível em qualquer máquina

### 4. Segurança
- Imagem oficial assinada
- Sem necessidade de verificar checksums manualmente
- Atualizações de segurança via tag updates

## Verificação do OpenTofu

### Durante o Build

O Dockerfile executa comandos OpenTofu durante o build:

```dockerfile
# Pre-cache providers to /opt/providers
RUN tofu providers mirror /opt/providers

# Initialize OpenTofu with cached providers
RUN tofu init -plugin-dir=/opt/providers
```

Isso garante que:
- O binário funciona corretamente
- Os providers são baixados e cacheados
- A configuração HCL é válida

### Após o Build

Você pode verificar a instalação:

```bash
# Verificar versão do OpenTofu
docker run --rm compliance-loop-execute-lambda:latest tofu --version

# Verificar localização do binário
docker run --rm compliance-loop-execute-lambda:latest which tofu

# Testar comando help
docker run --rm compliance-loop-execute-lambda:latest tofu --help
```

## Execução no Lambda

Quando a Lambda é invocada:

1. **Handler Python** (`handler.py`) é executado
2. **Copia arquivos** de `/opt/remediation/` para `/tmp/remediation/`
3. **Escreve tfvars** com bucket_name e region
4. **Executa OpenTofu** via subprocess:
   ```python
   subprocess.run([
       'tofu', 'apply',
       '-refresh=false',
       '-auto-approve',
       '-var-file=/tmp/terraform.tfvars.json',
       '-state=/tmp/remediation.tfstate',
       '-plugin-dir=/opt/providers'
   ])
   ```

## Troubleshooting

### Erro: "Failed to pull image"

```bash
# Verificar se a imagem está acessível
docker pull ghcr.io/opentofu/opentofu:1.6-minimal

# Se houver problemas de rede, use um mirror ou proxy
```

### Erro: "tofu: not found" no Lambda

```bash
# Verificar se o binário foi copiado
docker run --rm compliance-loop-execute-lambda:latest ls -la /usr/local/bin/tofu

# Verificar permissões
docker run --rm compliance-loop-execute-lambda:latest stat /usr/local/bin/tofu
```

### Erro: "exec format error"

```bash
# Verificar arquitetura do binário
docker run --rm compliance-loop-execute-lambda:latest file /usr/local/bin/tofu

# Deve mostrar: ELF 64-bit LSB executable, x86-64
```

### Build Lento

```bash
# Usar BuildKit para builds mais rápidos
export DOCKER_BUILDKIT=1
docker-compose build execute-lambda

# Cache das layers será reutilizado
```

## Atualizando a Versão do OpenTofu

Para atualizar para uma versão diferente, edite o Dockerfile:

```dockerfile
# Versão específica
FROM ghcr.io/opentofu/opentofu:1.6.2-minimal AS tofu

# Ou última versão 1.6.x
FROM ghcr.io/opentofu/opentofu:1.6-minimal AS tofu

# Ou última versão 1.x
FROM ghcr.io/opentofu/opentofu:1-minimal AS tofu
```

Depois reconstrua a imagem:

```bash
cd lambdas/execute/
./build.sh
```

## Comparação de Tamanhos

| Método | Tamanho da Layer | Dependências Extras |
|--------|------------------|---------------------|
| Download manual | ~60MB | curl, unzip, arquivos .zip |
| Multi-stage build | ~50MB | Nenhuma |

## Referências

- [OpenTofu Docker Installation](https://opentofu.org/docs/intro/install/docker/)
- [OpenTofu Minimal Images](https://github.com/orgs/opentofu/packages/container/package/opentofu)
- [Docker Multi-Stage Builds](https://docs.docker.com/build/building/multi-stage/)
- [AWS Lambda Container Images](https://docs.aws.amazon.com/lambda/latest/dg/images-create.html)

## Exemplo Completo

Dockerfile completo do Execute Lambda:

```dockerfile
# Stage 1: Get OpenTofu binary from official minimal image
FROM ghcr.io/opentofu/opentofu:1.6-minimal AS tofu

# Stage 2: Build Lambda image
FROM public.ecr.aws/lambda/python:3.12

# Copy OpenTofu binary from minimal image
COPY --from=tofu /usr/local/bin/tofu /usr/local/bin/tofu

# Create remediation directory
RUN mkdir -p /opt/remediation

# Copy remediation.tf to /opt/remediation/
COPY remediation.tf /opt/remediation/

# Set working directory for provider initialization
WORKDIR /opt/remediation

# Pre-cache providers to /opt/providers
RUN tofu providers mirror /opt/providers

# Initialize OpenTofu with cached providers
RUN tofu init -plugin-dir=/opt/providers

# Copy handler.py to Lambda task root
COPY handler.py ${LAMBDA_TASK_ROOT}/

# Set the CMD to your handler
CMD ["handler.lambda_handler"]
```

Este approach garante que o OpenTofu seja instalado de forma confiável, segura e eficiente.
