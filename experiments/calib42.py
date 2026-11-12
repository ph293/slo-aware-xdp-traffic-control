import sys, os, time, math, json, statistics as st, tempfile, subprocess
import numpy as np
import exp42 as E
# calib42: plant identification and pilot for exp42's lqr_orig (~2.5 min), started by calibrate42.sh. Writes ONLY
# calib42_* files: calib42_run.log (via calibrate42.sh), calib42_ts.csv, calib42_result.json (read by exp42.py,
# which refuses to start without it or if its verdict is FAIL). Victims V1 + V2 at 9 Gbit/s and all four flooders
# (2 TCP streams each) run in both phases; per-flooder CPU = its vhost thread (exp42.vhost_ns) sampled every 0.1 s;
# xdp_dropfrac.o on the 4 flooders for the whole calibration, xdp_limit.o back at the end.
#   S steps: u = 0 for SETTLE_T s, then per flooder A, B, C, D: u = 0.5 for STEP_ON s, u = 0 for STEP_OFF s
#            -> first-order + dead-time fit of each up and each down step: tau, DC gain G (CPU points per unit u)
#   M map:   all four flooders at u = 0, 0.05, 0.1, 0.2, 0.5 for LEVEL_T s each (first LEVEL_SKIP s discarded);
#            u = 0.5 is HELD while the flooder iperf3 clients end (ssh / iperf3 -J survival at u = 0.5)
# The plant for exp42: tau = median of the 8 fitted tau, G = median of the 8 gains, b = G / tau (so the DC gain
# b x tau of the authors' model A = [[1, dt], [-dt/tau, 1 - dt/tau]], B = [[0], [-b dt]] equals the measured one);
# cpu_ref = median over flooders of the mean vhost CPU at u = 0 in M (all four active, unregulated).
HERE = os.path.dirname(os.path.abspath(__file__))
TS = os.path.join(HERE, "calib42_ts.csv")
RESULT = E.CALIB                        # calib42_result.json
LOG_NAME = "calib42_run.log"            # created by calibrate42.sh (tee); checked there
CAL_DT = 0.1             # s, vhost CPU sampling period
SETTLE_T = 6.0           # s, S: all u = 0 before the first step
STEP_ON, STEP_OFF = 8.0, 6.0
STEP_BP = 5000           # step size 0 -> 0.5
FIT_PRE = 3.0            # s of pre-step samples in each fit window
THETAS = tuple(0.1 * i for i in range(11))           # dead time grid, s
TAUS = tuple(float(x) for x in np.geomspace(0.05, 10.0, 120))   # time constant grid, s
LEVELS_BP = (0, 500, 1000, 2000, 5000)
LEVEL_T, LEVEL_SKIP = 6.0, 2.0
STEADY_WIN = (2.0, SETTLE_T)            # S window used for the noise check (besides M's u = 0 level)
CAL_WAIT_S = 60.0        # s, timeout of the flooder waits (FAIL if reached at u = 0.5)
CAL_TAIL = 1             # s, the iperf3 clients run this much longer than the sampled phase (last window inside)
TARGET_FRACS = (0.4, 0.6, 0.8)          # exp42 lqr_orig T40 / T60 / T80
LQR_TOTALS = (8.8, 11.5, 14.7)          # exp39 measured flooder totals of lqr 79_4 / 85_4 / 90_4, Gbit/s
CPU_REF_MIN = 2.0        # % of one core: below this the vhost signal is unusable (FAIL)
SD_PASS, SD_WARN = 0.10, 0.20           # noise sd of 0.5 s samples / smallest target
MONO_TOL_ABS, MONO_TOL_REL = 0.5, 0.03  # CPU may rise this much between levels before it counts as a violation
MONO_AUTH = 0.9          # CPU(0.5) > 0.9 x CPU(0) for any flooder: no control authority (FAIL)
R2_PASS, R2_WARN = 0.8, 0.5
G_AGREE_PASS, G_AGREE_WARN = 0.3, 0.5   # |G_up - G_down| / mean
TAU_AGREE_PASS = 2.0     # max/min tau of the up and down fit (above: WARN only)
GAIN_RATIO_PASS = 1.5    # small-signal / 0 -> 0.5 gain: PASS below, WARN at or above (never FAIL: a property of the
                         # plant that the faithful port must face, not a testbed fault; nan = broken data -> FAIL)
