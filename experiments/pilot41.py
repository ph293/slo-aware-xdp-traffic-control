import sys, os, time, math, statistics as st, tempfile, subprocess
import exp41 as E
# pilot41: short feasibility pilot for exp41 (~3.5 min), started by pilot41.sh. Writes ONLY pilot41_* files
# (pilot41_run.log via pilot41.sh, pilot41_ts.csv, pilot41_vfrot_ts.csv); never touches the exp41 outputs.
# Phases (victims V1 9G + V2 2G + V2 ping in every phase, PAUSE s between phases):
#   P0 base         20 s, no flooders                         -> victim refs + RTT baseline for P4
#   P1 udp_free     20 s, B UDP -b 6G, cap unlimited         -> what B really offers, tick, CPU, RTT
#   P2 udp_cap2500  20 s, B UDP -b 6G, B capped 2500 Mbit/s  -> does the token bucket hold on UDP?
#   P3 udp_cap200   20 s, B UDP -b 6G, B capped 200 Mbit/s   -> cap hold at CAP_MIN, host CPU with B dropped
#   P4 vfrot        one full exp41 slot, vfrot 0.8_1_2_0.94, all four flooders (the real exp41.run())
HERE = os.path.dirname(os.path.abspath(__file__))
TS = os.path.join(HERE, "pilot41_ts.csv")
VTS = os.path.join(HERE, "pilot41_vfrot_ts.csv")
LOG_NAME = "pilot41_run.log"            # created by pilot41.sh (tee); checked there
PHASE_T = 20
PHASES = [("P0 base", None), ("P1 udp_free", 0.0), ("P2 udp_cap2500", 2500.0), ("P3 udp_cap200", 200.0)]
VF_CFG = ("vfrot", "0.8_1_2_0.94")
B = "B"
UDP_PASS_FRAC = 0.9       # offered >= 0.9 x 6G: PASS; >= UDP_WARN_GBIT: WARN; else FAIL
UDP_WARN_GBIT = 2.0
CAP_TOL, CAP_TOL_WARN = 1.05, 1.15   # passed <= cap x 1.05: PASS; <= x 1.15: WARN; else FAIL
CAP_LOW = 0.85            # ... and passed >= 0.85 x min(cap, offered), else WARN (bucket too strict / B too slow)
TICK_FAIL_S = 0.35        # mean tick > 0.30 (exp41 TICK_WARN_S): WARN, > 0.35: FAIL; max tick > 1 s: WARN
TICK_MAX_WARN_S = 1.0
RTT_FLOOD_WARN = 10.0     # flood phase p99 > 10 x base p99: WARN (the RTT SLO would be unreachable for any controller)
CPU_CAP_WARN = 95.0       # host CPU mean with B capped at 200 Mbit/s above this: WARN

def expected_s():
    # setup (checks, attach) ~20 s + 4 phases x (PHASE_T + ~1.5 s + PAUSE) + one slot (OFFSET + T + 2.5 + PAUSE)
    return 20 + len(PHASES) * (PHASE_T + 1.5 + E.PAUSE) + (E.OFFSET + E.T + 2.5 + E.PAUSE)

def plan_line():
    return (f"planned: pilot41 {len(PHASES)} phases x {PHASE_T} s ({', '.join(n for n, _ in PHASES)}) + one exp41 "
            f"{VF_CFG[0]} {VF_CFG[1]} slot ({E.T} s, all four flooders) ~ {expected_s() / 60:.1f} min; writes only "
            f"{LOG_NAME}, {os.path.basename(TS)}, {os.path.basename(VTS)}")

# ---------- verdicts (pure) ----------

def worst(grades):
    return "FAIL" if "FAIL" in grades else ("WARN" if "WARN" in grades else "PASS")

def grade_tick(mean, mx):
    if math.isnan(mean) or mean > TICK_FAIL_S: return "FAIL"
    if mean > E.TICK_WARN_S or mx > TICK_MAX_WARN_S: return "WARN"
    return "PASS"

def grade_offered(off_gbit, iperf_ok):
    if math.isnan(off_gbit) or off_gbit < UDP_WARN_GBIT: return "FAIL"
    if off_gbit < UDP_PASS_FRAC * E.UDP_GBIT or not iperf_ok: return "WARN"
    return "PASS"

def grade_cap(passed_mbit, offered_mbit, cap_mbit):
    if math.isnan(passed_mbit) or passed_mbit > CAP_TOL_WARN * cap_mbit: return "FAIL"
    if passed_mbit > CAP_TOL * cap_mbit or passed_mbit < CAP_LOW * min(cap_mbit, offered_mbit): return "WARN"
    return "PASS"

def grade_rtt_base(n_rx, loss):
    if n_rx == 0 or loss > E.REF_LOSS_MAX: return "FAIL"
    if loss > 0: return "WARN"
    return "PASS"

