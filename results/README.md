# Resultados

Cada série é uma configuração do artefato, medida por dez execuções da suíte de
cenários contra a conta real. Uma pasta por série, nunca misturadas.

## Séries

| Pasta | O que mudou em relação à anterior |
|---|---|
| `echo/` | Primeira série: a remediação é confirmada pelo eco do CloudTrail |
| `two-round/` | Laço fechado em duas rodadas, com espera fixa de 15 s antes da verificação |
| `no-wait/` | Verificação sem espera |
| `three-state/` | Grafo com os três estados formais; cenários C8 e CTRL-05 |
| `session-policy/` | Disjuntor de reincidência, controle detectivo e autoridade de remediação por sessão |
| `final/` | Identidades de serviço endurecidas |
| `upgrade/` | OPA 1.21.0, OpenTofu 1.12.6 e provider AWS 6.66.0 |
| `py314/` | Runtime das funções de Python 3.13 para 3.14 |
| `optimized/` | Execute otimizado. É a **série somativa** da monografia, etiqueta `v1.0.0` |
| `revocation/` | Releitura consistente das exceções, verificação lendo as definições na tabela, canal único de exceções; cenários C11 e C12. Décima série, etiqueta `v1.1.0` |

As séries até `final/` foram medidas com OPA 1.4.2 e o provider 5.100.0.
`discarded/` guarda as rodadas excluídas da agregação, com o motivo de cada uma.
`perf/` guarda o experimento de dimensionamento do Execute.

## Arquivos de uma série

| Arquivo | Conteúdo | Gerado por |
|---|---|---|
| `run-NN.json` | Dado bruto de uma execução da suíte | `scripts/benchmark.py` |
| `summary.json`, `summary.md` | n, média, desvio, mediana e IC 95% por cenário e hipótese | `scripts/benchmark.py --aggregate-only` |
| `report_data.json` | Amostras e estatísticas por cenário, base das tabelas e figuras | `scripts/build_report_data.py` |
| `cold_starts.json` | Classificação de cada amostra em fria ou quente e em patamar | `scripts/classify_cold_starts.py` |
| `verification_consistency.json` | Leituras da rodada de verificação e seu resultado | `scripts/verification_consistency.py` |
| `series_facts.json` | Execuções por rodada e ambientes do Execute | `scripts/collect_series_facts.py` |
| `log_audit.json` | Completude da trilha de auditoria | `scripts/audit_log_completeness.py` |
| `cloudtrail_link.json` | Ligação de cada remediação ao seu evento no CloudTrail | `scripts/audit_cloudtrail_link.py` |
| `cost_model.json`, `lambda_sizing.json` | Custo por incidente e dimensionamento | `scripts/cost_model.py`, `scripts/lambda_sizing.py` |

`../results/comparisons.json` traz as comparações entre séries (Welch,
permutação, Holm e g de Hedges), geradas por `scripts/compare_series.py`.

## Dados derivados

Os dados derivados de cada série são regenerados assim:

```bash
S=results/optimized
# Com acesso à conta (uma vez por série, enquanto o histórico existe):
.venv/bin/python scripts/classify_cold_starts.py --results $S
.venv/bin/python scripts/verification_consistency.py --results $S
.venv/bin/python scripts/collect_series_facts.py --results $S
# Offline, a partir dos arquivos acima:
python3 scripts/build_report_data.py --results $S --out $S/report_data.json
```

O Step Functions guarda o histórico por 90 dias; os três primeiros passos gravam
em cada série o que só existe na conta (classificação frio/quente, consistência
de leitura, containers do Execute e execuções por rodada), e daí em diante a
análise é refeita só com o repositório.

## Cenários validados

Todos aprovados em **10 de 10** execuções da suíte (160/160, `results/optimized/`).
Valores: média ± desvio. Nenhuma amostra foi fria nesta série (classificação por
`Init Duration` na invocação da Lambda, `scripts/classify_cold_starts.py`); as médias
de remediação juntam os dois patamares de ambiente — separados, estão em *Performance*.

