#!/usr/bin/env python3
"""
Repeated-run benchmark for the MAPE-K compliance loop.

A single run of integration_test.py reports one observation per metric, which
is enough to show the loop works but not enough to state a latency. This driver
repeats the suite N times, keeps every raw result, and reports n, mean, sample
standard deviation and a 95% confidence interval per scenario and per
hypothesis — the form the results chapter needs.

Two properties are deliberate:

- Latency statistics are computed ONLY over runs where the scenario passed.
  Averaging a failed run's partial timings would flatter the numbers. Failures
  are not hidden: every table carries a pass rate next to the latency.
- Every run's raw JSON is kept on disk, so the aggregate can be recomputed and
  audited without re-running against AWS.

Usage:
    python3 scripts/benchmark.py --runs 10
    python3 scripts/benchmark.py --runs 10 --test C1
    python3 scripts/benchmark.py --aggregate-only --out results/
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
INTEGRATION_TEST = os.path.join(HERE, "integration_test.py")

# Which metric answers which hypothesis. Metrics are collected per scenario and
# then pooled per hypothesis, because H1/H3 are claims about the system, not
# about any one scenario.
HYPOTHESES = {
    # (metrics, scenarios). Pooled over the S3 violation scenarios only: CTRL-05
    # also reports t_remediation, but for an account-level EC2 setting with its
    # own Terraform resource, and mixing it in would describe neither.
    "H1 (MTTR)": (["t_remediation"], ("C1", "C3", "C5")),
    "H3 (tempo de exposição)": (["t_exposure"], ("C1", "C5")),
    # Event -> the verification round confirms S_nominal, read straight after
    # the remediation (no consistency wait since results/no-wait/).
    "Convergência verificada": (["t_verified"], ("C1", "C5")),
}

# Two-tailed t critical values at 95%, indexed by degrees of freedom (n-1).
# Embedded rather than pulled from scipy: n is small here, and the whole point
# of this script is that it runs anywhere without extra dependencies.
T_95 = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
    8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
    15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060, 26: 2.056,
    27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
}


def t_critical(df):
    """t value for a 95% two-tailed interval; 1.960 (normal) for large df."""
    if df <= 0:
        return None
    return T_95.get(df, 1.960)


def summarize(values):
    """n / mean / sd / median / min / max / 95% CI for one metric."""
    n = len(values)
    if n == 0:
        return None
    mean = statistics.mean(values)
    if n == 1:
        # One observation is reported honestly as one observation: no spread,
        # no interval. This is exactly the situation the script exists to fix.
        return {"n": 1, "mean": mean, "sd": None, "median": mean,
                "min": mean, "max": mean, "ci95": None}
    sd = statistics.stdev(values)          # sample sd (n-1)
    half = t_critical(n - 1) * sd / (n ** 0.5)
    return {
        "n": n,
        "mean": mean,
        "sd": sd,
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
        "ci95": [mean - half, mean + half],
    }


def run_suite(run_index, out_dir, extra_args):
    """Run the suite once; return its parsed payload, or None if it produced none."""
    path = os.path.join(out_dir, f"run-{run_index:02d}.json")
    cmd = [sys.executable, INTEGRATION_TEST, "--json-out", path] + extra_args
    started = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = round(time.time() - started, 1)

    # A non-zero exit means some scenario failed, which is data, not a reason to
    # stop: the run's JSON still carries the scenarios that did pass.
    status = "ok" if proc.returncode == 0 else f"exit={proc.returncode}"
    if not os.path.exists(path):
        print(f"  run {run_index:02d}: NO OUTPUT ({status}, {elapsed}s)", flush=True)
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
        for line in tail:
            print(f"      {line}", flush=True)
        return None

    with open(path) as f:
        payload = json.load(f)
    passed = sum(1 for v in payload.get("results", {}).values() if v == "PASS")
    total = len(payload.get("results", {}))
    print(f"  run {run_index:02d}: {passed}/{total} passed ({status}, {elapsed}s) -> {path}", flush=True)
    return payload


def load_runs(out_dir):
    """Load every run-NN.json already on disk, in order."""
    if not os.path.isdir(out_dir):
        return []
    runs = []
    for name in sorted(os.listdir(out_dir)):
        if name.startswith("run-") and name.endswith(".json"):
            with open(os.path.join(out_dir, name)) as f:
                runs.append(json.load(f))
    return runs


def aggregate(runs):
    """Collapse many runs into per-scenario and per-hypothesis statistics."""
    scenarios = {}

    for payload in runs:
        results = payload.get("results", {})
        metrics = payload.get("metrics", {})
        for name, verdict in results.items():
            entry = scenarios.setdefault(name, {"pass": 0, "total": 0, "metrics": {}})
            entry["total"] += 1
            if verdict != "PASS":
                continue
            entry["pass"] += 1
            # Only a passing run contributes latencies.
            for metric, value in (metrics.get(name) or {}).items():
                if isinstance(value, (int, float)):
                    entry["metrics"].setdefault(metric, []).append(float(value))

    per_scenario = {}
    for name, entry in sorted(scenarios.items()):
        per_scenario[name] = {
            "pass": entry["pass"],
            "total": entry["total"],
            "pass_rate": entry["pass"] / entry["total"] if entry["total"] else 0.0,
            "metrics": {m: summarize(v) for m, v in sorted(entry["metrics"].items())},
        }

    # Pool each hypothesis' metric across every scenario that reports it.
    per_hypothesis = {}
    for hypothesis, (metric_names, pooled_scenarios) in HYPOTHESES.items():
        pooled, sources = [], []
        for name, entry in scenarios.items():
            if name not in pooled_scenarios:
                continue
            for metric in metric_names:
                if entry["metrics"].get(metric):
                    pooled.extend(entry["metrics"][metric])
                    sources.append(name)
        if pooled:
            summary = summarize(pooled)
            # Pooling is only meaningful if the scenarios share a distribution.
            # They often do not: remediating a fresh violation (cold Lambda,
            # cold provider cache) and remediating after an exception expires
            # (both already warm) are different regimes, each internally tight.
            # When that happens the pooled sd is dominated by the gap BETWEEN
            # scenarios, and the pooled mean describes neither — so say so
            # instead of publishing a number that looks like a measurement.
            per_scenario_means = []
            widest_within = 0.0
            for name, entry in scenarios.items():
                for metric in metric_names:
                    vals = entry["metrics"].get(metric)
                    if vals:
                        per_scenario_means.append((name, statistics.mean(vals)))
                        if len(vals) > 1:
                            widest_within = max(widest_within, statistics.stdev(vals))
            spread = (max(m for _, m in per_scenario_means)
                      - min(m for _, m in per_scenario_means)) if per_scenario_means else 0.0
            summary["per_scenario_means"] = dict(per_scenario_means)
            summary["widest_within_scenario_sd"] = widest_within or None
            # Heuristic: the scenario means are further apart than the noise
            # inside any one of them, by a wide margin.
            summary["heterogeneous"] = bool(
                len(per_scenario_means) > 1 and spread > max(3 * widest_within, 1.0))
            per_hypothesis[hypothesis] = {
                "metric": "/".join(metric_names),
                "scenarios": sorted(set(sources)),
                **summary,
            }

    return {"runs": len(runs), "scenarios": per_scenario, "hypotheses": per_hypothesis}


def fmt(value, nd=2):
    return "—" if value is None else f"{value:.{nd}f}"


def fmt_ci(ci):
    return "—" if ci is None else f"[{ci[0]:.2f}, {ci[1]:.2f}]"


def render_markdown(agg):
    """Emit tables that can be pasted straight into the results chapter."""
    out = []
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out.append(f"# Resultados agregados\n")
    out.append(f"Execuções da suíte: **{agg['runs']}** — gerado em {stamp}.")
    out.append("Latências consideram apenas execuções em que o cenário passou; "
               "a taxa de sucesso é reportada separadamente. "
               "IC 95% pela distribuição t de Student (desvio amostral, n-1).\n")

    if agg["hypotheses"]:
        out.append("## Por hipótese\n")
        out.append("| Hipótese | Métrica | n | Média (s) | Desvio (s) | Mediana (s) | Mín | Máx | IC 95% |")
        out.append("|---|---|---:|---:|---:|---:|---:|---:|---|")
        for name, s in agg["hypotheses"].items():
            flag = " ⚠️" if s.get("heterogeneous") else ""
            out.append(
                f"| {name}{flag} | `{s['metric']}` | {s['n']} | {fmt(s['mean'])} | {fmt(s['sd'])} "
                f"| {fmt(s['median'])} | {fmt(s['min'])} | {fmt(s['max'])} | {fmt_ci(s['ci95'])} |"
            )
        out.append("")

        for name, s in agg["hypotheses"].items():
            if not s.get("heterogeneous"):
                continue
            parts = ", ".join(f"{k} ≈ {v:.1f}s" for k, v in
                              sorted(s["per_scenario_means"].items(), key=lambda kv: kv[1]))
            out.append(
                f"> ⚠️ **{name} agrega cenários de regimes diferentes** ({parts}). "
                f"Cada cenário isolado é estável — o maior desvio dentro de um único "
                f"cenário é {fmt(s['widest_within_scenario_sd'])}s, contra {fmt(s['sd'])}s "
                f"no agregado. O desvio agregado mede a distância ENTRE cenários, não a "
                f"variabilidade da medição, e a média agregada não descreve nenhum deles. "
                f"Reporte os cenários separadamente, ou justifique a agregação.\n")

    out.append("## Por cenário\n")
    out.append("| Cenário | Sucesso | Métrica | n | Média (s) | Desvio (s) | Mediana (s) | Mín | Máx | IC 95% |")
    out.append("|---|---|---|---:|---:|---:|---:|---:|---:|---|")
    for name, entry in agg["scenarios"].items():
        rate = f"{entry['pass']}/{entry['total']}"
        if not entry["metrics"]:
            out.append(f"| {name} | {rate} | — | — | — | — | — | — | — | — |")
            continue
        first = True
        for metric, s in entry["metrics"].items():
            label = name if first else ""
            shown = rate if first else ""
            first = False
            out.append(
                f"| {label} | {shown} | `{metric}` | {s['n']} | {fmt(s['mean'])} | {fmt(s['sd'])} "
                f"| {fmt(s['median'])} | {fmt(s['min'])} | {fmt(s['max'])} | {fmt_ci(s['ci95'])} |"
            )
    out.append("")

    singles = [
        f"{n}.{m}" for n, e in agg["scenarios"].items()
        for m, s in e["metrics"].items() if s and s["n"] == 1
    ]
    if singles:
        out.append("> **Atenção**: métricas com n=1 não têm desvio nem intervalo — "
                   "reporte-as como observação única, não como média: "
                   + ", ".join(f"`{s}`" for s in singles) + ".\n")

    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(
        description="Run the integration suite N times and aggregate the metrics")
    parser.add_argument("--runs", type=int, default=10, help="how many times to run the suite")
    parser.add_argument("--start-index", type=int, default=1, metavar="N",
                        help="number the first run N instead of 1, so a batch can be "
                             "added to an existing set without overwriting it "
                             "(runs accumulate; --aggregate-only re-reads them all)")
    parser.add_argument("--out", default="results", help="directory for raw and aggregated output")
    parser.add_argument("--test", help="restrict every run to one scenario (e.g. C1)")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--prefix", default="compliance-loop")
    parser.add_argument("--env", default="dev")
    parser.add_argument("--aggregate-only", action="store_true",
                        help="skip execution; re-aggregate the run-NN.json already in --out")
    parser.add_argument("--settle", type=int, default=30,
                        help="seconds between runs, so one run's CloudTrail events do not "
                             "spawn executions during the next (default 30)")
    args = parser.parse_args()

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)

    if not args.aggregate_only:
        extra = ["--region", args.region, "--prefix", args.prefix, "--env", args.env]
        if args.test:
            extra += ["--test", args.test]

        first, last = args.start_index, args.start_index + args.runs - 1
        print(f"Running the suite {args.runs}x (run-{first:02d}..run-{last:02d}) "
              f"-> {out_dir}\n", flush=True)
        for i in range(first, last + 1):
            run_suite(i, out_dir, extra)
            if i < last and args.settle:
                print(f"  settling {args.settle}s...", flush=True)
                time.sleep(args.settle)
        print()

    runs = load_runs(out_dir)
    if not runs:
        print(f"No run-NN.json found in {out_dir}", file=sys.stderr)
        sys.exit(1)

    agg = aggregate(runs)

    summary_path = os.path.join(out_dir, "summary.json")
    with open(summary_path, "w") as f:
        json.dump(agg, f, indent=2)

    report = render_markdown(agg)
    report_path = os.path.join(out_dir, "summary.md")
    with open(report_path, "w") as f:
        f.write(report + "\n")

    print(report)
    print(f"\nWrote {summary_path} and {report_path}")


if __name__ == "__main__":
    main()