def grade_rtt_flood(n_rx, loss, p99, base_p99):
    if n_rx == 0: return "FAIL"
    if loss > E.REF_LOSS_MAX or not p99 <= RTT_FLOOD_WARN * base_p99: return "WARN"
    return "PASS"

def grade_cpu(cpu_mean):
    return "WARN" if not cpu_mean <= CPU_CAP_WARN else "PASS"

def grade_slot(r, tick_mean, n_rx):
    nm = E.RESULT_NAMES.index
    if tick_mean > TICK_FAIL_S or n_rx == 0 or math.isnan(r[nm("slo_ok_worst")]): return "FAIL"
    if (tick_mean > E.TICK_WARN_S or any(math.isnan(r[nm(f"flooder{g}_iperf_gbps")]) for g in E.FLOODERS)):
        return "WARN"
    return "PASS"

# ---------- one victims + ping (+ B) phase ----------

def phase(name, cap_b, procs, tsf):
    # victims + ping for PHASE_T s; cap_b None: no flooder; else B's UDP flood with B's cap = cap_b (0 = unlimited);
    # every interface is sampled every DT exactly as in exp41.run(), so the tick cost is realistic
    for g in E.FLOODERS:
        E.set_cap(E.IF[g], cap_b if (g == B and cap_b) else 0)
    hist = {g: [] for g in E.IF}; offered = {g: [] for g in E.IF}
    def sample():
        s0 = time.monotonic()
        for g in E.IF:
            c = E.counters(E.IF[g]); tn = time.monotonic()
            hist[g].append((tn, c[2])); offered[g].append((tn, c[2] + c[3]))
        return time.monotonic() - s0
    sample()
    vo = {v: tempfile.TemporaryFile("w+") for v in E.VICTIMS}
    bo, po = tempfile.TemporaryFile("w+"), tempfile.TemporaryFile("w+")
    t0 = time.monotonic()
    ps = []
    if cap_b is not None:
        ps.append(E.ssh(B, E.iperf_cmd(B, PHASE_T, 1, 0, E.UDP_GBIT, udp=True), bo))
    vp = [E.ssh(v, cmd, vo[v]) for v, cmd in E.victim_cmds(PHASE_T).items()]
    pp = E.ssh(E.PING_V, E.ping_cmd(PHASE_T), po)
    procs += ps + vp + [pp]
    c_prev = E.cpu(); cpus, steals, ticks = [], [], []
    v0 = E.VICTIMS[0]
    k = 1
    while any(p.poll() is None for p in vp) and time.monotonic() < t0 + PHASE_T + 30:
        time.sleep(max(0.0, t0 + k * E.DT - time.monotonic())); k += 1
        prev = {g: hist[g][-1] for g in E.IF}; prev_off = {g: offered[g][-1] for g in E.IF}
        samp = sample(); tnow = hist[v0][-1][0]; c_now = E.cpu()
        busy, steal = E.busy_steal(c_prev, c_now); c_prev = c_now
        dt = max(1e-3, tnow - prev[v0][0])
        cpus.append(busy); steals.append(steal); ticks.append(dt)
        rt = lambda h, p0, g: (h[g][-1][1] - p0[g][1]) * 8 / 1e9 / max(1e-3, h[g][-1][0] - p0[g][0])
        tsf.write(f"{name},{tnow - t0:.3f},{dt:.4f},{samp:.4f},{busy:.2f},{steal:.2f},{rt(hist, prev, 'V1'):.3f},"
                  f"{rt(hist, prev, 'V2'):.3f},{(cap_b or 0):.0f},{rt(offered, prev_off, B):.3f},"
                  f"{rt(hist, prev, B):.3f}\n")
    for p in vp + ps + [pp]: p.wait()
    sample()
    tsf.flush()
    po.seek(0); got, sent = E.parse_ping(po.read(), int(round(PHASE_T / E.PING_I)))
    p50, p99, loss, p99_slo = E.rtt_stats(got, sent)
    out = {"name": name, "cap": cap_b,
           "v": {v: (hist[v][-1][1] - hist[v][0][1]) * 8 / 1e9 / PHASE_T for v in E.VICTIMS},
           "b_off": (offered[B][-1][1] - offered[B][0][1]) * 8 / 1e9 / PHASE_T,
           "b_pass": (hist[B][-1][1] - hist[B][0][1]) * 8 / 1e9 / PHASE_T,
           "b_iperf": E.iperf_sum(bo, PHASE_T) if cap_b is not None else float("nan"),
           "cpu": E.nanstat(cpus, st.mean), "cpu_max": E.nanstat(cpus, max),
           "steal": E.nanstat(steals, st.mean), "steal_max": E.nanstat(steals, max),
           "tick": E.nanstat(ticks[1:], st.mean), "tick_max": E.nanstat(ticks[1:], max),
           "n_rx": len(got), "sent": sent, "rtts": list(got.values()),
           "p50": p50, "p99": p99, "loss": loss, "p99_slo": p99_slo}
    for f in list(vo.values()) + [bo, po]: f.close()
    print(f"{name}: V1 {out['v']['V1']:.2f} V2 {out['v']['V2']:.2f} Gbit/s (eBPF); B offered {out['b_off']:.2f} "
          f"passed {out['b_pass']:.2f} Gbit/s (eBPF), B iperf3 received {out['b_iperf']:.2f}; host CPU mean "
          f"{out['cpu']:.1f}% max {out['cpu_max']:.1f}%, steal {out['steal']:.2f}/{out['steal_max']:.2f}%; tick mean "
          f"{out['tick']:.3f} max {out['tick_max']:.3f} s; ping {len(got)}/{sent} replies, p50 {p50:.3f} p99 "
          f"{p99:.3f} ms, loss {loss:.1f}%", flush=True)
    return out

