import json, subprocess, time, struct, random, statistics as st, sys, os, math, tempfile
REPS = int(sys.argv[1]) if len(sys.argv) > 1 else 5
T, DT = 40, 0.25
OFFSET = 0.5            # victim starts 0.5 s after the flooders are launched (as exp5)
A_START, A_DUR, A_PAR = 0, 27, 4    # flooder A (tenant2): t=0..27 s, 4 streams
B_START, B_DUR, B_PAR = 13, 27, 2   # flooder B (tenant3): t=13..40 s, 2 streams
OVERLAP = (13.0, 27.0)  # both flooders active: window for Jain fairness (t relative to flooder launch)
KP, KI = 200.0, 300.0
CAP_MIN, CAP_MAX = 200.0, 70000.0
INIT_CAP_MBIT = 2000.0  # pid/ff: initial shared cap (split equally); slo: initial cap of each flooder
K_CPU = 0.93            # % host CPU per Gbit/s (from the Step 19 measurement)
SLO_FRAC = 0.95         # slo_ok_fraction threshold (all modes): SLO_FRAC * reference rate
SLO_FLOORS = (95,)      # slo controller floors, % of the reference rate (one config each)
MD, AI_MBIT = 0.7, 500.0   # slo: cut factor; additive increase per tick for a sending flooder
SLO_CONSEC = 2          # slo: cut only after this many consecutive ticks below the floor
HOLDOFF = 1.0           # s, slo: no further cut for this long after a cut
SEND_MBIT = 100.0       # a flooder counts as "sending" above this rate
SMOOTH = 1.0            # s, victim/flooder rate smoothing window for the slo controller
REF_RUNS, REF_T = 2, 10   # victim-only reference, measured at the start of EVERY repeat
PAUSE = 8
COOLDOWN = 45           # s, extra cool-down pause between repeats (thermal), on top of PAUSE
SESSION_REPEATS = 2     # repeats per session; a long rest follows each session except the last
SESSION_REST_S = 600    # s, thermal rest between sessions, on top of COOLDOWN
HOST = "10.10.0.1"
GUESTS = {"V": ("tenant1", "10.10.0.11", 5201),   # victim
          "A": ("tenant2", "10.10.0.12", 5202),   # flooder A
          "B": ("tenant3", "10.10.0.13", 5203)}   # flooder B
HERE = os.path.dirname(os.path.abspath(__file__))
OBJ = os.path.join(HERE, "xdp_limit.o")
CSV = os.path.join(HERE, "results_dyn10.csv")
TS_CSV = os.path.join(HERE, "ts_dyn10.csv")
REF_FILE = os.path.join(HERE, "ref_victim10.txt")
HEADER = ("rep,slot,mode,param,victim_mean_gbps,victim_p5_gbps,slo_ok_fraction,"
          "flooderA_iperf_gbps,flooderA_ebpf_gbps,flooderB_iperf_gbps,flooderB_ebpf_gbps,"
          "jain_fairness,cpu_mean,cpu_max,ref_gbps")
TS_HEADER = "rep,slot,mode,param,t,victim_gbps_1s,capA_mbit,capB_mbit,rateA_gbps,rateB_gbps"

IF = {}           # "V"/"A"/"B" -> {"dev", "prog", "cfg", "cnt"} (map ids, never names)
last_cap = {}     # cfg map id -> last bytes/s written

def sudo(args):
    return subprocess.run(["sudo", "-n"] + args, capture_output=True, text=True, check=True).stdout

# ---------- interfaces and eBPF maps, all addressed by id ----------

def vnet(dom):
    # names change after guest restarts, so always ask libvirt; '-' means the guest is not running
    rows = [l.split() for l in sudo(["virsh", "domiflist", dom]).splitlines()[2:] if l.strip()]
    rows = [r for r in rows if r[0] != "-"]
    if len(rows) != 1:
        raise SystemExit(f"{dom}: expected exactly one active interface in virsh domiflist, got {rows}")
    return rows[0][0]

