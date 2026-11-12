# Registro de mudanças

As etiquetas marcam o código que produziu cada série publicada. O projeto não
segue versionamento semântico: é um artefato de pesquisa, sem API pública.
Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/).

## Não lançado

### Adicionado
- Roteiro de implantação do zero: `scripts/bootstrap.sh`, `Makefile`, backend e
  variáveis por ambiente, executado numa segunda região em 09/11/2026.
- Correspondência de cada controle com o NIST SP 800-53 e a ISO/IEC 27002.
- `scripts/audit_cloudtrail_link.py`, que liga cada remediação da trilha ao seu
  evento no CloudTrail.
- Licença MIT, `CITATION.cff` e a documentação dividida em `ARCHITECTURE.md`,
  `REPRODUCING.md`, `results/README.md` e `docs/`.

### Alterado
- Scripts leem região, projeto e ambiente de um único módulo.
- Zips das layers reproduzíveis; versão do provider fixada no cache do Execute.
- O bucket de logs da trail é destruído com o conteúdo.

### Removido
- Página interativa de resultados e seu gerador.
- Arquivos sem uso: `docker-compose.yml`, executores de teste antigos, blocos
  de importação da conta original.

## v1.1.0 - 2026-10-29

Décima série, `results/revocation/`.

### Adicionado
- Canal único para a escrita de exceções: só o papel `exception-writer` grava
  na tabela (cenário C11).
- Revogação de exceção por status, reavaliada na hora (cenário C12).

### Alterado
- A exceção é relida na tabela, com leitura consistente, antes de valer.
- A rodada de verificação lê as definições dos controles na tabela.
- Hipóteses alinhadas à monografia: H1 exposição, H2 rastreabilidade, H3
  convergência.

## v1.0.0 - 2026-09-27

Série somativa da monografia, `results/optimized/`: laço em duas rodadas com
verificação por releitura, três estados de classificação, disjuntor de
reincidência, autoridade de remediação por sessão e Execute otimizado.