TARGET_STEP_MIN = 0.2    # Gbit/s, predicted totals should increase by at least this from T40 to T60 to T80 (WARN if
                         # not: a finding about lqr_orig's operating points, not a testbed fault; nan -> FAIL)
SPAN_PASS = 0.5          # predicted span >= 0.5 x the lqr span (14.7 - 8.8) and overlapping it: PASS
BPP_PASS = 1600          # bytes per XDP packet above this: GSO aggregates (WARN, informational)
DROP_TOL_PASS, DROP_TOL_WARN = 0.01, 0.03   # realized drop fraction vs commanded (0.05 and 0.5 levels)

def s_len():
    return SETTLE_T + len(E.FLOODERS) * (STEP_ON + STEP_OFF)

def m_len():
    return len(LEVELS_BP) * LEVEL_T

def expected_s():
    # checks + attach + swaps ~25 s; S and M each: secs + ~3 s end/wait; PAUSE between and after
    return 25 + s_len() + 3 + E.PAUSE + m_len() + 3 + E.PAUSE

def plan_line():
    return (f"planned: calib42 S {s_len():g} s (steps 0 -> 0.5 per flooder) + M {m_len():g} s (u levels "
            f"{', '.join(f'{b / 10000:g}' for b in LEVELS_BP)}) ~ {expected_s() / 60:.1f} min; writes only "
            f"{LOG_NAME}, {os.path.basename(TS)}, {os.path.basename(RESULT)}")

# ---------- pure analysis ----------

def worst(grades):
    return "FAIL" if "FAIL" in grades else ("WARN" if "WARN" in grades else "PASS")

def fit_step(t, y, t_step, post):
    # y = y0 + delta (1 - exp(-(t - t_step - theta) / tau)) for t >= t_step + theta, y0 before; least squares over
    # [t_step - FIT_PRE, t_step + post) on a (theta, tau) grid with (y0, delta) linear; returns dict incl. r2
    t, y = np.asarray(t, float), np.asarray(y, float)
    sel = (t >= t_step - FIT_PRE) & (t < t_step + post)
    tt, yy = t[sel], y[sel]
    if len(tt) < 10:
        return {"tau": float("nan"), "theta": float("nan"), "y0": float("nan"), "delta": float("nan"),
                "r2": float("nan"), "n": int(len(tt))}
    sst = float(np.sum((yy - yy.mean()) ** 2))
    best = None
    for th in THETAS:
        for tau in TAUS:
            x = np.where(tt >= t_step + th, 1.0 - np.exp(-np.maximum(0.0, tt - t_step - th) / tau), 0.0)
            X = np.column_stack([np.ones_like(x), x])
            coef, *_ = np.linalg.lstsq(X, yy, rcond=None)
            sse = float(np.sum((yy - X @ coef) ** 2))
            if best is None or sse < best[0]:
                best = (sse, th, tau, float(coef[0]), float(coef[1]))
    sse, th, tau, y0, delta = best
    return {"tau": tau, "theta": th, "y0": y0, "delta": delta, "r2": 1.0 - sse / sst if sst > 0 else float("nan"),
            "n": int(len(tt))}

def window_sd(t, y, a, b, group=5):
    # sd of the means of consecutive groups of `group` samples (0.5 s at CAL_DT 0.1) inside [a, b)
    ys = [v for tv, v in zip(t, y) if a <= tv < b]
    g = [st.mean(ys[i:i + group]) for i in range(0, len(ys) - group + 1, group)]
    return st.stdev(g) if len(g) > 1 else float("nan")

def window_mean(t, y, a, b):
    ys = [v for tv, v in zip(t, y) if a <= tv < b]
    return st.mean(ys) if ys else float("nan")