def xdp_prog(dev):
    for blk in json.loads(sudo(["bpftool", "-j", "net", "show", "dev", dev])):
        for x in blk.get("xdp", []):
            if x.get("devname") != dev:
                continue
            att = x.get("multi_attachments", [x])
            if len(att) != 1:
                raise SystemExit(f"{dev}: {len(att)} XDP programs attached, expected one")
            return att[0]["id"], att[0].get("mode")
    return None, None

def attach(dev, cap_mbit):
    pid, mode = xdp_prog(dev)
    if pid is None:
        sudo(["ip", "link", "set", "dev", dev, "xdpgeneric", "obj", OBJ, "sec", "xdp"])
        pid, mode = xdp_prog(dev)
        how = "attached"
    else:
        how = "reusing already attached"
    prog = json.loads(sudo(["bpftool", "-j", "prog", "show", "id", str(pid)]))
    if prog.get("name") != "xdp_limit" or mode != "generic":
        raise SystemExit(f"{dev}: XDP prog {pid} ({prog.get('name')}, mode {mode}) is not xdp_limit "
                         f"in generic mode; detach it yourself, then rerun")
    maps = {}
    for m in prog.get("map_ids", []):
        maps[json.loads(sudo(["bpftool", "-j", "map", "show", "id", str(m)]))["name"]] = m
    if "cfg" not in maps or "cnt" not in maps:
        raise SystemExit(f"{dev}: prog {pid} has maps {maps}, expected cfg and cnt")
    d = {"dev": dev, "prog": pid, "cfg": maps["cfg"], "cnt": maps["cnt"]}
    set_cap(d, cap_mbit)
    print(f"{dev}: {how} xdp_limit prog id {pid}, cfg map id {d['cfg']}, cnt map id {d['cnt']}", flush=True)
    return d

def counters(d):
    out = sudo(["bpftool", "-j", "map", "dump", "id", str(d["cnt"])])
    c = {}
    for e in json.loads(out):
        f = e.get("formatted", e); v = f["value"]
        c[f["key"]] = sum(v) if isinstance(v, list) else v
    return c

def set_cap(d, mbps):
    # 0 = unlimited; skip the bpftool call if this map already holds the value
    b = int(mbps * 1e6 / 8)
    if last_cap.get(d["cfg"]) == b:
        return
    h = [f"{x:02x}" for x in struct.pack("<Q", b)]
    sudo(["bpftool", "map", "update", "id", str(d["cfg"]), "key", "0", "0", "0", "0", "value", "hex"] + h)
    last_cap[d["cfg"]] = b

def cpu():
    f = list(map(int, open("/proc/stat").readline().split()[1:]))
    return sum(f), f[3] + f[4]

# ---------- helpers on (t, cumulative passed bytes) histories ----------

def cum_at(h, t):
    # linear interpolation of the cumulative byte counter, clamped to the sampled range
    if t <= h[0][0]: return h[0][1]
    if t >= h[-1][0]: return h[-1][1]
    for (ta, ba), (tb, bb) in zip(h, h[1:]):
        if ta <= t <= tb:
            return ba + (bb - ba) * (t - ta) / max(1e-9, tb - ta)

def rate_mbit(h, t1, t2):
    return (cum_at(h, t2) - cum_at(h, t1)) * 8 / 1e6 / max(1e-9, t2 - t1)

def jain(xs):
    s, q = sum(xs), sum(x * x for x in xs)
    return s * s / (len(xs) * q) if q > 0 else float("nan")

def p5(xs):
    if len(xs) < 2: return xs[0] if xs else float("nan")
    return st.quantiles(xs, n=20, method="inclusive")[0]

