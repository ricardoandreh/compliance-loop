#!/usr/bin/env python3
"""
Formal comparison between benchmark series, and a check on the independence
assumption behind the confidence intervals.

The results chapter attributes each change in MTTR to one change in the system
(+0.18 s for scheduling the verification round, +0.28 s for per-violation
authority, nothing for the rest). Overlapping intervals "by eye" is not a test,
so this script puts a number on each claim:

- Welch's t (unequal variances) and a two-sided permutation test, which assumes
  nothing about the shape of the distribution;
- Hedges' g as the effect size, so a significant but negligible difference is
  visible as such.

It also answers the methodological question a reader should ask: the three
remediation samples of one suite run share that run's execution environments,
so they are not independent. The interval is recomputed over per-run means
(n = 10, the independent unit) and compared with the per-sample interval. If
they agree, the dependence does not change any conclusion.

Only warm samples in the slow execution environment enter the comparison (L12):
mixing the two regimes would test the hosts, not the system.

Usage:
    python3 scripts/compare_series.py                 # table on stdout
    python3 scripts/compare_series.py --out results/comparisons.json
"""

import argparse
import json
import math
import os
import random
import statistics

# Oldest first. Each series is one configuration of the system.
SERIES = ["echo", "two-round", "no-wait", "three-state", "session-policy", "final", "upgrade", "py314", "optimized", "revocation"]

# The comparisons the text makes, with what changed between the two series.
CLAIMS = [
    ("echo", "two-round", "verificação explícita (ScheduleVerification)"),
    ("two-round", "no-wait", "leitura imediata na verificação"),
    ("no-wait", "three-state", "grafo com os três estados formais"),
    ("three-state", "session-policy", "autoridade de remediação por sessão"),
    ("session-policy", "final", "identidades de serviço endurecidas"),
    ("final", "upgrade", "OPA 1.21.0, OpenTofu 1.12.6, provider AWS 6.66.0"),
    ("upgrade", "py314", "runtime Python 3.13 → 3.14"),
    ("py314", "optimized", "Execute a 3008 MB, cache zstd no init, skip_* e cliente sob demanda"),
    ("optimized", "revocation", "releitura consistente das exceções no Analyze; verificação lendo as definições na tabela; folga nos tempos limite"),
]

# Two-sided 95% t quantiles by degrees of freedom (df > 30 uses 1.96).
T_95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
        8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
        15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
        21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060, 26: 2.056,
        27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042}


def warm_slow(root, series, metric="t_remediation", scenarios=("C1", "C3", "C5")):
    """(run, value) for warm samples that ran in the slow environment."""
    report = json.load(open(os.path.join(root, series, "report_data.json")))
    facts_path = os.path.join(root, series, "series_facts.json")
    flags = json.load(open(facts_path))["remediation_samples"] if os.path.exists(facts_path) else {}
    out = []
    for x in report["samples"]:
        if x["metric"] != metric or x["scenario"] not in scenarios or x["cold"] is not False:
            continue
        if flags.get(f"run-{x['run']:02d}.json/{x['scenario']}", {}).get("fast"):
            continue
        out.append((x["run"], x["value"]))
    return out


def ci95(values):
    n = len(values)
    m = statistics.mean(values)
    half = T_95.get(n - 1, 1.96) * statistics.stdev(values) / math.sqrt(n)
    return m, half


def welch(a, b):
    va, vb, na, nb = statistics.variance(a), statistics.variance(b), len(a), len(b)
    se = math.sqrt(va / na + vb / nb)
    t = (statistics.mean(b) - statistics.mean(a)) / se
    df = (va / na + vb / nb) ** 2 / ((va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1))
    return t, df


def permutation_p(a, b, rounds=20000, seed=7):
    """Two-sided: how often a random relabelling is at least as far apart."""
    rng = random.Random(seed)
    observed = abs(statistics.mean(b) - statistics.mean(a))
    pool, hits = list(a) + list(b), 0
    for _ in range(rounds):
        rng.shuffle(pool)
        if abs(statistics.mean(pool[len(a):]) - statistics.mean(pool[:len(a)])) >= observed:
            hits += 1
    return (hits + 1) / (rounds + 1)


def hedges_g(a, b):
    na, nb = len(a), len(b)
    pooled = math.sqrt(((na - 1) * statistics.variance(a) + (nb - 1) * statistics.variance(b)) / (na + nb - 2))
    d = (statistics.mean(b) - statistics.mean(a)) / pooled
    return d * (1 - 3 / (4 * (na + nb) - 9))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="results")
    ap.add_argument("--out")
    args = ap.parse_args()

    data = {s: warm_slow(args.root, s) for s in SERIES}

    comparisons = []
    for before, after, change in CLAIMS:
        a = [v for _, v in data[before]]
        b = [v for _, v in data[after]]
        t, df = welch(a, b)
        comparisons.append({
            "from": before, "to": after, "change": change,
            "n": [len(a), len(b)],
            "delta_s": round(statistics.mean(b) - statistics.mean(a), 3),
            "welch_t": round(t, 2), "welch_df": round(df, 1),
            "permutation_p": round(permutation_p(a, b), 4),
            "hedges_g": round(hedges_g(a, b), 2),
        })

    clustering = []
    for s in SERIES:
        values = [v for _, v in data[s]]
        per_run = {}
        for run, v in data[s]:
            per_run.setdefault(run, []).append(v)
        run_means = [statistics.mean(v) for v in per_run.values()]
        m, half = ci95(values)
        mr, half_r = ci95(run_means)
        clustering.append({
            "series": s,
            "per_sample": {"mean": round(m, 3), "ci_half": round(half, 3), "n": len(values)},
            "per_run": {"mean": round(mr, 3), "ci_half": round(half_r, 3), "n": len(run_means)},
            "width_ratio": round(half_r / half, 2),
        })

    print("Comparação entre séries — t_remediation quente, ambiente lento (C1 · C3 · C5)\n")
    print(f"{'de':15} {'para':15} {'Δ (s)':>8} {'t':>7} {'gl':>6} {'p perm':>8} {'g':>6}  mudança")
    for c in comparisons:
        p = "<0,0001" if c["permutation_p"] < 0.0001 else f"{c['permutation_p']:.4f}"
        print(f"{c['from']:15} {c['to']:15} {c['delta_s']:+8.3f} {c['welch_t']:7.2f} "
              f"{c['welch_df']:6.1f} {p:>8} {c['hedges_g']:6.2f}  {c['change']}")
    print("\nIntervalo por amostra contra intervalo por rodada (unidade independente)\n")
    for c in clustering:
        ps, pr = c["per_sample"], c["per_run"]
        print(f"{c['series']:15} amostras {ps['mean']:.3f} ± {ps['ci_half']:.3f} (n={ps['n']:2})   "
              f"rodadas {pr['mean']:.3f} ± {pr['ci_half']:.3f} (n={pr['n']:2})   razão {c['width_ratio']}")

    if args.out:
        with open(args.out, "w") as f:
            json.dump({"comparisons": comparisons, "clustering": clustering}, f, indent=2, ensure_ascii=False)
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
