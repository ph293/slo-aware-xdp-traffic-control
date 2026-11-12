import json, subprocess, time, struct, random, statistics as st
TARGET, T, DT = 50.0, 22, 0.25
KP, KI = 200.0, 300.0
CAP_MIN, CAP_MAX = 200.0, 70000.0
A, B = 0.8, 0.5
K_CPU = 0.93            # % host CPU per Gbit/s (from the Step 19 measurement)

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

def run(mode):
    cap = 10000.0 if mode == "static" else CAP_MAX
    set_cap(cap)
    flood = subprocess.Popen(["ssh","alpine@10.10.0.12",
        "for i in 1 2 3 4; do iperf3 -c 10.10.0.1 -p 5202 -t 3 -P 4 >/dev/null; sleep 3; done"])
    time.sleep(0.5)
    victim = subprocess.Popen(["ssh","alpine@10.10.0.11",
        f"iperf3 -c 10.10.0.1 -t {T} -J"], stdout=subprocess.PIPE, text=True)
    c0 = prev = counters(); pt, pi = cpu()
    e1 = 0.0; lvl = None; tr = 0.0; base = None; cpus = []
    for s in range(int(T / DT)):
        time.sleep(DT)
        cur = counters(); t, i = cpu()
        busy = 100.0 * (1 - (i - pi) / max(1, (t - pt))); pt, pi = t, i
        passed = (cur[2] - prev[2]) * 8 / 1e6 / DT; prev = cur
        cpus.append(busy)
        if mode in ("pid", "holt"):
            x = busy
            if mode == "holt":
                if lvl is None: lvl = busy
                new = A * busy + (1 - A) * (lvl + tr)
                tr = B * (new - lvl) + (1 - B) * tr
                lvl = new; x = lvl + tr
            e = TARGET - x
            cap += KP * (e - e1) + KI * DT * e
            cap = max(CAP_MIN, min(CAP_MAX, cap)); set_cap(cap); e1 = e
        elif mode == "ff":
            b = max(0.0, busy - K_CPU * passed / 1000.0)
            base = b if base is None else 0.5 * base + 0.5 * b
            cap = max(CAP_MIN, min(CAP_MAX, (TARGET - base) / K_CPU * 1000.0))
            set_cap(cap)
    c1 = counters()
    out = victim.communicate()[0]; flood.wait()
    try:
        v = json.loads(out)["end"]["sum_received"]["bits_per_second"] / 1e9
    except Exception:
        v = float("nan")
    return v, st.mean(cpus), max(cpus), (c1[2] - c0[2]) * 8 / 1e9 / T

random.seed(7)
modes = ["none", "static", "pid", "holt", "ff"]
res = {m: [] for m in modes}
csv = open("results_testbed.csv", "w")
csv.write("rep,mode,victim_gbps,cpu_mean,cpu_max,flooder_gbps\n")
print("rep mode   victim_Gbps mean_cpu% max_cpu% flooder_Gbps")
for rep in range(1, 6):
    order = modes[:]; random.shuffle(order)
    for m in order:
        r = run(m); res[m].append(r)
        csv.write(f"{rep},{m},{r[0]:.3f},{r[1]:.2f},{r[2]:.2f},{r[3]:.3f}\n"); csv.flush()
        print(f"{rep:3d} {m:6s} {r[0]:10.1f} {r[1]:9.1f} {r[2]:8.1f} {r[3]:11.1f}", flush=True)
        time.sleep(8)
set_cap(0)
print("\nSUMMARY (mean +- sd over 5 repeats)")
for m in modes:
    v = [r[0] for r in res[m]]
    print(f"{m:6s} victim={st.mean(v):5.1f} +- {st.stdev(v):3.1f} Gbps  cpu_mean={st.mean(r[1] for r in res[m]):5.1f}%  cpu_max={st.mean(r[2] for r in res[m]):5.1f}%  flooder={st.mean(r[3] for r in res[m]):4.1f}")
print("\nPAIRED vs pid (victim Gbps, same repeat): mean diff, repeats better")
for m in modes:
    if m == "pid": continue
    d = [res[m][k][0] - res["pid"][k][0] for k in range(5)]
    print(f"{m:6s} {st.mean(d):+5.2f}   better in {sum(x > 0 for x in d)} of 5")
