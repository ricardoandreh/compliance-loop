# Resultados agregados

Execuções da suíte: **3** — gerado em 2026-09-10 04:08 UTC.
Latências consideram apenas execuções em que o cenário passou; a taxa de sucesso é reportada separadamente. IC 95% pela distribuição t de Student (desvio amostral, n-1).

## Por hipótese

| Hipótese | Métrica | n | Média (s) | Desvio (s) | Mediana (s) | Mín | Máx | IC 95% |
|---|---|---:|---:|---:|---:|---:|---:|---|
| H1 (MTTR) ⚠️ | `t_remediation` | 9 | 14.50 | 6.44 | 18.62 | 5.83 | 19.20 | [9.55, 19.45] |
| H3 (tempo de exposição) | `t_exposure` | 6 | 27.36 | 1.30 | 27.45 | 25.38 | 29.01 | [25.99, 28.73] |

> ⚠️ **H1 (MTTR) agrega cenários de regimes diferentes** (C3 ≈ 5.9s, C5 ≈ 18.7s, C1 ≈ 18.9s). Cada cenário isolado é estável — o maior desvio dentro de um único cenário é 0.45s, contra 6.44s no agregado. O desvio agregado mede a distância ENTRE cenários, não a variabilidade da medição, e a média agregada não descreve nenhum deles. Reporte os cenários separadamente, ou justifique a agregação.

## Por cenário

| Cenário | Sucesso | Métrica | n | Média (s) | Desvio (s) | Mediana (s) | Mín | Máx | IC 95% |
|---|---|---|---:|---:|---:|---:|---:|---:|---|
| AUDIT | 0/3 | — | — | — | — | — | — | — | — |
| C1 | 3/3 | `t_detection` | 3 | 7.90 | 1.30 | 7.63 | 6.76 | 9.32 | [4.67, 11.14] |
|  |  | `t_exposure` | 3 | 26.80 | 1.40 | 26.83 | 25.38 | 28.18 | [23.32, 30.28] |
|  |  | `t_remediation` | 3 | 18.89 | 0.29 | 18.86 | 18.62 | 19.20 | [18.17, 19.62] |
| C2 | 3/3 | `t_classification` | 3 | 4.65 | 7.40 | 0.42 | 0.34 | 13.20 | [-13.74, 23.04] |
| C3 | 3/3 | `t_remediation` | 3 | 5.92 | 0.08 | 5.96 | 5.83 | 5.98 | [5.72, 6.13] |
|  |  | `t_remediation_post_expiry` | 3 | 78.02 | 10.52 | 82.03 | 66.09 | 85.94 | [51.90, 104.14] |
|  |  | `t_scheduler_fire` | 3 | 72.09 | 10.58 | 76.07 | 60.10 | 80.11 | [45.81, 98.38] |
| C4 | 3/3 | `t_escalation` | 3 | 0.56 | 0.02 | 0.56 | 0.55 | 0.58 | [0.53, 0.60] |
| C5 | 3/3 | `t_detection` | 3 | 9.24 | 0.99 | 9.78 | 8.09 | 9.84 | [6.77, 11.70] |
|  |  | `t_exposure` | 3 | 27.93 | 1.16 | 28.06 | 26.71 | 29.01 | [25.06, 30.80] |
|  |  | `t_remediation` | 3 | 18.69 | 0.45 | 18.62 | 18.28 | 19.17 | [17.57, 19.81] |
| C6 | 3/3 | `t_classification` | 3 | 9.17 | 7.56 | 13.29 | 0.45 | 13.78 | [-9.60, 27.95] |
| C7 | 3/3 | `t_sync_delete` | 3 | 15.58 | 0.00 | 15.58 | 15.58 | 15.58 | [15.58, 15.58] |
|  |  | `t_sync_insert` | 3 | 15.65 | 0.06 | 15.62 | 15.61 | 15.72 | [15.50, 15.80] |
| CTRL-01 | 3/3 | `t_total` | 3 | 29.14 | 0.11 | 29.10 | 29.06 | 29.27 | [28.87, 29.42] |
| CTRL-02 | 3/3 | `t_total` | 3 | 25.82 | 0.00 | 25.82 | 25.82 | 25.82 | [25.82, 25.82] |
| CTRL-03 | 3/3 | `t_total` | 3 | 28.22 | 2.01 | 29.37 | 25.90 | 29.39 | [23.23, 33.21] |
| CTRL-04 | 3/3 | `t_total` | 3 | 27.32 | 1.87 | 26.27 | 26.21 | 29.47 | [22.68, 31.95] |

