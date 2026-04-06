# Quick Start - Execute Lambda Container Build

Este guia rápido mostra como construir e fazer deploy da imagem Docker do Execute Lambda.

## Pré-requisitos

✅ Docker instalado e rodando  
✅ AWS CLI configurado com credenciais  
✅ Repositório ECR criado (via Terraform/OpenTofu)

## Verificar Pré-requisitos

```bash
# 1. Verificar Docker
docker --version
docker info

# 2. Verificar AWS CLI
aws --version
aws sts get-caller-identity

# 3. Verificar repositório ECR existe
aws ecr describe-repositories --repository-names compliance-loop-execute-lambda
```

## Build Local (Teste)

Para testar o build sem fazer push para ECR:

```bash
cd lambdas/execute/

# Opção 1: Usando o script
./build.sh

# Opção 2: Docker direto
docker-compose build execute-lambda
```

A imagem usa multi-stage build com OpenTofu oficial:
- Stage 1: `ghcr.io/opentofu/opentofu:1.6-minimal` (apenas o binário tofu)
- Stage 2: `public.ecr.aws/lambda/python:3.12` (runtime Lambda)

## Build e Push para ECR

```bash
cd lambdas/execute/

# Opção 1: Usando o script (recomendado)
./build.sh

# Opção 2: Usando Make
make push
```

O script irá:
1. ✓ Obter seu AWS Account ID
2. ✓ Autenticar Docker no ECR
3. ✓ Construir a imagem usando docker-compose (multi-stage build com OpenTofu oficial)
4. ✓ Fazer push para ECR

## Verificar Imagem no ECR

```bash
# Listar imagens no repositório
aws ecr list-images --repository-name compliance-loop-execute-lambda

# Ver detalhes da imagem
aws ecr describe-images \
  --repository-name compliance-loop-execute-lambda \
  --image-ids imageTag=latest
```

## Deploy da Lambda Function

Depois que a imagem estiver no ECR:

```bash
# Opção 1: Via Terraform/OpenTofu
cd infra/
tofu apply

# Opção 2: Atualizar função existente
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=us-east-1

aws lambda update-function-code \
  --function-name compliance-loop-execute \
  --image-uri ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/compliance-loop-execute-lambda:latest
```

## Testar Localmente

```bash
# Testar se o handler importa corretamente
make test

# Ou manualmente
docker run --rm \
  -e AWS_REGION=us-east-1 \
  compliance-loop-execute-lambda:latest \
  python -c "import handler; print('OK')"

# Abrir shell no container
make shell
```

## Comandos Úteis

```bash
# Ver tamanho da imagem
make size

# Inspecionar imagem
make inspect

# Limpar imagens locais
make clean

# Ver todos os comandos disponíveis
make help
```

## Troubleshooting Rápido

### Erro: "Docker is not running"
```bash
# Inicie o Docker Desktop (macOS/Windows)
# Ou no Linux:
sudo systemctl start docker
```

### Erro: "Failed to get AWS account ID"
```bash
# Configure AWS CLI
aws configure

# Ou use variáveis de ambiente
export AWS_ACCESS_KEY_ID=your-key
export AWS_SECRET_ACCESS_KEY=your-secret
export AWS_REGION=us-east-1
```

### Erro: "RepositoryNotFoundException"
```bash
# Crie o repositório ECR primeiro
cd infra/
tofu init
tofu apply -target=aws_ecr_repository.execute_lambda
```

### Erro: "exec format error" ao rodar Lambda
```bash
# Verifique se a imagem OpenTofu está acessível
docker pull ghcr.io/opentofu/opentofu:1.6-minimal

# Reconstrua a imagem
cd lambdas/execute/
./build.sh
```

### Erro: "tofu: command not found" na Lambda
```bash
# Verifique se o binário foi copiado corretamente
docker run --rm compliance-loop-execute-lambda:latest which tofu
docker run --rm compliance-loop-execute-lambda:latest tofu --version
```

## Workflow Completo

```bash
# 1. Criar repositório ECR
cd infra/
tofu init
tofu apply -target=aws_ecr_repository.execute_lambda

# 2. Build e push da imagem
cd ../lambdas/execute/
./build.sh

# 3. Deploy da infraestrutura completa
cd ../../infra/
tofu apply

# 4. Verificar
aws lambda get-function --function-name compliance-loop-execute
```

## Próximos Passos

Depois do deploy:
- Testar a Lambda function manualmente
- Configurar Step Functions para invocar a Lambda
- Monitorar logs no CloudWatch
- Configurar alarmes para falhas

Ver [README.md](README.md) para documentação completa.