| Cenário | Descrição | Resultado | Métrica (n=10) |
|---|---|---|---|
| C1 | Acesso público do S3 desligado pelo console | Remedia e verifica S_nominal | `t_remediation` 5,87s ± 0,81 |
| C2 | Acesso público com exceção ativa | Não remedia, registra S_exception | `t_classification` 0,49s ± 0,04 |
| C3 | Exceção expira → remediação | Scheduler dispara, remedia e verifica | `t_remediation` 6,09s ± 0,77 |
| C4 | Falha do sistema (bucket inexistente) | Sem estado; escala via GitHub Issue | `t_escalation` 0,45s ± 0,04 |
| C5 | Acesso público desligado por IaC externo | Detecta, remedia e verifica | `t_remediation` 5,68s ± 0,71 |
| C6 | Bucket já conforme | Confirma S_nominal, sem Execute | `t_classification` 0,49s ± 0,04 |
| C7 | Controle publicado e removido | DynamoDB Stream dispara controls-sync | `t_sync_insert` 15,62s ± 0,01 |
| C8 | Remediação negada pelo IAM | S_violation preservado, escala com o erro | `t_escalation` 5,98s ± 0,77 |
| C9 | Mesma violação repetida | Disjuntor escala na 4ª, sem remediar | `t_escalation` 0,73s ± 0,06 |
| C10 | Template adulterado para outro bucket | Sessão escopada nega a escrita | 10/10 |
| C11 | Exceção gravada fora do canal de aprovação | A tabela nega a escrita; a do papel `exception-writer` é aceita | 10/10 (`results/revocation/`) |
| C12 | Exceção revogada por status antes do vencimento | Reavalia na hora, remedia e verifica | 10/10: remediado 8,13 s ± 0,48 depois da revogação (`results/revocation/`) |
| AUDIT | Ausência de execuções falhas inesperadas | Nenhuma além das esperadas do C4, C8, C9 e CTRL-06 | 10/10 |

O **disjuntor disparou na quarta violação em 10 de 10 rodadas**: com limiar 3, as três
primeiras são remediadas e a quarta é a primeira a enxergar três remediações na janela.

## Controles validados

| Teste | Controle | Verificação | Resultado (n=10) |
|---|---|---|---|
| CTRL-01 | ctrl-s3-public-block | All 4 settings = true | `t_total` 29,10s ± 1,82 |
| CTRL-02 | ctrl-s3-ssl-only | DenyNonSSLRequests present | `t_total` 27,33s ± 1,35 |
| CTRL-03 | ctrl-s3-versioning | Versioning = Enabled | `t_total` 27,12s ± 0,71 |
| CTRL-04 | ctrl-s3-encryption | Classificado conforme (AES256 padrão) | `t_total` 26,97s ± 0,17 |
| CTRL-05 | ctrl-ebs-encryption-default | EbsEncryptionByDefault = true | `t_remediation` 5,77s ± 0,79 |
| CTRL-06 | ctrl-ebs-volume-encrypted (detectivo) | Volume sem criptografia escala sem remediação | `t_escalation` 0,86s ± 0,12 |

O `t_total` das CTRL-* mede o **teste**: criação de bucket, esperas de CloudTrail e a
espera pelas rodadas de verificação. Não é comparável ao `t_remediation`.

**Remediar EBS custa o mesmo que remediar S3**: o tempo do loop é dominado pela
inicialização do `tofu`, não pelo serviço. O **CTRL-06 é detectivo**: um volume existente
não pode ser criptografado no lugar, então o loop classifica e escala sem tocar no
recurso (L14).

## Desempenho

Agregado de **10 execuções completas da suíte** na configuração atual
(160/160 cenários aprovados, `results/optimized/`), via `scripts/benchmark.py`.
Desvio amostral (n−1) e IC 95% pela distribuição t.