def predict_totals(cpu_med, totals, targets):
    # flooder total (Gbit/s) the static map gives at each target CPU: linear interpolation over the levels,
    # sorted by CPU, clamped at both ends (above the u = 0 CPU the controller stays at u = 0)
    pts = sorted(zip(cpu_med, totals))
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    if any(math.isnan(v) for v in xs + ys):
        return [float("nan")] * len(targets)
    out = []
    for x in targets:
        if x <= xs[0]: out.append(ys[0]); continue
        if x >= xs[-1]: out.append(ys[-1]); continue
        for (xa, ya), (xb, yb) in zip(pts, pts[1:]):
            if xa <= x <= xb:
                out.append(ya + (yb - ya) * (x - xa) / max(1e-9, xb - xa)); break
    return out

def grade_steady(sd_max, cpu_ref):
    if math.isnan(cpu_ref) or cpu_ref < CPU_REF_MIN or math.isnan(sd_max): return "FAIL"
    tmin = TARGET_FRACS[0] * cpu_ref
    if sd_max <= SD_PASS * tmin: return "PASS"
    return "WARN" if sd_max <= SD_WARN * tmin else "FAIL"

def mono_violations(levels_cpu):
    # levels_cpu: per flooder list of mean CPU at LEVELS_BP (increasing u)
    n = 0
    for c in levels_cpu:
        tol = max(MONO_TOL_ABS, MONO_TOL_REL * c[0])
        n += sum(b > a + tol for a, b in zip(c, c[1:]))
    return n

def grade_monotone(levels_cpu):
    if any(math.isnan(x) for c in levels_cpu for x in c): return "FAIL"
    if any(c[-1] > MONO_AUTH * c[0] for c in levels_cpu): return "FAIL"
    n = mono_violations(levels_cpu)
    return "PASS" if n == 0 else ("WARN" if n == 1 else "FAIL")

def grade_r2(fits):
    r2 = [f["r2"] for f in fits]
    if any(math.isnan(x) for x in r2) or any(f["G"] <= 0 for f in fits): return "FAIL"
    m = min(r2)
    return "PASS" if m >= R2_PASS else ("WARN" if m >= R2_WARN else "FAIL")

def updown(fits):
    # per flooder: (|G_up - G_down| / mean, max/min tau)
    out = {}
    for g in E.FLOODERS:
        up = [f for f in fits if f["flooder"] == g and f["dir"] == "up"][0]
        dn = [f for f in fits if f["flooder"] == g and f["dir"] == "down"][0]
        m = (up["G"] + dn["G"]) / 2.0
        out[g] = (abs(up["G"] - dn["G"]) / m if m > 0 else float("inf"),
                  max(up["tau"], dn["tau"]) / max(1e-9, min(up["tau"], dn["tau"])))
    return out

def grade_updown(ud):
    gs = [v[0] for v in ud.values()]; ts_ = [v[1] for v in ud.values()]
    if any(math.isnan(x) for x in gs + ts_) or max(gs) > G_AGREE_WARN: return "FAIL"
    if max(gs) > G_AGREE_PASS or max(ts_) > TAU_AGREE_PASS: return "WARN"
    return "PASS"

def gain_ratio(cpu_med):
    # small-signal gain (largest of the 0 -> 0.05 and 0.05 -> 0.1 slopes) over the 0 -> 0.5 gain, static map
    u = [b / 10000.0 for b in LEVELS_BP]
    g_half = (cpu_med[0] - cpu_med[-1]) / (u[-1] - u[0])
    g_small = max((cpu_med[0] - cpu_med[1]) / (u[1] - u[0]), (cpu_med[1] - cpu_med[2]) / (u[2] - u[1]))
    return g_small / g_half if g_half > 0 else float("inf")

def grade_gain_ratio(r):
    if math.isnan(r): return "FAIL"
    return "PASS" if r < GAIN_RATIO_PASS else "WARN"

def grade_targets(pred):
    if any(math.isnan(x) for x in pred):
        return "FAIL"
    if any(b - a < TARGET_STEP_MIN for a, b in zip(pred, pred[1:])):
        return "WARN"
    span_ok = pred[-1] - pred[0] >= SPAN_PASS * (LQR_TOTALS[-1] - LQR_TOTALS[0])
    overlap = pred[0] <= LQR_TOTALS[-1] and pred[-1] >= LQR_TOTALS[0]
    return "PASS" if span_ok and overlap else "WARN"

