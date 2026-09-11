#!/usr/bin/env python3
"""
Turn the collected runs into the payload the results page embeds.

Keeps the page reproducible: re-run this after collecting more runs and paste
the output back in, rather than hand-editing numbers into the HTML.

Usage:
    python3 scripts/build_report_data.py                  # print JSON
    python3 scripts/build_report_data.py --out data.json
"""

import argparse
import glob
import json
import os
import statistics

# Which metric each scenario's headline number is, and what it means. Scenarios
# absent here still appear in the table; they just have no featured chart.
PRIMARY = {
    "C1": "t_remediation",
    "C3": "t_remediation",
    "C5": "t_remediation",
    "C2": "t_classification",
    "C6": "t_classification",
    "C4": "t_escalation",
    "C7": "t_sync_insert",
    "CTRL-01": "t_total",
    "CTRL-02": "t_total",
    "CTRL-03": "t_total",
    "CTRL-04": "t_total",
}

LABELS = {
    "C1": "Violação simples via console",
    "C2": "Violação com exceção ativa",
    "C3": "Exceção expira → remediação",
    "C4": "Falha de remediação → escalação",
    "C5": "Violação originada por IaC",
    "C6": "Recurso já conforme",
    "C7": "Novo controle → EventBridge",
    "CTRL-01": "ctrl-s3-public-block",
    "CTRL-02": "ctrl-s3-ssl-only",
    "CTRL-03": "ctrl-s3-versioning",
    "CTRL-04": "ctrl-s3-encryption",
    "AUDIT": "Ausência de falhas inesperadas",
}

T_95 = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
    8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
    15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
}


def summarize(values):
    n = len(values)
    if n == 0:
        return None
    mean = statistics.mean(values)
    if n == 1:
        return {"n": 1, "mean": mean, "sd": None, "median": mean,
                "min": mean, "max": mean, "ci95": None}
    sd = statistics.stdev(values)
    half = T_95.get(n - 1, 1.960) * sd / (n ** 0.5)
    return {"n": n, "mean": mean, "sd": sd, "median": statistics.median(values),
            "min": min(values), "max": max(values),
            "ci95": [mean - half, mean + half]}


def build(results_dir):
    files = sorted(glob.glob(os.path.join(results_dir, "run-*.json")))
    runs = [json.load(open(f)) for f in files]

    scenarios, observations = {}, {}
    for payload in runs:
        for name, verdict in payload.get("results", {}).items():
            entry = scenarios.setdefault(name, {"pass": 0, "total": 0})
            entry["total"] += 1
            if verdict == "PASS":
                entry["pass"] += 1
            metrics = (payload.get("metrics") or {}).get(name) or {}
            for metric, value in metrics.items():
                if verdict == "PASS" and isinstance(value, (int, float)):
                    observations.setdefault(name, {}).setdefault(metric, []).append(float(value))

    out_scenarios = []
    for name in sorted(scenarios, key=lambda k: (k.startswith("CTRL"), k)):
        mets = observations.get(name, {})
        out_scenarios.append({
            "id": name,
            "label": LABELS.get(name, name),
            "pass": scenarios[name]["pass"],
            "total": scenarios[name]["total"],
            "primary": PRIMARY.get(name),
            "metrics": {m: {"values": v, **summarize(v)} for m, v in sorted(mets.items())},
        })

    # t_exposure decomposes into detection + remediation; carried per run so the
    # page can show where the time actually goes instead of only the total.
    composition = []
    for i, payload in enumerate(runs, 1):
        row = {"run": i}
        for cen in ("C1", "C5"):
            m = (payload.get("metrics") or {}).get(cen) or {}
            if payload["results"].get(cen) == "PASS" and "t_detection" in m:
                row[cen] = {"detection": m["t_detection"],
                            "remediation": m["t_remediation"],
                            "exposure": m["t_exposure"]}
        composition.append(row)

    pooled = {}
    for hyp, metric, cens in (("H1", "t_remediation", ("C1", "C3", "C5")),
                              ("H3", "t_exposure", ("C1", "C5"))):
        vals = [v for c in cens for v in observations.get(c, {}).get(metric, [])]
        pooled[hyp] = {"metric": metric, "scenarios": list(cens),
                       "per_scenario": {c: summarize(observations.get(c, {}).get(metric, []))
                                        for c in cens},
                       **(summarize(vals) or {})}

    return {"runs": len(runs), "scenarios": out_scenarios,
            "composition": composition, "hypotheses": pooled}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="results")
    ap.add_argument("--out")
    args = ap.parse_args()
    data = build(args.results)
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")
        print(f"wrote {args.out} ({data['runs']} runs)")
    else:
        print(text)


if __name__ == "__main__":
    main()