Uma Lambda roda numa microVM que a AWS cria e reaproveita, e o hospedeiro varia: nesta
série, 123 das 470 invocações do Execute ficaram num patamar ~1,5s mais rápido, todas no
mesmo ambiente de execução. Por isso as comparações entre séries são feitas no **patamar
lento**, o único presente em todas (L12). Nenhuma das 80 amostras foi fria.

| Métrica | Quente (patamar lento) | Patamar rápido |
|---|---|---|
| **H1 — t_exposure** (C1 · C5) | **13,87s** ± 1,71 · IC [12,56 · 15,19] · n=9 | |
| MTTR — t_remediation, componente de H1 (C1 · C3 · C5) | **6,61s** ± 0,11 · IC [6,55 · 6,67] · n=15 | 5,15s ± 0,13 · n=15 |
| **H3 — t_verified** (convergência confirmada) | **14,19s** ± 1,70 · n=9 | |
| t_detection (evento → SFN start), componente de H1 | sem classe: 7,90s ± 1,46 · IC [7,21 · 8,58] · n=20 | |

**Toda a variação do MTTR entre as seis primeiras séries é atribuível, estado a estado:**
6,03s no laço por eco → +0,18s do estado que agenda a verificação → +0,28s da autoridade
por sessão = 6,54s. Leitura imediata, grafo de três estados, disjuntor, controle detectivo
e endurecimento das identidades de serviço **não** mexeram no MTTR.

**A atualização de versões custou mais que qualquer mudança de desenho, e a
otimização do Execute a devolveu.** Com OPA 1.21.0, OpenTofu 1.12.6 e o provider AWS
6.66.0 (`results/upgrade/`), a remediação quente foi a **7,59s ± 0,15** (+1,06s,
p < 0,0001), quase toda no Execute, que carrega um provider 19% maior a cada
`apply`. O Python 3.14 não mudou nada (`results/py314/`, −0,01s, p = 0,83). Com o
Execute a 3008 MB, o cache do provider em zstd baixado no init, os `skip_*` do provider
e o cliente escopado sob demanda (`results/optimized/`), voltou a
**6,61s ± 0,11** (−0,97s, p < 0,0001) — o nível da série final, com as versões novas.
O Python do handler responde por 3% do tempo do Execute; os outros 97% são o `tofu`
(`results/perf/`).

**A exposição é dominada pela detecção:** desvio de 1,46s na detecção contra 0,11s na
remediação, com `t_detection + t_remediation = t_exposure` em todas as execuções. Reduzir
a exposição exigiria trocar o Monitor, não otimizar o loop.

**Leitura imediata na verificação, medida.** Somando as sete séries sem espera fixa são
**2.760 leituras de controle, todas conformes**, feitas ~0,35s depois da escrita. O
`t_verified` fica **0,32s** acima da exposição, contra 15,4s quando havia a espera de 15s.

**Cold start** é raro e não custa sempre o mesmo: ~10s de Execute frio a 3008 MB
(~3,8s de init, que baixa e descompacta o cache, + ~6,1s de handler), contra 16–17s
no upgrade, quando o cache ainda era baixado no handler.

A otimização chave: o provider OpenTofu é pré-inicializado e armazenado no S3 como cache
(123 MB em zstd, 780 MB extraído, provider 6.66.0), baixado e extraído em `/tmp` no init
e compartilhado via symlink entre controles. Elimina `tofu init` (~30-50s) de cada invocação.

## Custo

Medido na série atual (`scripts/cost_model.py`, `results/optimized/`), com
preços de lista de us-east-1: **~US$ 0,0014 por incidente remediado** (US$ 1,40
a cada mil). A maior parcela são as transições do Step Functions Standard (66%),
seguidas pelo processamento das Lambdas (27%, quase todo do Execute a 3008 MB) e
pela ingestão de logs (8%). A camada de
operação custa ~US$ 1,30/mês em alarmes, mais US$ 3,00/mês do painel acima dos
três gratuitos.