def grade_bpp(bpp):
    if math.isnan(bpp) or bpp <= 0: return "FAIL"
    return "PASS" if bpp <= BPP_PASS else "WARN"

def grade_dropfrac(errs):
    if not errs or any(math.isnan(x) for x in errs): return "FAIL"
    m = max(errs)
    return "PASS" if m <= DROP_TOL_PASS else ("WARN" if m <= DROP_TOL_WARN else "FAIL")

def grade_swap(in_ok, out_ok):
    return "PASS" if in_ok and out_ok else "FAIL"

def grade_survive(timeouts, json_ok):
    if timeouts > 0: return "FAIL"
    return "PASS" if all(json_ok.values()) else "WARN"

# ---------- measurement ----------

def phase(name, secs, events, tsf, hold_end):
    # victims + all four flooders for secs; vhost CPU every CAL_DT; events = sorted [(t_rel, "drop", {g: bp}) or
    # (t_rel, "count", label)] applied at the first sample at or after t_rel (actual time recorded);
    # hold_end: keep the last drop values while waiting for the flooders (else release to 0 first)
    F = E.FLOODERS
    fo = {g: tempfile.TemporaryFile("w+") for g in F}
    vo = {v: tempfile.TemporaryFile("w+") for v in E.VICTIMS}
    secs_i = int(math.ceil(secs)) + CAL_TAIL
    t0 = time.monotonic()
    fp = {g: E.ssh(g, E.iperf_cmd(g, secs_i, E.PAR[g]), fo[g]) for g in F}
    vp = {v: E.ssh(v, E.iperf_cmd(v, secs_i, rate_gbit=E.V_RATE), vo[v]) for v in E.VICTIMS}
    prev = E.vh_read()
    bp = {g: 0 for g in F}
    t_s, cpu_s, bp_s, applied, counts = [], {g: [] for g in F}, {g: [] for g in F}, [], {}
    ev, k = list(events), 1
    while k * CAL_DT <= secs + 1e-9:
        time.sleep(max(0.0, t0 + k * CAL_DT - time.monotonic())); k += 1
        now = E.vh_read()
        t_s.append((prev[F[0]][0] + now[F[0]][0]) / 2.0 - t0)   # interval midpoint
        for g in F:
            cpu_s[g].append(E.vh_pct(prev[g], now[g])); bp_s[g].append(bp[g])
        prev = now
        trel = now[F[0]][0] - t0
        while ev and ev[0][0] <= trel + 1e-9:
            te, kind, x = ev.pop(0)
            if kind == "drop":
                for g, b in x.items():
                    E.set_drop(E.IF[g], b); bp[g] = b
                applied.append((te, time.monotonic() - t0, dict(x)))
            else:
                counts[x] = (time.monotonic() - t0, {g: E.counters(E.IF[g]) for g in F})
    t_end = time.monotonic() - t0
    if not hold_end:
        for g in F:
            E.set_drop(E.IF[g], 0)
    held = dict(bp) if hold_end else {g: 0 for g in F}
    vt = E.wait_all(list(vp.values()), CAL_WAIT_S)
    tw = time.monotonic()
    ft = E.wait_all(list(fp.values()), CAL_WAIT_S)
    wait_s = time.monotonic() - tw
    json_ok = {g: not math.isnan(E.iperf_sum(fo[g])) for g in F}
    for f in list(fo.values()) + list(vo.values()): f.close()
    for g in F:
        E.set_drop(E.IF[g], 0)
    tsf.write("".join(f"{name},{t:.3f}," + ",".join(f"{cpu_s[g][i]:.3f},{bp_s[g][i]}" for g in F) + "\n"
                      for i, t in enumerate(t_s)))
    tsf.flush()
    print(f"{name}: {len(t_s)} samples in {t_end:.1f} s, flooder wait {wait_s:.1f} s with u held at "
          + ", ".join(f"{g} {held[g] / 10000:g}" for g in F) + f", timeouts flooders {ft} victims {vt}, iperf3 -J ok "
          + ", ".join(f"{g} {'yes' if json_ok[g] else 'NO'}" for g in F), flush=True)
    return {"t": t_s, "cpu": cpu_s, "bp": bp_s, "applied": applied, "counts": counts, "timeouts": ft,
            "victim_timeouts": vt, "json_ok": json_ok, "wait_s": wait_s}

