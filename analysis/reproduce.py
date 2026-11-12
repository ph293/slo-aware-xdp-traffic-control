#!/usr/bin/env python3
"""reproduce.py -- rebuild the paper's tables and figures from the raw data in ../data (offline, no sudo, no testbed).

usage:  python3 analysis/reproduce.py [--out analysis/output]

Needs: python3 with numpy, scipy, pandas, matplotlib (see ../requirements.txt).

What it writes into the output folder:
  summary_exp34.txt ... summary_exp45.txt   per-experiment summary tables (exp34-39: the SUMMARY block of the run log;
                                            exp40-45: recomputed from results_dynNN.csv by expNN.py --summary)
  main_results.csv                          mean and sd over repeats of the SLO, flooder total and Jain fairness per
                                            experiment / scenario / controller (exp40-45)
  fig_frontier_exp43.png                    SLO against flooder throughput, exp43 (4 flooders, steady)
  fig_scenarios_exp45.png                   SLO and flooder throughput per scenario (S1, S2, S3), exp45
  flipflop_table.csv, flipflop_fig.png      flip-flop analysis of the faithful port (analyze_flipflop.py)

The experiment scripts read their inputs from their own folder, so the data and code are copied into a temporary
folder first; nothing in the repository is modified.
"""
import argparse, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CODE = os.path.join(ROOT, "experiments")
DATA = os.path.join(ROOT, "data")


def run(cmd, cwd, out_path=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=900)
    if out_path:
        with open(out_path, "w") as f:
            f.write(r.stdout)
    if r.returncode != 0:
        sys.stderr.write(f"FAILED {' '.join(cmd)}\n{r.stderr[-800:]}\n")
    return r.returncode