def checks(ph, slot):
    # ph: {phase name: phase()} ; slot: (r, tick_mean, n_rx) of the vfrot slot or None -> [(check, grade, text)]
    out = []
    b0 = ph["P0 base"]
    out.append(("rtt_base", grade_rtt_base(b0["n_rx"], b0["loss"]),
                f"V2 ping without flooders: {b0['n_rx']}/{b0['sent']} replies, p50 {b0['p50']:.3f} ms, p99 "
                f"{b0['p99']:.3f} ms, loss {b0['loss']:.1f}% (exp41 refuses a reference with loss > {E.REF_LOSS_MAX:g}%)"))
    for n, x in ph.items():
        out.append((f"tick {n}", grade_tick(x["tick"], x["tick_max"]),
                    f"mean {x['tick']:.3f} s, max {x['tick_max']:.3f} s (nominal {E.DT:g}; WARN > {E.TICK_WARN_S:g}, "
                    f"FAIL > {TICK_FAIL_S:g}), host CPU {x['cpu']:.1f}%"))
    u = ph["P1 udp_free"]
    out.append(("udp_offered", grade_offered(u["b_off"], not math.isnan(u["b_iperf"])),
                f"B UDP -b {E.UDP_GBIT:g}G uncapped offered {u['b_off']:.2f} Gbit/s at the host (eBPF), iperf3 "
                f"received {u['b_iperf']:.2f} (PASS >= {UDP_PASS_FRAC * E.UDP_GBIT:.1f}, FAIL < {UDP_WARN_GBIT:g})"))
    for n in ("P2 udp_cap2500", "P3 udp_cap200"):
        x = ph[n]
        out.append((f"udp_cap {x['cap']:g}", grade_cap(x["b_pass"] * 1000, x["b_off"] * 1000, x["cap"]),
                    f"B offered {x['b_off'] * 1000:.0f} Mbit/s, passed {x['b_pass'] * 1000:.0f} Mbit/s with the cap at "
                    f"{x['cap']:g} (PASS <= x{CAP_TOL:g} and >= {CAP_LOW:g} x min(cap, offered))"))
    for n in ("P1 udp_free", "P2 udp_cap2500", "P3 udp_cap200"):
        x = ph[n]
        ok = E.rtt_ok(x["p99_slo"], b0["p99"])
        out.append((f"rtt {n}", grade_rtt_flood(x["n_rx"], x["loss"], x["p99"], b0["p99"]),
                    f"p50 {x['p50']:.3f} p99 {x['p99']:.3f} ms ({x['p99'] / b0['p99']:.1f} x base), loss "
                    f"{x['loss']:.1f}%; exp41 slo_ok_v2_rtt would be {ok:.0f} (WARN if p99 > {RTT_FLOOD_WARN:g} x "
                    f"base or loss > {E.REF_LOSS_MAX:g}%)"))
    x = ph["P3 udp_cap200"]
    out.append(("cpu udp_cap200", grade_cpu(x["cpu"]),
                f"host CPU mean {x['cpu']:.1f}% with B dropped to 200 Mbit/s (uncapped {u['cpu']:.1f}%, base "
                f"{b0['cpu']:.1f}%): the CPU the cap cannot remove (WARN > {CPU_CAP_WARN:g}%)"))
    if slot is None:
        out.append(("vfrot_slot", "FAIL", "not run"))
    else:
        r, tm, n_rx = slot
        nm = E.RESULT_NAMES.index
        out.append(("vfrot_slot", grade_slot(r, tm, n_rx),
                    f"tick mean {tm:.3f} s; ok_v1 {r[nm('slo_ok_v1')]:.2f} ok_v2 {r[nm('slo_ok_v2')]:.2f} ok_worst "
                    f"{r[nm('slo_ok_worst')]:.2f} rtt p99 {r[nm('v2_rtt_p99_ms')]:.3f} ms loss "
                    f"{r[nm('v2_loss_pct')]:.1f}% rtt_ok {r[nm('slo_ok_v2_rtt')]:.0f} ok_w_rtt "
                    f"{r[nm('slo_ok_worst_with_rtt')]:.2f}; flooder offered/passed Gbit/s " + ", ".join(
                        f"{g} {r[nm(f'flooder{g}_offered_gbps')]:.2f}/{r[nm(f'flooder{g}_ebpf_gbps')]:.2f}"
                        for g in E.FLOODERS) + "; iperf3 reports " + ", ".join(
                        f"{g} {'ok' if not math.isnan(r[nm(f'flooder{g}_iperf_gbps')]) else 'MISSING'}"
                        for g in E.FLOODERS) + f"; ping {n_rx} replies"))
    return out

