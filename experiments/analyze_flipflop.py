#!/usr/bin/env python3
# analyze_flipflop.py -- OFFLINE analysis of lqr_orig's behaviour in exp43 / exp44 (no sudo, nothing run on the host,
# only reads files): is the faithful port of the authors' controller (paper K, dt 0.5 s) a CPU-target tracker or a
# period-2 relay that flips drop between 0.95 and 0?
# Reads: ts_dyn43.csv, results_dyn43.csv, ref_victim43.txt (exp43: steady4, lqr_orig on the four flooders,
#        T40 / T60 / T80 paper K and T60 ZOH K), ts_dyn44.csv, results_dyn44.csv, ref_victim44.txt (exp44: S1 / S2 with
#        lqr_orig on the active flooders, S3 on all six tenants, T40 / T60 paper K), calib42_result.json (cpu_ref ->
#        targets), and the scenario windows from exp43.py / exp44.py (imported, nothing executed).
# Per controlled tenant, over the controller steps (ctl = 1) inside its window (flooders: their iperf3 window; victims
#   OFFSET + 1 <= t < OFFSET + T):
#   duty      fraction of steps with u > 0
#   u dist    fractions at u = 0, 0 < u < 0.95, u = 0.95
#   alt       fraction of consecutive step pairs where (u > 0) flips -- 1.0 = a perfect period-2 relay
#   ubelow    fraction of steps with u > 0 while the CPU sample of that step (vhost CPU over the step's 0.5 s window
#             = tick-weighted mean of its two 0.25 s ticks, what lqr_orig was fed) < target; ubelow_eff: same with
#             the controller's own CPU_eff; p(below|u>0) = ubelow / duty
#   CPU       mean vhost CPU over all ticks in the window, mean CPU_eff over the steps, both against the target
# Per repeat: T40 vs T60 (and T80 in exp43): flooder total Gbit/s, victims' slo_ok_worst, flooder duty and CPU.
# Writes flipflop_table.csv (one row per source / scenario / param / tenant, plus pooled flooder and victim rows) and
# flipflop_fig.png (exp44 S3, one repeat, T40 and T60: u and CPU of victim V2 and flooder A over time).
# usage: python3 analyze_flipflop.py [--rep N]   (repeat for the figure, default 1)
import sys
sys.dont_write_bytecode = True
import csv, json, math, os, statistics as st, collections
HERE = os.path.dirname(os.path.abspath(__file__))
sys_argv, sys.argv = sys.argv, sys.argv[:1]          # exp43/exp44 read sys.argv[1] at import
import exp43, exp44
sys.argv = sys_argv
FIG_REP = int(sys.argv[sys.argv.index("--rep") + 1]) if "--rep" in sys.argv else 1
nan = float("nan")
TABLE = os.path.join(HERE, "flipflop_table.csv")
FIG = os.path.join(HERE, "flipflop_fig.png")
UMAX = 0.95
OFFSET, T = exp44.OFFSET, exp44.T
CPU_REF = float(json.load(open(os.path.join(HERE, "calib42_result.json")))["cpu_ref"])
VICTIMS, FLOODERS = exp44.VICTIMS, exp44.FLOODERS


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return nan


def target_of(param):
    return float(param.split("_")[0][1:]) / 100.0 * CPU_REF


def windows(source, scen):
    w = exp43.STEADY["steady4"] if source == "exp43" else exp44.SCENARIOS[scen]["win"]
    out = {g: (float(a), float(a + d)) for g, (a, d) in w.items()}
    out.update({v: (OFFSET + 1.0, OFFSET + T) for v in VICTIMS})
    return out


def load_slots(path, source):
    # {(source, scenario, rep, slot, param): [rows sorted by t]} for lqr_orig slots
    slots = collections.defaultdict(list)
    with open(path) as fh:
        for r in csv.DictReader(fh):
            if r["mode"] != "lqr_orig":
                continue
            scen = r.get("scenario", "steady4")
            slots[(source, scen, int(r["rep"]), int(r["slot"]), r["param"])].append(r)
    for k in slots:
        slots[k].sort(key=lambda r: f(r["t"]))
    return slots