def s_events():
    ev, t = [], SETTLE_T
    for g in E.FLOODERS:
        ev.append((t, "drop", {g: STEP_BP})); ev.append((t + STEP_ON, "drop", {g: 0}))
        t += STEP_ON + STEP_OFF
    return ev

def m_events():
    ev = []
    for i, b in enumerate(LEVELS_BP):
        if i > 0:
            ev.append((i * LEVEL_T, "drop", {g: b for g in E.FLOODERS}))
        ev.append((i * LEVEL_T + LEVEL_SKIP, "count", f"a{i}"))
        ev.append(((i + 1) * LEVEL_T - CAL_DT / 2, "count", f"b{i}"))
    return sorted(ev, key=lambda e: e[0])

def analyse(S, M):
    # everything the checks and calib42_result.json need, from the two phase records (pure)
    F = E.FLOODERS
    fits = []
    for te, ta, x in S["applied"]:
        (g, b), = x.items()
        f = fit_step(S["t"], S["cpu"][g], ta, STEP_ON if b else STEP_OFF)
        f.update({"flooder": g, "dir": "up" if b else "down", "t_step": ta})
        f["G"] = (-f["delta"] if b else f["delta"]) / (STEP_BP / 10000.0)
        fits.append(f)
    lv_cpu, lv_tot, lv_fl, bpp, derr = [], [], [], float("nan"), []
    for i, b in enumerate(LEVELS_BP):
        (ta, ca), (tb, cb) = M["counts"][f"a{i}"], M["counts"][f"b{i}"]
        lv_cpu.append([window_mean(M["t"], M["cpu"][g], ta, tb) for g in F])
        fl = [(cb[g][2] - ca[g][2]) * 8 / 1e9 / max(1e-3, tb - ta) for g in F]
        lv_fl.append(fl); lv_tot.append(sum(fl))
        pk = {g: (cb[g][0] - ca[g][0], cb[g][1] - ca[g][1]) for g in F}
        if b == 0:
            npk = sum(p + d for p, d in pk.values())
            nby = sum(cb[g][2] + cb[g][3] - ca[g][2] - ca[g][3] for g in F)
            bpp = nby / npk if npk > 0 else float("nan")
        if b in (500, 5000):
            derr += [abs(d / (p + d) - b / 10000.0) if p + d > 0 else float("nan") for p, d in pk.values()]
    cpu_med = [st.median(c) for c in lv_cpu]
    cpu_ref = cpu_med[0]
    per_fl = [[lv_cpu[i][j] for i in range(len(LEVELS_BP))] for j in range(len(F))]
    a0, b0 = M["counts"]["a0"][0], M["counts"]["b0"][0]
    sd = max([window_sd(S["t"], S["cpu"][g], *STEADY_WIN) for g in F] + [window_sd(M["t"], M["cpu"][g], a0, b0)
                                                                         for g in F])
    tau = st.median(f["tau"] for f in fits) if fits else float("nan")
    G = st.median(f["G"] for f in fits) if fits else float("nan")
    targets = [x * cpu_ref for x in TARGET_FRACS]
    return {"fits": fits, "levels_cpu": per_fl, "cpu_med": cpu_med, "totals": lv_tot, "flooder_gbps": lv_fl,
            "bpp": bpp, "drop_err": derr, "cpu_ref": cpu_ref, "sd_max": sd, "tau": tau, "G": G,
            "b": G / tau if tau > 0 else float("nan"), "targets": targets,
            "pred_totals": predict_totals(cpu_med, lv_tot, targets), "updown": updown(fits) if len(fits) == 8 else {},
            "gain_ratio": gain_ratio(cpu_med)}