def main():
    t_start = time.monotonic()
    existing = [f for f in (TS, VTS) if os.path.exists(f)]
    if existing:
        raise SystemExit("REFUSING TO START: pilot output file(s) already exist: " + ", ".join(existing))
    nproc, ncpu = E.host_cpus()
    if nproc != E.NPROC_REQUIRED:
        raise SystemExit(f"REFUSING TO START: nproc = {nproc} (os.cpu_count() = {ncpu}), pilot41 requires exactly "
                         f"{E.NPROC_REQUIRED}")
    try:
        E.sudo(["true"])
    except subprocess.CalledProcessError:
        raise SystemExit("sudo needs a password: run 'sudo -v' first (pilot41.sh does this)")
    if not os.path.exists(E.OBJ):
        raise SystemExit(f"{E.OBJ} not found")
    E.check_servers()
    E.check_guests()
    E.setup_configs([VF_CFG])
    print(plan_line(), flush=True)
    procs, ph, slot = [], {}, None
    tsf = open(TS, "a"); vtsf = open(VTS, "a")
    try:
        for g in E.VICTIMS + E.FLOODERS:
            E.IF[g] = E.attach(E.vnet(E.GUESTS[g][0]), 0)
        tsf.write("phase,t,tick_s,sample_s,cpu,steal,v1_gbps_tick,v2_gbps_tick,capB_mbit,offB_gbps_tick,"
                  "passB_gbps_tick\n")
        for name, cap in PHASES:
            ph[name] = phase(name, cap, procs, tsf)
            time.sleep(E.PAUSE)
        b0 = ph["P0 base"]
        refs = dict(b0["v"])
        if any(not refs[v] > 0 for v in E.VICTIMS) or b0["n_rx"] < E.REF_RTT_MIN_N:
            print("P4 skipped: P0 gave no usable victim / RTT reference", flush=True)
        else:
            rref = {"p50": b0["p50"], "p99": b0["p99"], "loss": b0["loss"], "n": b0["n_rx"]}
            print(f"P4 {VF_CFG[0]} {VF_CFG[1]}: one exp41 slot with refs from P0 " + ", ".join(
                f"{v} {refs[v]:.3f}" for v in E.VICTIMS) + f" Gbit/s, RTT p99 {rref['p99']:.3f} ms", flush=True)
            r, ts = E.run(*VF_CFG, refs, b0["cpu"], procs, rref)
            vtsf.write(E.TS_HEADER + "\n" + "".join(E.ts_line(0, 0, *VF_CFG, row) for row in ts)); vtsf.flush()
            nm = E.RESULT_NAMES.index   # replies = probes x (1 - loss); 0 when no reply arrived (p99 nan)
            n_rx = (0 if math.isnan(r[nm("v2_rtt_p99_ms")])
                    else int(round((100 - r[nm("v2_loss_pct")]) / 100 * int(round(E.T / E.PING_I)))))
            slot = (r, r[nm("tick_mean_s")], n_rx)
    finally:
        for p in procs:
            if p.poll() is None: p.terminate()
        for d in E.IF.values():
            E.last_cap.pop(d["cfg"], None)
            try:
                E.set_cap(d, 0)
            except Exception as ex:
                print(f"WARNING: could not reset cap on {d['dev']} (cfg map id {d['cfg']}): {ex}", flush=True)
        tsf.close(); vtsf.close()
    res = checks(ph, slot)
    print("\nPILOT41 CHECKS")
    for n, g, txt in res:
        print(f"{g:4s}  {n:20s} {txt}")
    print(f"PILOT41 OVERALL: {worst([g for _, g, _ in res])} (elapsed {(time.monotonic() - t_start) / 60:.1f} min)",
          flush=True)
    return res

if __name__ == "__main__":
    main()