def tenant_steps(rows, g, win):
    # per controller step of tenant g inside win: (t, u, cpu sample of the step, cpu_eff); plus all tick CPUs in win
    a, b = win
    steps, ticks = [], []
    for i, r in enumerate(rows):
        t = f(r["t"]); vh = f(r.get(f"vh{g}_cpu"))
        if a <= t < b and not math.isnan(vh):
            ticks.append(vh)
        if r["ctl"] != "1" or not (a <= t < b):
            continue
        u = f(r.get(f"u{g}"))
        if math.isnan(u):
            continue
        # what lqr_orig was fed: vhost CPU over the 0.5 s since its last step = tick-weighted mean of this tick and
        # the previous one
        if i == 0:
            continue
        w1, w0 = f(r["tick_s"]), f(rows[i - 1]["tick_s"])
        c = (vh * w1 + f(rows[i - 1][f"vh{g}_cpu"]) * w0) / max(1e-9, w1 + w0)
        steps.append((t, u, c, f(r.get(f"cpueff{g}"))))
    return steps, ticks


def metrics(steps, ticks, target):
    n = len(steps)
    if not n:
        return None
    us = [s[1] for s in steps]
    pos = [u > 1e-9 for u in us]
    flips = sum(a != b for a, b in zip(pos, pos[1:]))
    ub = sum(p and s[2] < target for p, s in zip(pos, steps))
    ube = sum(p and s[3] < target for p, s in zip(pos, steps))
    duty = sum(pos) / n
    return {"n_steps": n, "duty": duty, "u0": sum(u <= 1e-9 for u in us) / n,
            "umid": sum(1e-9 < u < UMAX - 1e-9 for u in us) / n, "u095": sum(u >= UMAX - 1e-9 for u in us) / n,
            "u_mean": st.mean(us), "alt": flips / max(1, n - 1), "ubelow": ub / n, "ubelow_eff": ube / n,
            "p_below_given_u": (ub / sum(pos)) if sum(pos) else nan,
            "cpu_mean": st.mean(ticks) if ticks else nan, "eff_mean": st.mean(s[3] for s in steps),
            "target": target, "cpu_over_target": (st.mean(ticks) / target) if ticks else nan,
            "cpu_step_p10": sorted(s[2] for s in steps)[n // 10], "cpu_step_p90": sorted(s[2] for s in steps)[9 * n // 10],
            "eff_min": min(s[3] for s in steps), "eff_max": max(s[3] for s in steps)}


def pool(ms):
    # step-weighted pooling of per-slot metric dicts
    ms = [m for m in ms if m]
    if not ms:
        return None
    N = sum(m["n_steps"] for m in ms)
    out = {"n_steps": N}
    for k in ms[0]:
        if k == "n_steps":
            continue
        vals = [(m[k], m["n_steps"]) for m in ms if not math.isnan(m[k])]
        if k == "eff_min":
            out[k] = min(v for v, _ in vals)
        elif k == "eff_max":
            out[k] = max(v for v, _ in vals)
        else:
            out[k] = sum(v * w for v, w in vals) / sum(w for _, w in vals) if vals else nan
    out["p_below_given_u"] = out["ubelow"] / out["duty"] if out["duty"] > 0 else nan
    return out


def read_results(path, source):
    out = {}
    with open(path) as fh:
        for r in csv.DictReader(fh):
            out[(source, r.get("scenario", "steady4"), int(r["rep"]), r["mode"], r["param"])] = r
    return out


def read_ref44(path):
    # {(rep, scenario, victim): unregulated vhost CPU % of one core} from ref_victim44.txt
    out = {}
    for line in open(path):
        if line.startswith("#") or not line.strip():
            continue
        p = [x.strip() for x in line.split(",")]
        out[(int(p[0]), p[1], p[3])] = float(p[9])
    return out


def read_ref43(path):
    out = {}
    for line in open(path):
        if line.startswith("#") or not line.strip():
            continue
        p = [x.strip() for x in line.split(",")]
        out[(int(p[0]), p[2])] = float(p[4])
    return out


def main():
    slots = {}
    slots.update(load_slots(os.path.join(HERE, "ts_dyn43.csv"), "exp43"))
    slots.update(load_slots(os.path.join(HERE, "ts_dyn44.csv"), "exp44"))
    res = {}
    res.update(read_results(os.path.join(HERE, "results_dyn43.csv"), "exp43"))
    res.update(read_results(os.path.join(HERE, "results_dyn44.csv"), "exp44"))
    ref44 = read_ref44(os.path.join(HERE, "ref_victim44.txt"))
    ref43 = read_ref43(os.path.join(HERE, "ref_victim43.txt"))

    per_slot = collections.defaultdict(list)    # (source, scen, param, tenant) -> [metrics per slot]
    per_rep = {}                                # (source, scen, param, rep) -> {tenant: metrics}
    for (src, scen, rep, slot, param), rows in slots.items():
        tgt = target_of(param)
        wins = windows(src, scen)
        per_rep[(src, scen, param, rep)] = {}
        for g in VICTIMS + FLOODERS:
            if f"u{g}" not in rows[0] or g not in wins:   # exp43's ts has no victim columns
                continue
            steps, ticks = tenant_steps(rows, g, wins[g])
            m = metrics(steps, ticks, tgt)
            if m:
                per_slot[(src, scen, param, g)].append(m)
                per_rep[(src, scen, param, rep)][g] = m

    keys = sorted({k[:3] for k in per_slot}, key=lambda k: (k[0], ["steady4", "S1", "S2", "S3"].index(k[1]), k[2]))
    table = []
    for src, scen, param in keys:
        for g in VICTIMS + FLOODERS:
            if (src, scen, param, g) in per_slot:
                m = pool(per_slot[(src, scen, param, g)])
                table.append((src, scen, param, g, len(per_slot[(src, scen, param, g)]), m))
        for grp, members in (("flooders", FLOODERS), ("victims", VICTIMS)):
            ms = [m for g in members for m in per_slot.get((src, scen, param, g), [])]
            if ms:
                table.append((src, scen, param, grp, len(ms), pool(ms)))

    cols = ["n_steps", "duty", "u0", "umid", "u095", "u_mean", "alt", "ubelow", "ubelow_eff", "p_below_given_u",
            "cpu_mean", "eff_mean", "target", "cpu_over_target", "cpu_step_p10", "cpu_step_p90", "eff_min", "eff_max"]
    with open(TABLE, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["source", "scenario", "param", "tenant", "n_slots"] + cols)
        for src, scen, param, g, ns, m in table:
            w.writerow([src, scen, param, g, ns] + [f"{m[c]:.4f}" if isinstance(m[c], float) else m[c] for c in cols])

    print("FLIP-FLOP TABLE (pooled over repeats, controller steps inside each tenant's window; duty = P(u > 0); "
          "u0 / umid / u095 = P(u = 0) / P(0 < u < 0.95) / P(u = 0.95); alt = P(u > 0 flips between consecutive "
          "steps), 1.0 = period-2 relay; ubelow = P(u > 0 AND the step's CPU sample < target); p|u = P(CPU < target | "
          "u > 0); CPU = mean vhost CPU % of one core over the window; eff range = min / max CPU_eff)")
    print(f"{'src':5s} {'scen':7s} {'param':10s} {'tenant':8s} {'steps':>5s} {'duty':>5s} {'u0':>5s} {'umid':>5s} "
          f"{'u095':>5s} {'alt':>5s} {'ubelow':>6s} {'p|u':>5s} {'CPU':>6s} {'target':>6s} {'CPU/t':>5s} "
          f"{'eff range':>15s}")
    for src, scen, param, g, ns, m in table:
        if g not in ("flooders", "victims") and g not in ("A", "V2"):
            continue   # the console shows the pooled groups plus V2 and A; every tenant is in the CSV
        print(f"{src:5s} {scen:7s} {param:10s} {g:8s} {m['n_steps']:5d} {m['duty']:5.3f} {m['u0']:5.3f} "
              f"{m['umid']:5.3f} {m['u095']:5.3f} {m['alt']:5.3f} {m['ubelow']:6.3f} {m['p_below_given_u']:5.3f} "
              f"{m['cpu_mean']:6.1f} {m['target']:6.2f} {m['cpu_over_target']:5.2f} "
              f"{m['eff_min']:7.1f}/{m['eff_max']:6.1f}")

    # u distribution in detail (pooled flooders and victims)
    print("\nU DISTRIBUTION (fraction of controller steps per drop level; levels with < 0.5 % omitted)")
    for src, scen, param in keys:
        for grp, members in (("flooders", FLOODERS), ("victims", VICTIMS)):
            us = []
            for (s_, sc_, r_, sl_, p_), rows in slots.items():
                if (s_, sc_, p_) != (src, scen, param):
                    continue
                wins = windows(src, scen)
                for g in members:
                    if g in wins and f"u{g}" in rows[0]:
                        us += [st_[1] for st_ in tenant_steps(rows, g, wins[g])[0]]
            if not us:
                continue
            c = collections.Counter(round(u, 2) for u in us)
            print(f"  {src} {scen:7s} {param:10s} {grp:8s} n={len(us):5d}: " + "  ".join(
                f"{lv:.2f}:{c[lv] / len(us):.3f}" for lv in sorted(c) if c[lv] / len(us) >= 0.005))

    # victims' unregulated demand vs target (exp44 references), exp43 victim references for completeness
    print("\nVICTIM DEMAND vs TARGET (exp44 references: victims alone at the scenario's rates, vhost CPU % of one core; "
          "T40 / T60 targets " + f"{target_of('T40_x'):.2f} / {target_of('T60_x'):.2f} %)")
    for scen in ("S1", "S2", "S3"):
        for v in VICTIMS:
            xs = [x for (r, s, vv), x in ref44.items() if s == scen and vv == v]
            if xs:
                print(f"  {scen} {v}: {st.mean(xs):.1f} % (min {min(xs):.1f}, max {max(xs):.1f}, n={len(xs)}) = "
                      f"{st.mean(xs) / target_of('T40_x'):.2f} x T40, {st.mean(xs) / target_of('T60_x'):.2f} x T60")
    print("  exp43 references (rates only, no vhost column): " + ", ".join(
        f"{v} {st.mean(x for (r, vv), x in ref43.items() if vv == v):.3f} Gbit/s" for v in VICTIMS))

    # T40 vs T60 (vs T80) per repeat
    print("\nT40 vs T60 PER REPEAT (fl_total = flooder eBPF Gbit/s summed over the active flooders; slo = slo_ok_worst; "
          "duty / CPU = pooled over the controlled flooders; d = T60 - T40)")
    for src, scen in sorted({(k[0], k[1]) for k in keys}, key=lambda k: (k[0], ["steady4", "S1", "S2", "S3"].index(k[1]))):
        params = [p for p in ("T40_paperK", "T60_paperK", "T80_paperK") if (src, scen, p) in keys]
        reps = sorted({k[3] for k in per_rep if k[:3] == (src, scen, params[0])})
        print(f"  {src} {scen}: " + " | ".join(params))
        dif = collections.defaultdict(list)
        for rep in reps:
            cells = []
            vals = {}
            for p in params:
                r = res.get((src, scen, rep, "lqr_orig", p))
                fm = pool([per_rep.get((src, scen, p, rep), {}).get(g) for g in FLOODERS])
                vals[p] = (f(r["flooder_total_gbps"]) if r else nan, f(r["slo_ok_worst"]) if r else nan,
                           fm["duty"] if fm else nan, fm["cpu_mean"] if fm else nan)
                cells.append(f"fl_total {vals[p][0]:6.2f} slo {vals[p][1]:5.3f} duty {vals[p][2]:5.3f} "
                             f"CPU {vals[p][3]:5.1f}")
            for i, name in enumerate(("fl_total", "slo", "duty", "CPU")):
                dif[name].append(vals["T60_paperK"][i] - vals["T40_paperK"][i])
            print(f"    rep {rep}: " + " | ".join(cells))
        line = []
        for name, ds in dif.items():
            ds = [d for d in ds if not math.isnan(d)]
            if ds:
                wins = sum(d > 1e-9 for d in ds); loss = sum(d < -1e-9 for d in ds)
                line.append(f"{name} {st.mean(ds):+.3f} +- {st.stdev(ds) if len(ds) > 1 else 0:.3f} "
                            f"(T60 higher/equal/lower {wins}/{len(ds) - wins - loss}/{loss})")
        print("    d (T60 - T40): " + "; ".join(line))

    make_fig(slots)
    print(f"\nwrote {TABLE} ({len(table)} rows) and {FIG} (exp44 S3 rep {FIG_REP})")


def make_fig(slots):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    SURF, INK, INK2, GRID = "#fcfcfb", "#1f1f1e", "#5f5e58", "#e4e3de"
    COL = {"V2": "#2a78d6", "A": "#eb6834"}          # categorical slots 1 and 2 (fixed order)
    LBL = {"V2": "victim V2", "A": "flooder A"}
    T0, T1 = 10.0, 20.0                               # zoom: 20 controller steps
    fig, axes = plt.subplots(3, 2, figsize=(11, 7.4), sharex=True, facecolor=SURF,
                             gridspec_kw={"height_ratios": [1, 1, 1.6]})
    for j, param in enumerate(("T40_paperK", "T60_paperK")):
        ks = [k for k in slots if k[0] == "exp44" and k[1] == "S3" and k[2] == FIG_REP and k[4] == param]
        if not ks:
            raise SystemExit(f"no exp44 S3 rep {FIG_REP} {param} slot in ts_dyn44.csv")
        rows = slots[ks[0]]
        tgt = target_of(param)
        for i, g in enumerate(("V2", "A")):
            steps, _ = tenant_steps(rows, g, (T0, T1))
            ts_ = [s_[0] for s_ in steps]
            axes[i][j].step(ts_, [s_[1] for s_ in steps], where="post", color=COL[g], lw=2)
            axes[2][j].step(ts_, [s_[2] for s_ in steps], where="post", color=COL[g], lw=2, label=LBL[g])
            axes[i][j].set_ylim(-0.05, 1.05)
            axes[i][j].set_yticks([0, 0.5, 0.95])
            if j == 0:
                axes[i][j].set_ylabel(f"u, {LBL[g]}", color=INK2, fontsize=9)
        axes[2][j].axhline(tgt, color=INK2, lw=1.2, ls=(0, (4, 3)))
        axes[2][j].text(T1 - 0.1, tgt + 2, f"target {tgt:.1f} %", ha="right", va="bottom", color=INK2, fontsize=9)
        axes[2][j].set_ylim(0, 105)
        axes[2][j].set_xlabel("time since flooder launch (s)", color=INK2, fontsize=9)
        axes[0][j].set_title(f"lqr_orig {param.split('_')[0]} (paper K), exp44 S3 rep {FIG_REP}", color=INK,
                             fontsize=11, loc="left")
    for ax in axes.flat:
        ax.set_facecolor(SURF)
        ax.grid(True, color=GRID, lw=0.8)
        ax.set_axisbelow(True)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        for sp in ("left", "bottom"):
            ax.spines[sp].set_color(GRID)
        ax.tick_params(colors=INK2, labelsize=9)
        ax.set_xlim(T0, T1)
    axes[2][0].set_ylabel("vhost CPU fed to the controller\n(% of one core, 0.5 s window)", color=INK2, fontsize=9)
    h, l = axes[2][0].get_legend_handles_labels()
    fig.legend(h, l, loc="upper right", ncol=2, frameon=False, fontsize=9, labelcolor=INK, bbox_to_anchor=(0.99, 0.965))
    fig.suptitle("lqr_orig flips drop between 0.95 and 0 on alternate 0.5 s steps (victim and flooder in anti-phase)",
                 color=INK, fontsize=12, x=0.01, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(FIG, dpi=130, facecolor=SURF)


if __name__ == "__main__":
    main()
