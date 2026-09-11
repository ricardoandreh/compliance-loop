# Resultados agregados

Execuções da suíte: **3** — gerado em 2026-09-11 02:49 UTC.
Latências consideram apenas execuções em que o cenário passou; a taxa de sucesso é reportada separadamente. IC 95% pela distribuição t de Student (desvio amostral, n-1).

## Por hipótese

| Hipótese | Métrica | n | Média (s) | Desvio (s) | Mediana (s) | Mín | Máx | IC 95% |
|---|---|---:|---:|---:|---:|---:|---:|---|
| H1 (MTTR) ⚠️ | `t_remediation` | 9 | 15.40 | 7.98 | 18.00 | 4.88 | 24.97 | [9.27, 21.54] |
| H3 (tempo de exposição) | `t_exposure` | 6 | 27.44 | 4.23 | 26.93 | 23.29 | 33.01 | [23.00, 31.89] |

> ⚠️ **H1 (MTTR) agrega cenários de regimes diferentes** (C3 ≈ 5.5s, C1 ≈ 19.5s, C5 ≈ 21.2s). Cada cenário isolado é estável — o maior desvio dentro de um único cenário é 4.89s, contra 7.98s no agregado. O desvio agregado mede a distância ENTRE cenários, não a variabilidade da medição, e a média agregada não descreve nenhum deles. Reporte os cenários separadamente, ou justifique a agregação.

## Por cenário

| Cenário | Sucesso | Métrica | n | Média (s) | Desvio (s) | Mediana (s) | Mín | Máx | IC 95% |
|---|---|---|---:|---:|---:|---:|---:|---:|---|
| AUDIT | 3/3 | — | — | — | — | — | — | — | — |
| C1 | 3/3 | `t_detection` | 3 | 7.25 | 1.12 | 7.75 | 5.97 | 8.04 | [4.47, 10.04] |
|  |  | `t_exposure` | 3 | 26.76 | 5.43 | 23.97 | 23.29 | 33.01 | [13.28, 40.24] |
|  |  | `t_remediation` | 3 | 19.50 | 4.89 | 18.00 | 15.54 | 24.97 | [7.35, 31.66] |
| C2 | 3/3 | `t_classification` | 3 | 0.51 | 0.03 | 0.52 | 0.48 | 0.54 | [0.44, 0.59] |
| C3 | 3/3 | `t_remediation` | 3 | 5.55 | 0.60 | 5.73 | 4.88 | 6.04 | [4.06, 7.04] |
|  |  | `t_remediation_post_expiry` | 3 | 72.00 | 1.89 | 71.95 | 70.13 | 73.91 | [67.30, 76.69] |
|  |  | `t_scheduler_fire` | 3 | 66.44 | 2.11 | 67.07 | 64.09 | 68.17 | [61.20, 71.69] |
| C4 | 3/3 | `t_escalation` | 3 | 0.55 | 0.05 | 0.55 | 0.50 | 0.60 | [0.43, 0.67] |
| C5 | 3/3 | `t_detection` | 3 | 6.97 | 2.45 | 5.99 | 5.16 | 9.76 | [0.88, 13.06] |
|  |  | `t_exposure` | 3 | 28.13 | 3.74 | 29.90 | 23.83 | 30.65 | [18.84, 37.42] |
|  |  | `t_remediation` | 3 | 21.16 | 3.13 | 20.14 | 18.67 | 24.67 | [13.39, 28.93] |
| C6 | 3/3 | `t_classification` | 3 | 0.47 | 0.07 | 0.49 | 0.39 | 0.52 | [0.30, 0.64] |
| C7 | 3/3 | `t_sync_delete` | 3 | 15.56 | 0.01 | 15.56 | 15.56 | 15.57 | [15.55, 15.58] |
|  |  | `t_sync_insert` | 3 | 15.60 | 0.01 | 15.60 | 15.59 | 15.60 | [15.58, 15.61] |
| CTRL-01 | 3/3 | `t_total` | 3 | 26.92 | 1.82 | 25.89 | 25.85 | 29.03 | [22.39, 31.46] |
| CTRL-02 | 3/3 | `t_total` | 3 | 25.86 | 0.11 | 25.82 | 25.78 | 25.99 | [25.59, 26.14] |
| CTRL-03 | 3/3 | `t_total` | 3 | 26.92 | 1.81 | 25.90 | 25.85 | 29.01 | [22.42, 31.42] |
| CTRL-04 | 3/3 | `t_total` | 3 | 26.22 | 0.02 | 26.21 | 26.20 | 26.24 | [26.16, 26.27] |

