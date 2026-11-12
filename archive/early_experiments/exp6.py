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
SLO_FRAC = 0.95         # victim floor F = SLO_FRAC * reference rate
MD, AI_MBIT = 0.7, 300.0
SEND_MBIT = 100.0       # a flooder counts as "sending" above this rate
SMOOTH = 1.0            # s, victim/flooder rate smoothing window for the slo controller
REF_RUNS, REF_T = 3, 10
PAUSE = 8
HOST = "10.10.0.1"
GUESTS = {"V": ("tenant1", "10.10.0.11", 5201),   # victim
          "A": ("tenant2", "10.10.0.12", 5202),   # flooder A
          "B": ("tenant3", "10.10.0.13", 5203)}   # flooder B
HERE = os.path.dirname(os.path.abspath(__file__))
OBJ = os.path.join(HERE, "xdp_limit.o")
CSV = os.path.join(HERE, "results_dyn.csv")
REF_FILE = os.path.join(HERE, "ref_victim.txt")
HEADER = ("rep,mode,param,victim_mean_gbps,victim_p5_gbps,slo_ok_fraction,"
          "flooderA_iperf_gbps,flooderA_ebpf_gbps,flooderB_iperf_gbps,flooderB_ebpf_gbps,"
          "jain_fairness,cpu_mean,cpu_max,ref_gbps")

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

def reference(procs):
    # victim alone, flooder caps unlimited, eBPF bytes on the victim vnet over the 10 s run
    ebpf, ip = [], []
    for i in range(REF_RUNS):
        out = tempfile.TemporaryFile("w+")
        c0 = counters(IF["V"])[2]
        p = ssh("V", iperf_cmd("V", REF_T), out); procs.append(p); p.wait()
        c1 = counters(IF["V"])[2]
        ebpf.append((c1 - c0) * 8 / 1e9 / REF_T); ip.append(iperf_sum(out)); out.close()
        print(f"ref run {i + 1}: victim alone {ebpf[-1]:.2f} Gbit/s (eBPF), {ip[-1]:.2f} Gbit/s (iperf3)", flush=True)
        time.sleep(PAUSE)
    ref = st.median(ebpf)
    if math.isnan(ref) or ref <= 0:
        raise SystemExit("reference measurement failed")
    new = not os.path.exists(REF_FILE) or os.path.getsize(REF_FILE) == 0
    with open(REF_FILE, "a") as f:
        if new:
            f.write("# time, ref_gbps (median eBPF), eBPF runs, iperf3 runs; victim alone, "
                    f"{REF_RUNS} x {REF_T} s\n")
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}, {ref:.3f}, "
                f"{' '.join(f'{x:.3f}' for x in ebpf)}, {' '.join(f'{x:.3f}' for x in ip)}\n")
        f.flush(); os.fsync(f.fileno())
    return ref

# ---------- one run ----------

def run(mode, param, ref, procs):
    ref_mbit = ref * 1000.0
    floor = SLO_FRAC * ref_mbit
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
    t0 = time.monotonic()
    pa = ssh("A", iperf_cmd("A", A_DUR, A_PAR, A_START), fa)
    pb = ssh("B", iperf_cmd("B", B_DUR, B_PAR, B_START), fb)
    time.sleep(max(0.0, t0 + OFFSET - time.monotonic()))
    pv = ssh("V", iperf_cmd("V", T), vo)
    procs += [pa, pb, pv]
    pt, pi = cpu(); cpus = []; e1 = 0.0; base = None; vfirst = None
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
            if v < floor:
                caps = [c * MD for c in caps]
            else:
                caps = [c + AI_MBIT if rate_mbit(hist[g], tnow - SMOOTH, tnow) > SEND_MBIT else c
                        for c, g in zip(caps, ("A", "B"))]
            caps = [max(CAP_MIN, min(CAP_MAX, c)) for c in caps]
        if mode in ("pid", "ff", "slo"):
            set_cap(IF["A"], caps[0]); set_cap(IF["B"], caps[1])
    pv.wait(); pa.wait(); pb.wait()
    # end of the eBPF window: after all iperf3 sessions have stopped, same bytes iperf3 counts
    sample()
    vmean = iperf_sum(vo)
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
    return (vmean, vp5, ok, a_ip, a_eb, b_ip, b_eb, jain([ra, rb]),
            st.mean(cpus) if cpus else float("nan"), max(cpus) if cpus else float("nan"))