def iperf_sum(f, secs=None):
    f.seek(0)
    try:
        s = json.loads(f.read())["end"]["sum_received"]
        return s["bytes"] * 8 / 1e9 / secs if secs else s["bits_per_second"] / 1e9
    except Exception:
        return float("nan")

def ssh(guest, cmd, out):
    return subprocess.Popen(["ssh", f"alpine@{GUESTS[guest][1]}", cmd], stdout=out, text=True)

def iperf_cmd(guest, secs, par=1, delay=0):
    pre = f"sleep {delay}; " if delay else ""
    p = f" -P {par}" if par > 1 else ""
    return f"{pre}iperf3 -c {HOST} -p {GUESTS[guest][2]} -t {secs}{p} -J"

# ---------- setup ----------

def ensure_servers():
    out = subprocess.run(["ss", "-ltnH"], capture_output=True, text=True).stdout
    for g in ("V", "A", "B"):
        port = GUESTS[g][2]
        if not any(l.split()[3].endswith(f":{port}") for l in out.splitlines() if len(l.split()) > 3):
            subprocess.run(["iperf3", "-s", "-D", "-p", str(port)], check=True)
            print(f"NOTE: no iperf3 server was listening on port {port}; started 'iperf3 -s -D -p {port}' "
                  f"(it keeps running after this script)", flush=True)

def reference(rep, procs):
    # victim alone, flooder caps unlimited, eBPF bytes on the victim vnet over the 10 s run
    for g in ("A", "B"):
        set_cap(IF[g], 0)   # undo the caps left by the previous repeat's last run
    ebpf, ip = [], []
    for i in range(REF_RUNS):
        out = tempfile.TemporaryFile("w+")
        c0 = counters(IF["V"])[2]
        p = ssh("V", iperf_cmd("V", REF_T), out); procs.append(p); p.wait()
        c1 = counters(IF["V"])[2]
        ebpf.append((c1 - c0) * 8 / 1e9 / REF_T); ip.append(iperf_sum(out)); out.close()
        print(f"rep {rep} ref run {i + 1}: victim alone {ebpf[-1]:.2f} Gbit/s (eBPF), {ip[-1]:.2f} Gbit/s (iperf3)", flush=True)
        time.sleep(PAUSE)
    ref = st.median(ebpf)
    if math.isnan(ref) or ref <= 0:
        raise SystemExit(f"reference measurement failed (rep {rep})")
    new = not os.path.exists(REF_FILE) or os.path.getsize(REF_FILE) == 0
    with open(REF_FILE, "a") as f:
        if new:
            f.write("# rep, time, ref_gbps (median eBPF), eBPF runs, iperf3 runs; victim alone, "
                    f"{REF_RUNS} x {REF_T} s at the start of each repeat\n")
        f.write(f"{rep}, {time.strftime('%Y-%m-%d %H:%M:%S')}, {ref:.3f}, "
                f"{' '.join(f'{x:.3f}' for x in ebpf)}, {' '.join(f'{x:.3f}' for x in ip)}\n")
        f.flush(); os.fsync(f.fileno())
    return ref

# ---------- one run ----------

