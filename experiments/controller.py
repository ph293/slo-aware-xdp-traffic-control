import json, subprocess, time, struct, sys

TARGET = float(sys.argv[1]) if len(sys.argv) > 1 else 50.0
SECS = int(sys.argv[2]) if len(sys.argv) > 2 else 20
KP, KI = 200.0, 300.0          # Mbit/s of cap per 1% CPU error
CAP_MIN, CAP_MAX = 200.0, 70000.0

def counters():
    out = subprocess.run(["sudo","bpftool","-j","map","dump","name","cnt"],
                         capture_output=True, text=True).stdout
    d = {}
    for e in json.loads(out):
        f = e.get("formatted", e)
        v = f["value"]; k = f["key"]
        d[k] = sum(v) if isinstance(v, list) else v
    return d

def cpu():
    f = list(map(int, open("/proc/stat").readline().split()[1:]))
    return sum(f), f[3] + f[4]

def set_cap(mbps):
    b = int(mbps * 1e6 / 8)
    h = [f"{x:02x}" for x in struct.pack("<Q", b)]
    subprocess.run(["sudo","bpftool","map","update","name","cfg",
                    "key","0","0","0","0","value","hex"] + h)

cap = CAP_MAX
set_cap(cap)
prev, (pt, pi) = counters(), cpu()
e1 = 0.0
print("sec  offered_Mbps passed_Mbps cap_Mbps host_cpu_%")
for s in range(1, SECS + 1):
    time.sleep(1)
    cur, (t, i) = counters(), cpu()
    off = (cur[2] + cur[3] - prev[2] - prev[3]) * 8 / 1e6
    pas = (cur[2] - prev[2]) * 8 / 1e6
    busy = 100.0 * (1 - (i - pi) / max(1, (t - pt)))
    e = TARGET - busy
    cap += KP * (e - e1) + KI * e
    cap = max(CAP_MIN, min(CAP_MAX, cap))
    set_cap(cap)
    print(f"{s:3d}  {off:11.0f} {pas:11.0f} {cap:8.0f} {busy:9.1f}")
    prev, pt, pi, e1 = cur, t, i, e