def ms(xs):
    xs = [x for x in xs if not math.isnan(x)]
    if not xs: return float("nan"), float("nan")
    return st.mean(xs), (st.stdev(xs) if len(xs) > 1 else 0.0)

def main():
    random.seed(7)
    configs = ([("none", "na")]
               + [("static", c) for c in ("2", "5")]
               + [("pid", "65"), ("ff", "65"), ("slo", f"{SLO_FRAC * 100:.0f}")])
    res = {c: [] for c in configs}
    try:
        sudo(["true"])
    except subprocess.CalledProcessError:
        raise SystemExit("sudo needs a password: run 'sudo -v' first (run6.sh does this)")
    if not os.path.exists(OBJ):
        raise SystemExit(f"{OBJ} not found")
    new = not os.path.exists(CSV) or os.path.getsize(CSV) == 0
    if not new and open(CSV).readline().strip() != HEADER:
        raise SystemExit(f"{CSV} exists with a different header; not appending to it")
    ensure_servers()
    procs = []
    csv = open(CSV, "a")
    try:
        for g in ("V", "A", "B"):
            IF[g] = attach(vnet(GUESTS[g][0]), 0)   # all unlimited; victim stays 0 (counter only)
        ref = reference(procs)
        print(f"REFERENCE victim rate {ref:.3f} Gbit/s (median of {REF_RUNS} x {REF_T} s, eBPF), "
              f"SLO floor {SLO_FRAC * ref:.3f} Gbit/s, saved to {REF_FILE}", flush=True)
        if new:
            csv.write(HEADER + "\n"); csv.flush()
        print(f"{REPS} repeats x {len(configs)} configs, writing {CSV}", flush=True)
        print("rep mode   param v_mean  v_p5  slo_ok A_iperf A_ebpf B_iperf B_ebpf  jain cpu_mean cpu_max", flush=True)
        for rep in range(1, REPS + 1):
            order = configs[:]; random.shuffle(order)
            for m, p in order:
                r = run(m, p, ref, procs); res[(m, p)].append(r)
                csv.write(f"{rep},{m},{p},{r[0]:.3f},{r[1]:.3f},{r[2]:.3f},{r[3]:.3f},{r[4]:.3f},"
                          f"{r[5]:.3f},{r[6]:.3f},{r[7]:.4f},{r[8]:.2f},{r[9]:.2f},{ref:.3f}\n"); csv.flush()
                os.fsync(csv.fileno())
                print(f"{rep:3d} {m:6s} {p:>5s} {r[0]:6.2f} {r[1]:5.2f} {r[2]:6.2f} {r[3]:7.2f} {r[4]:6.2f} "
                      f"{r[5]:7.2f} {r[6]:6.2f} {r[7]:5.3f} {r[8]:8.1f} {r[9]:7.1f}", flush=True)
                time.sleep(PAUSE)
    finally:
        for p in procs:
            if p.poll() is None: p.terminate()
        for d in IF.values():
            last_cap.pop(d["cfg"], None)   # force the write
            try:
                set_cap(d, 0)
            except Exception as ex:
                print(f"WARNING: could not reset cap on {d['dev']} (cfg map id {d['cfg']}): {ex}", flush=True)
        csv.close()

    names = ["v_mean", "v_p5", "slo_ok", "A_iperf", "A_ebpf", "B_iperf", "B_ebpf", "jain", "cpu_mean", "cpu_max"]
    print(f"\nSUMMARY (mean +- sd over {REPS} repeats)")
    print("mode   param " + " ".join(f"{n:>15s}" for n in names))
    for c in configs:
        cols = [ms([r[j] for r in res[c]]) for j in range(len(names))]
        print(f"{c[0]:6s} {c[1]:>5s} " + " ".join(f"{a:6.2f} +- {b:5.2f}" for a, b in cols))

if __name__ == "__main__":
    main()