def summary_block_from_log(log_path, out_path):
    """exp34-39 print their summary at the end of the run log, starting at the line 'SUMMARY'."""
    with open(log_path, errors="replace") as f:
        lines = f.read().splitlines()
    idx = [i for i, l in enumerate(lines) if l.startswith("SUMMARY")]
    if not idx:
        return False
    with open(out_path, "w") as f:
        f.write("\n".join(lines[idx[0]:]) + "\n")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "output"))
    args = ap.parse_args()
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)

    import pandas as pd
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    with tempfile.TemporaryDirectory() as work:
        for src in (CODE, DATA):
            for name in os.listdir(src):
                p = os.path.join(src, name)
                if os.path.isfile(p):
                    shutil.copy(p, work)

        # 1. summaries
        for n in range(34, 40):
            ok = summary_block_from_log(os.path.join(work, f"exp{n}.log"), os.path.join(out, f"summary_exp{n}.txt"))
            print(f"exp{n}: summary {'extracted from the log' if ok else 'NOT FOUND'}")
        for n in range(40, 46):
            rc = run([sys.executable, "-I", "-B", f"exp{n}.py", "--summary", f"results_dyn{n}.csv"], work,
                     os.path.join(out, f"summary_exp{n}.txt"))
            print(f"exp{n}: summary {'recomputed' if rc == 0 else 'FAILED'}")

        # 2. flip-flop analysis
        rc = run([sys.executable, "-B", "analyze_flipflop.py"], work)
        for f in ("flipflop_table.csv", "flipflop_fig.png"):
            if os.path.exists(os.path.join(work, f)):
                shutil.copy(os.path.join(work, f), out)
        print("flip-flop analysis:", "done" if rc == 0 else "FAILED")

    # 3. main results table (exp40-45)
    rows = []
    for n in range(40, 46):
        d = pd.read_csv(os.path.join(DATA, f"results_dyn{n}.csv"))
        d = d[d["mode"] != "alone"].copy()
        if "scenario" not in d.columns:
            d["scenario"] = "steady4"
        for (scen, mode, param), g in d.groupby(["scenario", "mode", "param"]):
            per_rep = g.groupby("rep").mean(numeric_only=True)
            row = {"experiment": f"exp{n}", "scenario": scen, "mode": mode, "param": param, "repeats": len(per_rep)}
            for col, key in (("slo_ok_worst", "slo"), ("flooder_total_gbps", "flooder_total_gbps"),
                             ("jain_fairness", "jain"), ("min_flooder_own_gbps", "min_flooder_gbps")):
                if col in per_rep:
                    row[key + "_mean"] = per_rep[col].mean()
                    row[key + "_sd"] = per_rep[col].std()
            rows.append(row)
    table = pd.DataFrame(rows)
    table.round(4).to_csv(os.path.join(out, "main_results.csv"), index=False)
    print("main_results.csv:", len(table), "rows")

    # 4. figures
    colors = {"vfrot": "#0072B2", "lqr_orig": "#D55E00", "lqr": "#009E73", "hyb": "#CC79A7", "vffair": "#E69F00"}
    t43 = table[table["experiment"] == "exp43"]
    fig, ax = plt.subplots(figsize=(6.4, 4.2))
    for _, r in t43.iterrows():
        ax.errorbar(r["flooder_total_gbps_mean"], r["slo_mean"], xerr=r["flooder_total_gbps_sd"], yerr=r["slo_sd"],
                    fmt="o", color=colors.get(r["mode"], "gray"), capsize=2, label=r["mode"])
        ax.annotate(str(r["param"]).replace("0.8_1_2_0.94", "").strip("_") or "", (r["flooder_total_gbps_mean"], r["slo_mean"]),
                    textcoords="offset points", xytext=(5, 4), fontsize=7)
    h, l = ax.get_legend_handles_labels()
    uniq = dict(zip(l, h))
    ax.legend(uniq.values(), uniq.keys(), fontsize=8, frameon=False)
    ax.set_xlabel("Flooder throughput (Gbit/s, mean and sd over 6 repeats)")
    ax.set_ylabel("Victim SLO (share of good 1-s windows)")
    ax.set_title("exp43: four flooders, steady load")
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(os.path.join(out, "fig_frontier_exp43.png"), dpi=200)
    plt.close(fig)

    t5 = table[table["experiment"] == "exp45"].copy()
    names = {("vfrot", "0.8_1_2_0.94"): "vfrot", ("lqr_orig", "T40_paperK"): "authors\nT40",
             ("lqr_orig", "T60_paperK"): "authors\nT60", ("hyb", "T60_paperK"): "hybrid"}
    t5["label"] = [names.get((m, p), f"{m}\n{p}") for m, p in zip(t5["mode"], t5["param"])]
    order = ["vfrot", "authors\nT40", "authors\nT60", "hybrid"]
    fig, axes = plt.subplots(2, 3, figsize=(10, 5.6), sharey="row")
    for j, scen in enumerate(["S1", "S2", "S3"]):
        g = t5[t5["scenario"] == scen].set_index("label").reindex(order)
        for i, (col, sd, ylab) in enumerate((("slo_mean", "slo_sd", "Victim SLO"),
                                             ("flooder_total_gbps_mean", "flooder_total_gbps_sd", "Flooder total (Gbit/s)"))):
            ax = axes[i][j]
            ax.bar(range(len(order)), g[col], yerr=g[sd], capsize=3, width=0.7,
                   color=[colors["vfrot"], colors["lqr_orig"], colors["lqr_orig"], colors["hyb"]])
            ax.set_xticks(range(len(order)))
            ax.set_xticklabels(order, fontsize=8)
            if i == 0:
                ax.set_title({"S1": "S1: one flooder", "S2": "S2: two flooders", "S3": "S3: heavy victims, all tenants controlled"}[scen], fontsize=9)
            if j == 0:
                ax.set_ylabel(ylab)
            ax.grid(axis="y", alpha=0.25)
    fig.suptitle("exp45: mean and sd over 6 repeats", fontsize=10)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(os.path.join(out, "fig_scenarios_exp45.png"), dpi=200)
    plt.close(fig)
    print("figures written to", out)


if __name__ == "__main__":
    main()
