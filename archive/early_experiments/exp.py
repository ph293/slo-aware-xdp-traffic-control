import json, subprocess, time, struct, statistics as st
TARGET, T = 50.0, 22
KP, KI = 200.0, 300.0
CAP_MIN, CAP_MAX = 200.0, 70000.0
A, B = 0.8, 0.5

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
    cap = CAP_MAX; set_cap(cap)
    flood = subprocess.Popen(["ssh","alpine@10.10.0.12",
        "for i in 1 2 3 4; do iperf3 -c 10.10.0.1 -p 5202 -t 3 -P 4 >/dev/null; sleep 3; done"])
    time.sleep(0.5)
    victim = subprocess.Popen(["ssh","alpine@10.10.0.11",
        f"iperf3 -c 10.10.0.1 -t {T} -J"], stdout=subprocess.PIPE, text=True)
    c0, (pt, pi) = counters(), cpu()
    e1 = 0.0; lvl = None; tr = 0.0; cpus = []
    for s in range(T):
        time.sleep(1)
        (t, i) = cpu()
        busy = 100.0 * (1 - (i - pi) / max(1, (t - pt))); pt, pi = t, i
        cpus.append(busy)
        if mode != "none":
            x = busy
            if mode == "holt":
                if lvl is None: lvl = busy
                new = A * busy + (1 - A) * (lvl + tr)
                tr = B * (new - lvl) + (1 - B) * tr
                lvl = new; x = lvl + tr
            e = TARGET - x
            cap += KP * (e - e1) + KI * e
            cap = max(CAP_MIN, min(CAP_MAX, cap)); set_cap(cap); e1 = e
    c1 = counters()
    out = victim.communicate()[0]; flood.wait()
    try:
        v = json.loads(out)["end"]["sum_received"]["bits_per_second"] / 1e9
    except Exception:
        v = float("nan")
    passed = (c1[2] - c0[2]) * 8 / 1e9 / T
    return v, st.mean(cpus), max(cpus), passed

res = {"none": [], "pid": [], "holt": []}
print("mode  victim_Gbps  mean_cpu_%  max_cpu_%  flooder_passed_Gbps")
for rep in range(3):
    for mode in ["none", "pid", "holt"]:
        r = run(mode); res[mode].append(r)
        print(f"{mode:5s} {r[0]:10.1f} {r[1]:11.1f} {r[2]:10.1f} {r[3]:18.1f}", flush=True)
        time.sleep(3)
set_cap(0)
print("\nSUMMARY (mean of 3 runs)")
for m, rs in res.items():
    print(f"{m:5s} victim={st.mean(r[0] for r in rs):5.1f} Gbps  cpu_mean={st.mean(r[1] for r in rs):5.1f}%  cpu_max={st.mean(r[2] for r in rs):5.1f}%  flooder={st.mean(r[3] for r in rs):5.1f} Gbps")