def checks(a, swap_in, swap_out, M):
    out = []
    out.append(("vhost_signal_steady", grade_steady(a["sd_max"], a["cpu_ref"]),
                f"cpu_ref {a['cpu_ref']:.2f} % of one core (FAIL < {CPU_REF_MIN:g}); max sd of 0.5 s samples at u = 0 "
                f"{a['sd_max']:.2f} vs smallest target {TARGET_FRACS[0] * a['cpu_ref']:.2f} (PASS <= {SD_PASS:g} x, "
                f"WARN <= {SD_WARN:g} x)"))
    out.append(("vhost_monotone_in_u", grade_monotone(a["levels_cpu"]),
                f"CPU at u = {'/'.join(f'{b / 10000:g}' for b in LEVELS_BP)} per flooder: "
                + "; ".join(f"{g} " + "/".join(f"{x:.1f}" for x in c) for g, c in zip(E.FLOODERS, a["levels_cpu"]))
                + f" ({mono_violations(a['levels_cpu'])} rise(s); FAIL if CPU(0.5) > {MONO_AUTH:g} x CPU(0))"))
    out.append(("step_fit_r2", grade_r2(a["fits"]),
                f"min R^2 {min(f['r2'] for f in a['fits']):.3f} over {len(a['fits'])} fits (PASS >= {R2_PASS:g}, "
                f"WARN >= {R2_WARN:g}); gains G " + " ".join(f"{f['flooder']}{f['dir'][0]} {f['G']:.1f}"
                                                         for f in a["fits"])))
    out.append(("step_up_down_agreement", grade_updown(a["updown"]),
                "; ".join(f"{g} |dG|/G {v[0]:.2f} tau ratio {v[1]:.2f}" for g, v in a["updown"].items())
                + f" (PASS |dG|/G <= {G_AGREE_PASS:g} and tau ratio <= {TAU_AGREE_PASS:g}, FAIL |dG|/G > "
                  f"{G_AGREE_WARN:g})"))
    gr = grade_gain_ratio(a["gain_ratio"])
    out.append(("small_u_gain_ratio", gr,
                f"small-signal gain / 0 -> 0.5 gain = {a['gain_ratio']:.2f} (PASS < {GAIN_RATIO_PASS:g}, else WARN; "
                f"FAIL only if not computable); the ARE gain is stable only up to ~1.5 x the identified plant gain"
                + (f" -- WARN MEANING: near u = 0 the plant is {a['gain_ratio']:.1f} x steeper than the 0 -> 0.5 step "
                   f"used to identify b, so lqr_orig's re-identified K is effectively too aggressive at small drop "
                   f"fractions; expect a limit cycle between 0 and the first levels (0.05-0.10) rather than smooth "
                   f"tracking, i.e. report its tracking error and victim SLO as properties of the faithful port in "
                   f"our TCP plant, not of a mis-tuned port (nothing is retuned)" if gr == "WARN" else "")))
    tg = grade_targets(a["pred_totals"])
    flat = any(b - x < TARGET_STEP_MIN for x, b in zip(a["pred_totals"], a["pred_totals"][1:]))
    out.append(("targets_span", tg,
                "predicted flooder totals at " + ", ".join(f"T{int(f * 100)} ({t:.1f} %) {p:.2f}" for f, t, p in
                                                           zip(TARGET_FRACS, a["targets"], a["pred_totals"]))
                + f" Gbit/s vs exp39 lqr 79/85/90 {'/'.join(f'{x:g}' for x in LQR_TOTALS)}"
                + ((f" -- WARN MEANING: the totals do not rise by >= {TARGET_STEP_MIN:g} Gbit/s from T40 to T60 to T80, "
                    f"so the three lqr_orig configs are (nearly) the same operating point; they cannot be read as a "
                    f"low/mid/high throughput sweep like lqr 79/85/90, only as repeats of one point (the static map "
                    f"is a prediction; the measured fl_total decides)") if tg == "WARN" and flat else
                   (f" -- WARN MEANING: the predicted span is narrower than half of lqr 79-90's or does not overlap it, "
                    f"so lqr_orig's operating points cover a different throughput range than lqr's; compare at matched "
                    f"fl_total, not by config name") if tg == "WARN" else "")))
    out.append(("xdp_bytes_per_packet", grade_bpp(a["bpp"]),
                f"{a['bpp']:.0f} bytes per XDP packet at u = 0 (PASS <= {BPP_PASS}: per-segment drop; WARN: GSO "
                f"aggregates, one drop removes several segments)"))
    out.append(("xdp_drop_fraction", grade_dropfrac(a["drop_err"]),
                f"max |realized - commanded| drop fraction at u = 0.05 and 0.5 = "
                f"{max(a['drop_err']) if a['drop_err'] else float('nan'):.4f} (PASS <= {DROP_TOL_PASS:g})"))
    out.append(("xdp_swap_in_out", grade_swap(swap_in, swap_out),
                f"xdp_dropfrac on all 4 flooders: {'yes' if swap_in else 'NO'}; xdp_limit back on all 4: "
                f"{'yes' if swap_out else 'NO'}"))
    out.append(("ssh_iperf3_at_u_0.5", grade_survive(M["timeouts"], M["json_ok"]),
                f"u = 0.5 held on all 4 flooders to the end: {M['timeouts']} flooder ssh/iperf3 still running after "
                f"{CAL_WAIT_S:g} s (FAIL if > 0), -J parsed " + ", ".join(
                    f"{g} {'yes' if ok else 'NO'}" for g, ok in M["json_ok"].items()) + f", wait {M['wait_s']:.1f} s"))
    return out

