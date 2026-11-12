import json, subprocess, time, struct, random, statistics as st, sys, os, math, tempfile
REPS = int(sys.argv[1]) if len(sys.argv) > 1 else 5
T, DT = 22, 0.25
KP, KI = 200.0, 300.0
CAP_MIN, CAP_MAX = 200.0, 70000.0
K_CPU = 0.93            # % host CPU per Gbit/s (from the Step 19 measurement)
CSV = "results_pareto.csv"
SEP = "@@@IPERF_END@@@"

def counters():
    out = subprocess.run(["sudo","bpftool","-j","map","dump","name","cnt"],
                         capture_output=True, text=True).stdout
    d = {}
    for e in json.loads(out):
        f = e.get("formatted", e); v = f["value"]
        d[f["key"]] = sum(v) if isinstance(v, list) else v
    return d

def cpu():
    f = list(map(int, open("/proc/stat").readline().split()[1:]))
    return sum(f), f[3] + f[4]

def set_cap(mbps):
    h = [f"{x:02x}" for x in struct.pack("<Q", int(mbps * 1e6 / 8))]
    subprocess.run(["sudo","bpftool","map","update","name","cfg",
                    "key","0","0","0","0","value","hex"] + h)

def flooder_gbps(text):
    # total bytes received over the 4 flooder sessions, averaged over T like the eBPF figure
    parts = [p.strip() for p in text.split(SEP) if p.strip()]
    if len(parts) != 4:
        return float("nan")
    try:
        return sum(json.loads(p)["end"]["sum_received"]["bytes"] for p in parts) * 8 / 1e9 / T
    except Exception:
        return float("nan")

def run(mode, param):
    if mode == "static":
        cap = float(param) * 1000.0
    else:
        cap = CAP_MAX
    target = float(param) if mode in ("pid", "ff") else None
    set_cap(cap)
    # temp files instead of pipes so large -J output can never block ssh/iperf3 mid-run
    fout, vout = tempfile.TemporaryFile("w+"), tempfile.TemporaryFile("w+")
    flood = None
    if mode != "alone":
        flood = subprocess.Popen(["ssh","alpine@10.10.0.12",
            f"for i in 1 2 3 4; do iperf3 -c 10.10.0.1 -p 5202 -t 3 -P 4 -J; echo '{SEP}'; sleep 3; done"],
            stdout=fout, text=True)
    time.sleep(0.5)
    victim = subprocess.Popen(["ssh","alpine@10.10.0.11",
        f"iperf3 -c 10.10.0.1 -t {T} -J"], stdout=vout, text=True)
    c0 = prev = counters(); pt, pi = cpu()
    e1 = 0.0; base = None; cpus = []
    for s in range(int(T / DT)):
        time.sleep(DT)
        cur = counters(); t, i = cpu()
        busy = 100.0 * (1 - (i - pi) / max(1, (t - pt))); pt, pi = t, i
        passed = (cur[2] - prev[2]) * 8 / 1e6 / DT; prev = cur
        cpus.append(busy)
        if mode == "pid":
            e = target - busy
            cap += KP * (e - e1) + KI * DT * e
            cap = max(CAP_MIN, min(CAP_MAX, cap)); set_cap(cap); e1 = e
        elif mode == "ff":
            b = max(0.0, busy - K_CPU * passed / 1000.0)
            base = b if base is None else 0.5 * base + 0.5 * b
            cap = max(CAP_MIN, min(CAP_MAX, (target - base) / K_CPU * 1000.0))
            set_cap(cap)
    c1 = counters()
    victim.wait()
    vout.seek(0)
    try:
        v = json.loads(vout.read())["end"]["sum_received"]["bits_per_second"] / 1e9
    except Exception:
        v = float("nan")
    if flood is not None:
        flood.wait(); fout.seek(0)
        fi = flooder_gbps(fout.read())
    else:
        fi = 0.0
    fout.close(); vout.close()
    return v, st.mean(cpus), max(cpus), (c1[2] - c0[2]) * 8 / 1e9 / T, fi

def ms(xs):
    xs = [x for x in xs if not math.isnan(x)]
    if not xs: return float("nan"), float("nan")
    return st.mean(xs), (st.stdev(xs) if len(xs) > 1 else 0.0)

random.seed(7)
configs = ([("alone", "na"), ("none", "na")]
           + [("static", c) for c in ("2", "5", "10")]
           + [("pid", c) for c in ("45", "55", "65")]
           + [("ff", c) for c in ("45", "55", "65")])
res = {c: [] for c in configs}
new = not os.path.exists(CSV) or os.path.getsize(CSV) == 0
csv = open(CSV, "a")
if new:
    csv.write("rep,mode,param,victim_gbps,cpu_mean,cpu_max,flooder_ebpf_gbps,flooder_iperf_gbps\n"); csv.flush()
print(f"{REPS} repeats x {len(configs)} configs, writing {CSV}", flush=True)
print("rep mode   param victim_Gbps mean_cpu% max_cpu% fl_ebpf_Gbps fl_iperf_Gbps", flush=True)
try:
    for rep in range(1, REPS + 1):
        order = configs[:]; random.shuffle(order)
        for m, p in order:
            r = run(m, p); res[(m, p)].append(r)
            csv.write(f"{rep},{m},{p},{r[0]:.3f},{r[1]:.2f},{r[2]:.2f},{r[3]:.3f},{r[4]:.3f}\n"); csv.flush()
            os.fsync(csv.fileno())
            print(f"{rep:3d} {m:6s} {p:>5s} {r[0]:10.1f} {r[1]:9.1f} {r[2]:8.1f} {r[3]:12.2f} {r[4]:13.2f}", flush=True)
            time.sleep(8)
finally:
    set_cap(0)
    csv.close()

print(f"\nSUMMARY (mean +- sd over {REPS} repeats)")
print("mode   param   victim_Gbps      cpu_mean%       cpu_max%        fl_ebpf_Gbps    fl_iperf_Gbps")
for c in configs:
    cols = [ms([r[j] for r in res[c]]) for j in range(5)]
    print(f"{c[0]:6s} {c[1]:>5s}  " + "  ".join(f"{a:6.2f} +- {b:5.2f}" for a, b in cols))
