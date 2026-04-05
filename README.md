# MVP Compliance Automation Loop

Sistema de automação de conformidade baseado no padrão MAPE-K para AWS S3.

## Estrutura do Projeto

```
.
├── infra/              # Configurações OpenTofu/Terraform
├── lambdas/            # Código das funções Lambda
│   ├── analyze/        # Analyze Lambda (classificação de conformidade)
│   ├── execute/        # Execute Lambda (remediação com OpenTofu)
│   ├── escalation/     # Escalation Lambda (criação de GitHub Issues)
│   ├── requirements.txt
│   └── Dockerfile      # Container image para Execute Lambda
├── policies/           # Políticas Rego para OPA
├── tests/              # Testes
│   ├── unit/           # Testes unitários
│   ├── integration/    # Testes de integração end-to-end
│   └── property/       # Property-based tests
└── .kiro/specs/        # Documentação de especificação
```

## Requisitos

- Python 3.12+
- OpenTofu 1.6+
- Docker (para build da Execute Lambda container image)
- AWS CLI configurado

## Instalação

```bash
# Instalar dependências Python
pip install -r lambdas/requirements.txt

# Build da container image do Execute Lambda
cd lambdas
docker build -t execute-lambda .
```

## Testes

```bash
# Executar todos os testes
pytest tests/

# Executar apenas unit tests
pytest tests/unit/

# Executar property tests com cobertura
pytest tests/property/ --hypothesis-show-statistics

# Executar com cobertura
pytest --cov=lambdas tests/
```