def write_result(res):
    with open(RESULT, "w") as f:
        json.dump(res, f, indent=1)
        f.flush(); os.fsync(f.fileno())

def main():
    existing = [p for p in (TS, RESULT) if os.path.exists(p)]
    if existing:
        raise SystemExit("REFUSING TO START: " + ", ".join(existing) + " already exist(s) (calib42 never appends)")
    nproc, ncpu = E.host_cpus()
    if nproc != E.NPROC_REQUIRED:
        raise SystemExit(f"REFUSING TO START: nproc = {nproc}, calib42 requires exactly {E.NPROC_REQUIRED}")
    try:
        E.sudo(["true"])
    except Exception as e:
        raise SystemExit(f"sudo needs a password: run 'sudo -v' first (calibrate42.sh does this) [{e}]")
    for o in (E.OBJ, E.OBJ_DF):
        if not os.path.exists(o):
            raise SystemExit(f"{o} not found")
    E.check_servers()
    E.check_guests()
    E.resolve_vhost()
    print(plan_line(), flush=True)
    t_start = time.monotonic()
    tsf = open(TS, "w")
    tsf.write("phase,t," + ",".join(f"vh{g}_cpu,drop_bp{g}" for g in E.FLOODERS) + "\n")
    procs_ok, swap_in, swap_out, S, M, err = True, False, False, None, None, None
    try:
        for g in E.VICTIMS + E.FLOODERS:
            E.IF[g] = E.attach(E.vnet(E.GUESTS[g][0]), 0)
        try:
            for g in E.FLOODERS:
                E.swap(g, "dropfrac")
            swap_in = all(E.prog_maps(E.IF[g]["dev"], "dropfrac")["kind"] == "dropfrac" for g in E.FLOODERS)
        except (SystemExit, subprocess.CalledProcessError) as e:   # E.SudoError: str(e) carries ip's stderr
            err = f"swap in: {e}"
            print(f"ERROR: {err}", flush=True)
        if swap_in:
            print("XDP: xdp_dropfrac on " + ", ".join(f"{g} {E.IF[g]['dev']}" for g in E.FLOODERS), flush=True)
            S = phase("S", s_len(), s_events(), tsf, hold_end=False)
            time.sleep(E.PAUSE)
            M = phase("M", m_len(), m_events(), tsf, hold_end=True)
    finally:
        # after ANY failure or Ctrl+C: xdp_limit back on all 4 flooders, checked against the kernel's state
        failed = E.restore_flooders()
        try:
            swap_out = not failed and all(E.prog_maps(E.IF[g]["dev"], "limit")["kind"] == "limit"
                                          for g in E.FLOODERS)
        except (SystemExit, KeyError, subprocess.CalledProcessError) as e:
            failed = failed + [("check", str(e))]
        if failed:
            err = (err + "; " if err else "") + "swap out: " + "; ".join(f"{g} {t}" for g, t in failed)
        for d in E.IF.values():
            if d.get("kind", "limit") == "limit":
                E.last_cap.pop(d["cfg"], None)
                try:
                    E.set_cap(d, 0)
                except Exception as ex:
                    print(f"WARNING: could not reset cap on {d['dev']}: {ex}", flush=True)
        tsf.close()
    dur = time.monotonic() - t_start
    if S is None or M is None:
        res = {"verdict": "FAIL", "checks": [["xdp_swap_in_out", "FAIL", err or "phases not run"]],
               "cpu_ref": None, "tau": None, "b": None, "written": time.strftime("%Y-%m-%d %H:%M:%S")}
        print(f"CHECK xdp_swap_in_out: FAIL -- {err}\nVERDICT: FAIL (phases not run)", flush=True)
        write_result(res)
        return res
    try:
        a = analyse(S, M)
        ch = checks(a, swap_in, swap_out, M)
    except Exception as e:   # unusable data: still write a FAIL result (exp42 then refuses with the reason)
        res = {"verdict": "FAIL", "checks": [["analysis", "FAIL", f"{e!r}"]], "cpu_ref": None, "tau": None,
               "b": None, "written": time.strftime("%Y-%m-%d %H:%M:%S")}
        print(f"CHECK analysis: FAIL -- {e!r}\nVERDICT: FAIL", flush=True)
        write_result(res)
        return res
    verdict = worst([c[1] for c in ch])
    for name, grade, txt in ch:
        print(f"CHECK {name}: {grade} -- {txt}", flush=True)
    for f in a["fits"]:
        print(f"FIT {f['flooder']} {f['dir']:4s} at {f['t_step']:.2f} s: tau {f['tau']:.3f} s, dead time "
              f"{f['theta']:.1f} s, y0 {f['y0']:.2f}, delta {f['delta']:+.2f}, G {f['G']:.2f}, R^2 {f['r2']:.3f}",
              flush=True)
    print(f"PLANT: tau {a['tau']:.3f} s (median of {len(a['fits'])}), G {a['G']:.2f} CPU points per unit u, b = G/tau "
          f"{a['b']:.3f}; cpu_ref {a['cpu_ref']:.2f} %; targets T40/T60/T80 "
          + "/".join(f"{t:.2f}" for t in a["targets"]) + "; ARE K at each target: "
          + "; ".join(f"[{k[0]:.4f}, {k[1]:.4f}]" for k in (E.lqo_gain(a["tau"], a["b"], t) for t in a["targets"]))
          + f"; paper K spectral radius on this plant {E.lqo_eig(a['tau'], a['b'], E.K_PAPER):.3f}"
          if all(math.isfinite(x) and x > 0 for x in (a["tau"], a["b"], a["cpu_ref"])) else
          f"PLANT: not identifiable (tau {a['tau']}, b {a['b']}, cpu_ref {a['cpu_ref']})", flush=True)
    print(f"STATIC MAP: u " + " ".join(f"{b / 10000:g}: median CPU {c:.1f} %, total {t:.2f} Gbit/s;"
                                      for b, c, t in zip(LEVELS_BP, a["cpu_med"], a["totals"])), flush=True)
    print(f"VERDICT: {verdict}  (duration {dur:.0f} s; exp42/run42.sh refuse to start on FAIL)", flush=True)
    res = {"verdict": verdict, "checks": [list(c) for c in ch], "cpu_ref": a["cpu_ref"], "tau": a["tau"],
           "b": a["b"], "G": a["G"], "targets": dict(zip(("T40", "T60", "T80"), a["targets"])),
           "pred_totals": dict(zip(("T40", "T60", "T80"), a["pred_totals"])), "fits": a["fits"],
           "levels_bp": list(LEVELS_BP), "levels_cpu": a["levels_cpu"], "cpu_med": a["cpu_med"],
           "totals": a["totals"], "flooder_gbps": a["flooder_gbps"], "bytes_per_packet": a["bpp"],
           "gain_ratio": a["gain_ratio"], "sd_max": a["sd_max"], "vhost": dict(E.VH),
           "duration_s": dur, "written": time.strftime("%Y-%m-%d %H:%M:%S")}
    write_result(res)
    return res

if __name__ == "__main__":
    E.exit_on_term_signals()
    main()
