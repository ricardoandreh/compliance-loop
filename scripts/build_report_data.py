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
    "C8": "t_escalation",
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
    "C4": "Falha do sistema → escalação",
    "C8": "Remediação falha → escalação",
    "C5": "Violação originada por IaC",
    "C6": "Recurso já conforme",
    "C7": "Novo controle → EventBridge",
    "CTRL-01": "ctrl-s3-public-block",
    "CTRL-02": "ctrl-s3-ssl-only",
    "CTRL-03": "ctrl-s3-versioning",
    "CTRL-04": "ctrl-s3-encryption",
    "AUDIT": "Ausência de falhas inesperadas",
}

# Same table as scripts/benchmark.py. It used to stop at 20 degrees of freedom,
# so every n > 21 silently fell back to the normal 1.960 instead of the t value
# (2.045 at n = 30) and published intervals slightly narrower than the data
# supports. Keep the two tables identical.
PRE_EXECUTION_METRICS = {"t_detection", "t_scheduler_fire"}

T_95 = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
    8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
    15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060, 26: 2.056,
    27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
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

    # Cold/warm classification from scripts/classify_cold_starts.py, keyed by
    # (run index, scenario). It is evidence-based (Init Duration on the exact
    # Lambda invocation), so the page never has to guess from a duration.
    cold_path = os.path.join(results_dir, "cold_starts.json")
    classified = None
    if os.path.exists(cold_path):
        doc = json.load(open(cold_path))
        if doc["summary"]["problems"]:
            raise SystemExit(f"{cold_path} reports problems; fix them before building: "
                             f"{doc['summary']['problems']}")
        index = {os.path.basename(f): i for i, f in enumerate(files, 1)}
        classified = {(index[x["run"]], x["scenario"]): x["cold"] for x in doc["samples"]}

    def is_cold(run, scenario):
        return None if classified is None else classified.get((run, scenario))

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
                            "exposure": m["t_exposure"],
                            "verified": m.get("t_verified"),
                            "cold": is_cold(i, cen)}
        composition.append(row)

    # Per-sample view with the class attached. t_exposure and t_verified of
    # C1/C5 come from the same detection execution as that run's remediation,
    # so they inherit its class; t_detection precedes any Lambda and has none.
    samples = []
    for i, payload in enumerate(runs, 1):
        for cen, m in (payload.get("metrics") or {}).items():
            if payload["results"].get(cen) != "PASS":
                continue
            for metric, value in m.items():
                if isinstance(value, (int, float)):
                    # A container's state can only affect metrics that include a
                    # Lambda invocation. Detection and the scheduler's fire time
                    # are over before the execution starts, so they get no class
                    # — splitting them would attribute CloudTrail or Scheduler
                    # delay to a cold start that had not happened yet.
                    cls = None if metric in PRE_EXECUTION_METRICS else is_cold(i, cen)
                    samples.append({"run": i, "scenario": cen, "metric": metric,
                                    "value": float(value), "cold": cls})

    def split(metric, cens):
        rows = [x for x in samples if x["metric"] == metric and x["scenario"] in cens]
        vals = lambda pred: [x["value"] for x in rows if pred(x)]
        out = {"metric": metric, "scenarios": list(cens),
               "all": summarize(vals(lambda x: True)),
               "per_scenario": {c: summarize(vals(lambda x, c=c: x["scenario"] == c)) for c in cens}}
        if classified is not None:
            out["warm"] = summarize(vals(lambda x: x["cold"] is False))
            out["cold"] = summarize(vals(lambda x: x["cold"] is True))
            out["cold_samples"] = [{"run": x["run"], "scenario": x["scenario"], "value": x["value"]}
                                   for x in rows if x["cold"]]
        return out

    pooled = {
        "H1": split("t_remediation", ("C1", "C3", "C5")),
        "H3": split("t_exposure", ("C1", "C5")),
        "verified": split("t_verified", ("C1", "C5")),
        "detection": {"metric": "t_detection", "scenarios": ["C1", "C5"],
                      "all": summarize([x["value"] for x in samples
                                        if x["metric"] == "t_detection" and x["scenario"] in ("C1", "C5")])},
    }

    # Every scenario and metric, split where the sample carries a class. Metrics
    # of unclassified scenarios (CTRL-*, C7) report "all" only — they time the
    # whole test, so there is no single invocation to classify.
    by_scenario = {}
    for x in samples:
        by_scenario.setdefault(x["scenario"], {}).setdefault(x["metric"], []).append(x)
    for cen, metrics in by_scenario.items():
        for metric, rows in metrics.items():
            entry = {"all": summarize([r["value"] for r in rows])}
            if any(r["cold"] is not None for r in rows):
                entry["warm"] = summarize([r["value"] for r in rows if r["cold"] is False])
                entry["cold"] = summarize([r["value"] for r in rows if r["cold"] is True])
            metrics[metric] = entry

    return {"runs": len(runs), "classified": classified is not None,
            "by_scenario": by_scenario,
            "scenarios": out_scenarios, "composition": composition,
            "samples": samples, "hypotheses": pooled}


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