def run(mode, param, ref, procs):
    ref_mbit = ref * 1000.0
    floor = float(param) / 100.0 * ref_mbit if mode == "slo" else None
    shared = INIT_CAP_MBIT
    if mode == "static":
        caps = [float(param) * 1000.0] * 2
    elif mode in ("pid", "ff"):
        caps = [shared / 2] * 2
    elif mode == "slo":
        caps = [INIT_CAP_MBIT] * 2
    else:
        caps = [CAP_MAX] * 2
    target = float(param) if mode in ("pid", "ff") else None
    # caps first, then the eBPF baseline, then the flooders: no flooder byte can precede the baseline
    set_cap(IF["A"], caps[0]); set_cap(IF["B"], caps[1])
    hist = {g: [] for g in IF}
    def sample():
        for g in IF:
            b = counters(IF[g])[2]; hist[g].append((time.monotonic(), b))
    sample()
    # temp files instead of pipes so large -J output can never block ssh/iperf3 mid-run
    fa, fb, vo = (tempfile.TemporaryFile("w+") for _ in range(3))
    alone = mode == "alone"   # victim only, no flooders; same timeline as the other modes
    t0 = time.monotonic()
    pa = None if alone else ssh("A", iperf_cmd("A", A_DUR, A_PAR, A_START), fa)
    pb = None if alone else ssh("B", iperf_cmd("B", B_DUR, B_PAR, B_START), fb)
    time.sleep(max(0.0, t0 + OFFSET - time.monotonic()))
    pv = ssh("V", iperf_cmd("V", T), vo)
    procs += [p for p in (pa, pb, pv) if p is not None]
    pt, pi = cpu(); cpus = []; e1 = 0.0; base = None; vfirst = None
    below = 0; last_cut = None   # slo: consecutive ticks below the floor, tick index of the last cut
    hold_ticks = round(HOLDOFF / DT)
    ts = []                      # one row per tick for ts_dyn10.csv
    k = 1
    while pv.poll() is None and time.monotonic() < t0 + OFFSET + T + 30:
        time.sleep(max(0.0, t0 + OFFSET + k * DT - time.monotonic())); k += 1
        prev = {g: hist[g][-1] for g in IF}
        sample(); tnow = hist["V"][-1][0]; t, i = cpu()
        busy = 100.0 * (1 - (i - pi) / max(1, (t - pt))); pt, pi = t, i
        cpus.append(busy)
        if vfirst is None and hist["V"][-1][1] > hist["V"][0][1]:
            vfirst = tnow   # first sample after victim bytes started flowing
        # flooder rates over the real elapsed time between counter reads, not the nominal DT
        tick = {g: (hist[g][-1][1] - prev[g][1]) * 8 / 1e6 / max(1e-3, hist[g][-1][0] - prev[g][0]) for g in IF}
        dt = max(1e-3, tnow - prev["V"][0])
        if mode == "pid":
            e = target - busy
            shared += KP * (e - e1) + KI * dt * e
            shared = max(CAP_MIN, min(CAP_MAX, shared)); e1 = e
            caps = [shared / 2] * 2
        elif mode == "ff":
            b = max(0.0, busy - K_CPU * (tick["A"] + tick["B"]) / 1000.0)
            base = b if base is None else 0.5 * base + 0.5 * b
            shared = max(CAP_MIN, min(CAP_MAX, (target - base) / K_CPU * 1000.0))
            caps = [shared / 2] * 2
        elif mode == "slo" and vfirst is not None and tnow - vfirst >= SMOOTH:
            # act only once the 1 s window lies entirely inside the victim's run
            v = rate_mbit(hist["V"], tnow - SMOOTH, tnow)
            below = below + 1 if v < floor else 0
            if v < floor:
                # cut only after SLO_CONSEC consecutive ticks below the floor and outside the hold-off
                # (counted in ticks so timing jitter cannot shorten it); otherwise hold the caps
                if below >= SLO_CONSEC and (last_cut is None or k - last_cut >= hold_ticks):
                    caps = [c * MD for c in caps]; last_cut = k
            else:
                caps = [c + AI_MBIT if rate_mbit(hist[g], tnow - SMOOTH, tnow) > SEND_MBIT else c
                        for c, g in zip(caps, ("A", "B"))]
            caps = [max(CAP_MIN, min(CAP_MAX, c)) for c in caps]
        if mode in ("pid", "ff", "slo"):
            set_cap(IF["A"], caps[0]); set_cap(IF["B"], caps[1])
        # t relative to flooder launch; caps as set at this tick; flooder rates over this tick
        ts.append((tnow - t0, rate_mbit(hist["V"], tnow - SMOOTH, tnow) / 1000.0,
                   caps[0], caps[1], tick["A"] / 1000.0, tick["B"] / 1000.0))
    pv.wait()
    if not alone: pa.wait(); pb.wait()
    # end of the eBPF window: after all iperf3 sessions have stopped, same bytes iperf3 counts
    sample()
    vmean = iperf_sum(vo)
    if alone:
        a_ip = a_eb = b_ip = b_eb = 0.0
    else:
        a_ip, b_ip = iperf_sum(fa, A_DUR), iperf_sum(fb, B_DUR)
        a_eb = (hist["A"][-1][1] - hist["A"][0][1]) * 8 / 1e9 / A_DUR
        b_eb = (hist["B"][-1][1] - hist["B"][0][1]) * 8 / 1e9 / B_DUR
    for f in (fa, fb, vo): f.close()
    # 1 s victim windows from the first sample with victim bytes; T-1 windows stay inside the run
    wins = []
    if vfirst is not None:
        wins = [rate_mbit(hist["V"], vfirst + j, vfirst + j + 1) / 1000.0 for j in range(int(T) - 1)]
    vp5 = p5(wins)
    ok = sum(w >= SLO_FRAC * ref for w in wins) / len(wins) if wins else float("nan")
    ra = rate_mbit(hist["A"], t0 + OVERLAP[0], t0 + OVERLAP[1])
    rb = rate_mbit(hist["B"], t0 + OVERLAP[0], t0 + OVERLAP[1])
    jf = float("nan") if alone else jain([ra, rb])   # no flooders: fairness undefined
    return (vmean, vp5, ok, a_ip, a_eb, b_ip, b_eb, jf,
            st.mean(cpus) if cpus else float("nan"), max(cpus) if cpus else float("nan")), ts

