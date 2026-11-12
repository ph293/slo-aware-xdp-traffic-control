import json, subprocess, time

def counters():
    out = subprocess.run(["sudo", "bpftool", "-j", "map", "dump", "name", "cnt"],
                         capture_output=True, text=True).stdout
    d = {}
    for e in json.loads(out):
        v = e.get("formatted", e).get("value") if "formatted" in e else e["value"]
        k = e.get("formatted", e).get("key") if "formatted" in e else e["key"]
        if isinstance(v, list):
            v = sum(v)
        d[k] = v
    return d

def cpu():
    f = open("/proc/stat").readline().split()[1:]
    f = list(map(int, f))
    return sum(f), f[3] + f[4]

prev, (pt, pi) = counters(), cpu()
print("sec  offered_Mbps  passed_Mbps  dropped_pkts  host_cpu_%")
for s in range(1, 11):
    time.sleep(1)
    cur, (t, i) = counters(), cpu()
    off = (cur[2] + cur[3] - prev[2] - prev[3]) * 8 / 1e6
    pas = (cur[2] - prev[2]) * 8 / 1e6
    drp = cur[1] - prev[1]
    busy = 100.0 * (1 - (i - pi) / max(1, (t - pt)))
    print(f"{s:3d}  {off:12.0f}  {pas:11.0f}  {drp:12d}  {busy:9.1f}")
    prev, pt, pi = cur, t, i