def ms(xs):
    xs = [x for x in xs if not math.isnan(x)]
    if not xs: return float("nan"), float("nan")
    return st.mean(xs), (st.stdev(xs) if len(xs) > 1 else 0.0)

def main():
    random.seed(7)
    configs = ([("static", c) for c in ("1", "3")]
               + [("pid", "75"), ("pid", "85"), ("ff", "75"), ("ff", "85")]
               + [("slo", str(f)) for f in SLO_FLOORS])
    ALONE = ("alone", "na")   # victim-only control, run before the first config and after every config
    res = {c: [] for c in configs + [ALONE]}
    try:
        sudo(["true"])
    except subprocess.CalledProcessError:
        raise SystemExit("sudo needs a password: run 'sudo -v' first (run10.sh does this)")
    if not os.path.exists(OBJ):
        raise SystemExit(f"{OBJ} not found")
    new = not os.path.exists(CSV) or os.path.getsize(CSV) == 0
    if not new and open(CSV).readline().strip() != HEADER:
        raise SystemExit(f"{CSV} exists with a different header; not appending to it")
    ts_new = not os.path.exists(TS_CSV) or os.path.getsize(TS_CSV) == 0
    if not ts_new and open(TS_CSV).readline().strip() != TS_HEADER:
        raise SystemExit(f"{TS_CSV} exists with a different header; not appending to it")
    ensure_servers()
    procs = []
    refs = []         # reference used in each repeat
    csv = open(CSV, "a")
    tsf = open(TS_CSV, "a")
    try:
        for g in ("V", "A", "B"):
            IF[g] = attach(vnet(GUESTS[g][0]), 0)   # all unlimited; victim stays 0 (counter only)
        if new:
            csv.write(HEADER + "\n"); csv.flush()
        if ts_new:
            tsf.write(TS_HEADER + "\n"); tsf.flush()
        print(f"{REPS} repeats x ({len(configs)} configs + {len(configs) + 1} interleaved alone runs), "
              f"writing {CSV} and {TS_CSV}; reference re-measured at the start of every repeat, "
              f"{COOLDOWN} s cool-down between repeats, {SESSION_REST_S} s rest after every "
              f"{SESSION_REPEATS} repeats", flush=True)
        for rep in range(1, REPS + 1):
            if rep > 1:
                print(f"cool-down {COOLDOWN} s before repeat {rep}", flush=True)
                time.sleep(COOLDOWN)
            # reference first (consumes no random numbers), then this repeat's shuffled order
            ref = reference(rep, procs); refs.append(ref)
            print(f"REFERENCE rep {rep}: victim rate {ref:.3f} Gbit/s (median of {REF_RUNS} x {REF_T} s, eBPF), "
                  f"slo_ok threshold {SLO_FRAC * ref:.3f} Gbit/s, slo floors "
                  + ", ".join(f"{f}% = {f / 100 * ref:.3f}" for f in SLO_FLOORS)
                  + f" Gbit/s, saved to {REF_FILE}", flush=True)
            print("rep slot mode   param v_mean  v_p5  slo_ok A_iperf A_ebpf B_iperf B_ebpf  jain cpu_mean cpu_max", flush=True)
            order = configs[:]; random.shuffle(order)
            seq = [ALONE] + [x for c in order for x in (c, ALONE)]   # alone, c1, alone, c2, ..., c7, alone
            for slot, (m, p) in enumerate(seq):
                r, ts = run(m, p, ref, procs); res[(m, p)].append(r)
                csv.write(f"{rep},{slot},{m},{p},{r[0]:.3f},{r[1]:.3f},{r[2]:.3f},{r[3]:.3f},{r[4]:.3f},"
                          f"{r[5]:.3f},{r[6]:.3f},{r[7]:.4f},{r[8]:.2f},{r[9]:.2f},{ref:.3f}\n"); csv.flush()
                os.fsync(csv.fileno())
                tsf.write("".join(f"{rep},{slot},{m},{p},{t:.3f},{v:.3f},{ca:.1f},{cb:.1f},{a:.3f},{b:.3f}\n"
                                  for t, v, ca, cb, a, b in ts)); tsf.flush()
                os.fsync(tsf.fileno())
                print(f"{rep:3d} {slot:4d} {m:6s} {p:>5s} {r[0]:6.2f} {r[1]:5.2f} {r[2]:6.2f} {r[3]:7.2f} {r[4]:6.2f} "
                      f"{r[5]:7.2f} {r[6]:6.2f} {r[7]:5.3f} {r[8]:8.1f} {r[9]:7.1f}", flush=True)
                time.sleep(PAUSE)
            if rep % SESSION_REPEATS == 0 and rep < REPS:
                print(f"session rest {SESSION_REST_S} s", flush=True)
                time.sleep(SESSION_REST_S)
    finally:
        for p in procs:
            if p.poll() is None: p.terminate()
        for d in IF.values():
            last_cap.pop(d["cfg"], None)   # force the write
            try:
                set_cap(d, 0)
            except Exception as ex:
                print(f"WARNING: could not reset cap on {d['dev']} (cfg map id {d['cfg']}): {ex}", flush=True)
        csv.close(); tsf.close()

    names = ["v_mean", "v_p5", "slo_ok", "A_iperf", "A_ebpf", "B_iperf", "B_ebpf", "jain", "cpu_mean", "cpu_max"]
    print("\nREFERENCES per repeat (Gbit/s): " + ", ".join(f"rep {i + 1} = {r:.3f}" for i, r in enumerate(refs)))
    print(f"\nSUMMARY (mean +- sd over {REPS} repeats; alone pooled over all {len(configs) + 1} slots per repeat)")
    print("mode   param    n " + " ".join(f"{n:>15s}" for n in names))
    for c in configs + [ALONE]:
        cols = [ms([r[j] for r in res[c]]) for j in range(len(names))]
        print(f"{c[0]:6s} {c[1]:>5s} {len(res[c]):4d} " + " ".join(f"{a:6.2f} +- {b:5.2f}" for a, b in cols))

if __name__ == "__main__":
    main()
