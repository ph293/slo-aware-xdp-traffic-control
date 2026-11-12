import json, subprocess, time, struct, random, statistics as st, sys, os, math, tempfile
import numpy as np
REPS = int(sys.argv[1]) if len(sys.argv) > 1 else 5
T, DT = 40, 0.25
OFFSET = 0.5            # victim starts 0.5 s after the flooders are launched (as exp5)
A_START, A_DUR, A_PAR = 0, 27, 4    # flooder A (tenant2): t=0..27 s, 4 streams
B_START, B_DUR, B_PAR = 13, 27, 2   # flooder B (tenant3): t=13..40 s, 2 streams
OVERLAP = (13.0, 27.0)  # both flooders active: window for Jain fairness (t relative to flooder launch)
SCENARIO = "burst"      # "steady" = exp23 continuous flooders (A 0-27 s, B 13-40 s); "burst" = seeded on/off bursts within T
BURST_ON = (3.0, 6.0)   # burst: on-burst duration drawn uniformly in this range (s)
BURST_OFF = (2.0, 5.0)  # burst: off-gap duration drawn uniformly in this range (s)
BURST_START = (0.0, 4.0)  # burst: first on-burst starts at a random offset in this range (s)
KP, KI = 200.0, 300.0
CAP_MIN, CAP_MAX = 200.0, 70000.0
INIT_CAP_MBIT = 2000.0  # pid/ff: initial shared cap (split equally); slo: initial cap of each flooder
K_CPU = 0.93            # % host CPU per Gbit/s (from the Step 19 measurement)
VICTIM_RATE_GBIT = float(os.environ.get("VICTIM_RATE_GBIT", 25))  # victim offered rate, total over its streams
SLO_FRAC = 0.95         # slo_ok_fraction threshold (all modes): SLO_FRAC * reference rate
SLO_FLOORS = (95,)      # slo controller floors, % of the reference rate (one config each)
MD, AI_MBIT = 0.7, 500.0   # slo: cut factor; additive increase per tick for a sending flooder
SLO_CONSEC = 2          # slo: cut only after this many consecutive ticks below the floor
HOLDOFF = 1.0           # s, slo: no further cut for this long after a cut
SEND_MBIT = 100.0       # a flooder counts as "sending" above this rate
SMOOTH = 1.0            # s, victim/flooder rate smoothing window for the slo controller
HOLT_ALPHA, HOLT_BETA = 0.5, 0.3   # dfs: Holt level / trend smoothing of each flooder's offered rate
DFS_ACTIVE_MBIT = 50.0  # dfs: a flooder whose forecast demand is below this is inactive (gets CAP_MIN)
DFS_FLOOR = 95          # dfs: victim guard floor, % of the per-repeat reference (as slo 95)
DFS_CUT, DFS_RECOVER = 0.8, 0.05   # dfs: budget x0.8 per tick below the floor, else +5% of the ff budget
DFS2_TARGET = 75.0       # dfs2: CPU target % for the ff feed-forward budget (same target as the ff 75 config)
DFS2_WARMUP = 5.0        # s, dfs2: victim guard disabled for the first this many seconds of the run
DFS2_VS_TAU = 3.0        # s, dfs2: EMA time constant of the smoothed 1 s victim rate v_s
DFS2_VATT_TAU = 5.0      # s, dfs2: EMA time constant of the attainable-rate estimate v_att
DFS2_VATT_INIT = 0.98    # dfs2: v_att initialised to this fraction of the per-repeat reference (rate-limited
                         # victim should nearly reach its offered rate)
DFS3_VS_TAU = 1.5        # s, dfs3: EMA time constant of the smoothed 1 s victim rate v_s
DFS3_KI_DOWN = 3.0       # dfs3: integral gain of the smooth victim guard when below target (per s, x ffb)
DFS3_UP_RATE = 0.05      # dfs3: budget increase when at/above target, fraction of ffb per second
DFS4 = {}                # dfs4: param string -> (slack %, warm, minshare), parsed once in main()
ROT = {}                 # rot: param string -> (total Gbit/s, rotation period s), parsed once in main()
PIDROT = {}              # pidrot: param string -> (host CPU target %, rotation period s), parsed once in main()
PIDROTF = {}             # pidrotf: param string -> (host CPU target %, rotation period s), parsed once in main()
VFROT = {}               # vfrot: param string -> (beta, add Gbit/s per s, rotation period s, thresh), parsed once in main()
VFROT_VS_TAU = 0.5       # s, vfrot: EMA time constant of the smoothed 1 s victim rate v
VFROT_THRESH = 0.98      # vfrot: default violation threshold (3-part param): v < this x the per-repeat reference
VFROT_HOLD = 1.0         # s, vfrot: no budget increase for this long after a violation tick
VFROT_CUT_HOLD = 1.0     # s, vfrot: no further cut for this long after a cut (the 1 s victim window lags recovery)
DFS5 = {}                # dfs5: param string -> (slack %, rotation period s), parsed once in main()
DFS5_WARM = 0.3          # dfs5: warm-start budget, fraction of ffb (as dfs4 "3_0.3_0.4")
LQR = {}                 # lqr: param string -> (host CPU target %, R weight multiplier, gain K 1x2), set in main()
LQR_TAU = 0.5            # s, lqr plant time constant: y(k+1) = a*y(k) + b*u(k), a = exp(-DT/LQR_TAU),
                         # b = K_CPU*(1-a) (steady-state gain K_CPU, the same constant as pid/ff)
LQR_Q = (1.0, 1.0)       # lqr: state weights Q = diag(q_e, q_d)
LQR_R = 0.5              # lqr: input weight on du (Gbit/s); R = LQR_R * RWT
REF_RUNS, REF_T = 2, 10   # victim-only reference, measured at the start of EVERY repeat
PAUSE = 8
COOLDOWN = 45           # s, extra cool-down pause between repeats (thermal), on top of PAUSE
SESSION_REPEATS = 1     # repeats per session; a long rest follows each session except the last
SESSION_REST_S = 600    # s, thermal rest between sessions, on top of COOLDOWN
HOST = "10.10.0.1"
GUESTS = {"V": ("tenant1", "10.10.0.11", 5201),   # victim
          "A": ("tenant2", "10.10.0.12", 5202),   # flooder A
          "B": ("tenant3", "10.10.0.13", 5203)}   # flooder B
HERE = os.path.dirname(os.path.abspath(__file__))
OBJ = os.path.join(HERE, "xdp_limit.o")
CSV = os.path.join(HERE, "results_dyn29.csv")
TS_CSV = os.path.join(HERE, "ts_dyn29.csv")
REF_FILE = os.path.join(HERE, "ref_victim29.txt")
HEADER = ("rep,slot,mode,param,victim_mean_gbps,victim_p5_gbps,slo_ok_fraction,"
          "flooderA_iperf_gbps,flooderA_ebpf_gbps,flooderB_iperf_gbps,flooderB_ebpf_gbps,"
          "jain_fairness,cpu_mean,cpu_max,ref_gbps")
TS_HEADER = "rep,slot,mode,param,t,victim_gbps_1s,capA_mbit,capB_mbit,rateA_gbps,rateB_gbps,v_att,budget_mbit"

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

def waterfill(budget, demand):
    # max-min fair split of budget over demand {g: Mbit/s}; any budget left once every demand is
    # met is shared equally, so the allocations always sum to the whole budget
    alloc, left, rest = {}, budget, sorted(demand, key=demand.get)
    while rest and demand[rest[0]] <= left / len(rest):
        g = rest.pop(0); alloc[g] = demand[g]; left -= demand[g]
    for g in rest:
        alloc[g] = left / len(rest)
    if not rest and alloc:
        for g in alloc: alloc[g] += left / len(alloc)
    return alloc

def parse_dfs4(param):
    # "SLACK_WARM_MINSHARE", e.g. "3_0.5_0.4": slack 3 %, warm start 0.5 x ffb, min share 0.4 x budget
    parts = param.split("_")
    try:
        if len(parts) != 3: raise ValueError
        slack, warm, minshare = (float(x) for x in parts)
    except ValueError:
        raise SystemExit(f"dfs4 param {param!r} is malformed: expected SLACK_WARM_MINSHARE, e.g. 3_0.5_0.4")
    if not (0 <= slack < 100 and 0 < warm <= 1 and 0 <= minshare <= 1):
        raise SystemExit(f"dfs4 param {param!r} out of range: need 0 <= SLACK < 100, 0 < WARM <= 1, "
                         f"0 <= MINSHARE <= 1")
    return slack, warm, minshare

def parse_rot(param):
    # "TOTAL_T", e.g. "16_2": TOTAL = total flooder budget 16 Gbit/s, T = rotation period 2 s
    parts = param.split("_")
    try:
        if len(parts) != 2: raise ValueError
        total, rot_t = (float(x) for x in parts)
    except ValueError:
        raise SystemExit(f"rot param {param!r} is malformed: expected TOTAL_T, e.g. 16_2")
    if not (total > 0 and rot_t > 0):
        raise SystemExit(f"rot param {param!r} out of range: need TOTAL > 0 and T > 0")
    return total, rot_t

def parse_pidrot(param):
    # "TARGET_T", e.g. "86_3": TARGET = host CPU target %, T = rotation period s
    parts = param.split("_")
    try:
        if len(parts) != 2: raise ValueError
        target, rot_t = (float(x) for x in parts)
    except ValueError:
        raise SystemExit(f"pidrot param {param!r} is malformed: expected TARGET_T, e.g. 86_3")
    if not (0 < target <= 100 and rot_t > 0):
        raise SystemExit(f"pidrot param {param!r} out of range: need 0 < TARGET <= 100 and T > 0")
    return target, rot_t

def parse_pidrotf(param):
    # "TARGET_T", e.g. "87_3": same format as pidrot (host CPU target %, rotation period s)
    parts = param.split("_")
    try:
        if len(parts) != 2: raise ValueError
        target, rot_t = (float(x) for x in parts)
    except ValueError:
        raise SystemExit(f"pidrotf param {param!r} is malformed: expected TARGET_T, e.g. 87_3")
    if not (0 < target <= 100 and rot_t > 0):
        raise SystemExit(f"pidrotf param {param!r} out of range: need 0 < TARGET <= 100 and T > 0")
    return target, rot_t

def parse_vfrot(param):
    # "BETA_ADD_T", e.g. "0.8_1_3": cut factor 0.8, additive increase 1 Gbit/s per s, rotation period 3 s,
    # threshold VFROT_THRESH; or "BETA_ADD_T_THRESH", e.g. "0.8_1_3_0.96": same with threshold 0.96
    parts = param.split("_")
    try:
        if len(parts) not in (3, 4): raise ValueError
        beta, add, rot_t = (float(x) for x in parts[:3])
        thresh = float(parts[3]) if len(parts) == 4 else VFROT_THRESH
    except ValueError:
        raise SystemExit(f"vfrot param {param!r} is malformed: expected BETA_ADD_T or BETA_ADD_T_THRESH, "
                         f"e.g. 0.8_1_3 or 0.8_1_3_0.96")
    if not (0 < beta < 1 and add > 0 and rot_t > 0 and 0.85 <= thresh <= 1.0):
        raise SystemExit(f"vfrot param {param!r} out of range: need 0 < BETA < 1, ADD > 0, T > 0 "
                         f"and 0.85 <= THRESH <= 1.0")
    return beta, add, rot_t, thresh

def parse_dfs5(param):
    # "SLACK_T", e.g. "3_5": slack 3 %, rotation period 5 s
    parts = param.split("_")
    try:
        if len(parts) != 2: raise ValueError
        slack, rot_t = (float(x) for x in parts)
    except ValueError:
        raise SystemExit(f"dfs5 param {param!r} is malformed: expected SLACK_T, e.g. 3_5")
    if not (0 <= slack < 100 and rot_t > 0):
        raise SystemExit(f"dfs5 param {param!r} out of range: need 0 <= SLACK < 100 and T > 0")
    return slack, rot_t

def parse_lqr(param):
    # "TARGET" or "TARGET_RWT", e.g. "86" or "86_4": host CPU reference %, multiplier on the input weight R
    parts = param.split("_")
    try:
        if len(parts) not in (1, 2): raise ValueError
        target = float(parts[0])
        rwt = float(parts[1]) if len(parts) == 2 else 1.0
    except ValueError:
        raise SystemExit(f"lqr param {param!r} is malformed: expected TARGET or TARGET_RWT, e.g. 86 or 86_4")
    if not (70 <= target <= 95 and 0.05 <= rwt <= 20):
        raise SystemExit(f"lqr param {param!r} out of range: need 70 <= TARGET <= 95 and 0.05 <= RWT <= 20")
    return target, rwt

def lqr_plant(gain_scale=1.0):
    # incremental model on x = [e, d] (e = y - target, d = e(k) - e(k-1)), input du (Gbit/s):
    # e(k+1) = e(k) + a*d(k) + b*du(k), d(k+1) = a*d(k) + b*du(k); gain_scale scales the plant gain
    a = math.exp(-DT / LQR_TAU)
    b = gain_scale * K_CPU * (1.0 - a)
    return np.array([[1.0, a], [0.0, a]]), np.array([[b], [b]]), a, b

def lqr_gain(rwt, tol=1e-12, max_iter=100000):
    # discrete LQR gain K (1x2) by plain Riccati iteration on the nominal plant
    A, B, _, _ = lqr_plant()
    Q, R = np.diag(LQR_Q), np.array([[LQR_R * rwt]])
    P = Q.copy()
    for _ in range(max_iter):
        Pn = Q + A.T @ P @ A - A.T @ P @ B @ np.linalg.solve(R + B.T @ P @ B, B.T @ P @ A)
        done = np.max(np.abs(Pn - P)) <= tol * max(1.0, np.max(np.abs(Pn)))
        P = Pn
        if done:
            return np.linalg.solve(R + B.T @ P @ B, B.T @ P @ A)
    raise SystemExit(f"lqr: Riccati iteration did not converge (RWT {rwt})")

def lqr_report(param, rwt, K):
    # a, b, K, closed-loop eigenvalues of A - BK, and stability with the plant gain scaled by 0.33 ... 3
    A, B, a, b = lqr_plant()
    eig = np.linalg.eigvals(A - B @ K)
    lines = [f"LQR {param}: a={a:.6f} b={b:.6f} (%/Gbit per tick) Q=diag{LQR_Q} R={LQR_R * rwt:g} "
             f"K=[{K[0, 0]:.6f}, {K[0, 1]:.6f}] eig(A-BK)=[{', '.join(f'{z:.4f}' for z in eig)}] "
             f"|eig|max={max(abs(eig)):.4f}"]
    for s in (0.33, 0.5, 2.0, 3.0):
        As, Bs, _, _ = lqr_plant(s)
        es = np.linalg.eigvals(As - Bs @ K)
        lines.append(f"LQR {param}: plant gain x{s:g}: eig=[{', '.join(f'{z:.4f}' for z in es)}] "
                     f"|eig|max={max(abs(es)):.4f} stable {'yes' if max(abs(es)) < 1 else 'no'}")
    return lines

def rot_caps(act, on_cap, elapsed, rot_t, caps):
    # rot/dfs5: one active flooder gets on_cap, the other CAP_MIN; both active: the "on" flooder
    # rotates every rot_t s from the flooder phase start; no flooder active: keep the last caps
    if len(act) == 1:
        return [on_cap if g == act[0] else CAP_MIN for g in ("A", "B")]
    if len(act) == 2:
        on = int(elapsed / rot_t) % 2   # 0 = A, 1 = B
        return [on_cap if i == on else CAP_MIN for i in (0, 1)]
    return caps

def pidrotf_caps(act, on_cap, elapsed, rot_t, caps, delivered, state):
    # pidrotf both-active rule: the high cap (on_cap) goes to the flooder with the LOWER cumulative
    # delivered traffic, re-decided only at rotation boundaries (slot = int(elapsed / rot_t), so the
    # holder changes at most once every rot_t s); ties go to the flooder that did not hold it last.
    # One active flooder gets on_cap (and becomes the "last holder"); with none active, hold the caps.
    if len(act) == 1:
        state["holder"] = act[0]
        return [on_cap if g == act[0] else CAP_MIN for g in ("A", "B")]
    if len(act) == 2:
        slot = int(elapsed / rot_t)
        if state["holder"] is None or slot != state["slot"]:
            da, db = delivered["A"], delivered["B"]
            if da < db:
                holder = "A"
            elif db < da:
                holder = "B"
            else:
                holder = "A" if state["holder"] != "A" else "B"   # tie -> not the last holder
            state["holder"], state["slot"] = holder, slot
        return [on_cap if g == state["holder"] else CAP_MIN for g in ("A", "B")]
    return caps

def minshare_fill(budget, demand, minshare):
    # dfs4: with >= 2 active flooders each gets at least minshare * budget (scaled down so the floors sum
    # to at most the budget); the remaining budget is water-filled over the demand above that floor
    if len(demand) < 2:
        return waterfill(budget, demand)
    floor = min(minshare * budget, budget / len(demand))
    extra = waterfill(budget - floor * len(demand), {g: max(0.0, d - floor) for g, d in demand.items()})
    return {g: floor + extra[g] for g in demand}

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

def iperf_cmd(guest, secs, par=1, delay=0, rate_gbit=0):
    pre = f"sleep {delay}; " if delay else ""
    p = f" -P {par}" if par > 1 else ""
    # -b is the per-stream target bitrate; iperf3 applies it to each -P stream, so the total
    # offered rate is split equally over the streams (rate_gbit / par per stream)
    b = f" -b {rate_gbit / par:g}G" if rate_gbit else ""
    return f"{pre}iperf3 -c {HOST} -p {GUESTS[guest][2]} -t {secs}{p}{b} -J"

def burst_schedule(rep, idx):
    # burst: seeded on/off sequence of (start, on_duration) pairs inside the T s window; the schedule
    # depends only on (rep, flooder index), so it is identical for every config within a repeat (paired)
    # and A (idx 0) and B (idx 1) are drawn independently
    rng = random.Random(1000 * rep + idx)
    sched, t = [], rng.uniform(*BURST_START)
    while t < T:
        on = min(rng.uniform(*BURST_ON), T - t)   # clip the last burst so it stays inside the window
        if on <= 0.0:
            break
        sched.append((t, on))
        t += on + rng.uniform(*BURST_OFF)
    return sched

def burst_cmd(guest, sched, par):
    # burst: one iperf3 -t <on> client per on-burst, chained in a single remote shell so each starts at
    # its scheduled time (a leading sleep covers the gap since the previous burst ended); same -P streams
    p = f" -P {par}" if par > 1 else ""
    parts, prev_end = [], 0.0
    for start, on in sched:
        gap = start - prev_end
        pre = f"sleep {gap:g}; " if gap > 0 else ""
        parts.append(f"{pre}iperf3 -c {HOST} -p {GUESTS[guest][2]} -t {on:g}{p} -J")
        prev_end = start + on
    return "; ".join(parts)

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
    # victim alone, flooder caps unlimited, eBPF bytes on the victim vnet over the 10 s run;
    # also measure the mean host CPU during the victim-alone runs -> base_cpu for this repeat
    for g in ("A", "B"):
        set_cap(IF[g], 0)   # undo the caps left by the previous repeat's last run
    ebpf, ip, cpus = [], [], []
    for i in range(REF_RUNS):
        out = tempfile.TemporaryFile("w+")
        c0 = counters(IF["V"])[2]
        pt0, pi0 = cpu()
        p = ssh("V", iperf_cmd("V", REF_T, rate_gbit=VICTIM_RATE_GBIT), out); procs.append(p); p.wait()
        pt1, pi1 = cpu()
        c1 = counters(IF["V"])[2]
        busy = 100.0 * (1 - (pi1 - pi0) / max(1, (pt1 - pt0)))
        cpus.append(busy)
        ebpf.append((c1 - c0) * 8 / 1e9 / REF_T); ip.append(iperf_sum(out)); out.close()
        print(f"rep {rep} ref run {i + 1}: victim alone {ebpf[-1]:.2f} Gbit/s (eBPF), {ip[-1]:.2f} Gbit/s (iperf3), "
              f"host CPU {busy:.1f}%", flush=True)
        time.sleep(PAUSE)
    ref = st.median(ebpf)
    base_cpu = st.mean(cpus)
    if math.isnan(ref) or ref <= 0:
        raise SystemExit(f"reference measurement failed (rep {rep})")
    new = not os.path.exists(REF_FILE) or os.path.getsize(REF_FILE) == 0
    with open(REF_FILE, "a") as f:
        if new:
            f.write("# rep, time, ref_gbps (median eBPF), base_cpu (mean host %), eBPF runs, iperf3 runs; "
                    f"victim alone, {REF_RUNS} x {REF_T} s at VICTIM_RATE_GBIT={VICTIM_RATE_GBIT} Gbit/s "
                    f"at the start of each repeat\n")
        f.write(f"{rep}, {time.strftime('%Y-%m-%d %H:%M:%S')}, {ref:.3f}, {base_cpu:.1f}, "
                f"{' '.join(f'{x:.3f}' for x in ebpf)}, {' '.join(f'{x:.3f}' for x in ip)}\n")
        f.flush(); os.fsync(f.fileno())
    return ref, base_cpu

# ---------- one run ----------

def run(mode, param, ref, base_cpu, procs, scheds=None):
    ref_mbit = ref * 1000.0
    floor = (float(param) if mode == "slo" else DFS_FLOOR) / 100.0 * ref_mbit if mode in ("slo", "dfs") else None
    shared = INIT_CAP_MBIT
    if mode == "static":
        caps = [float(param) * 1000.0] * 2
    elif mode == "stasymA":
        # param = total flooder budget (Gbit/s); A gets all but CAP_MIN, B gets CAP_MIN
        caps = [float(param) * 1000.0 - CAP_MIN, CAP_MIN]
    elif mode == "stasymB":
        # mirror of stasymA: B gets all but CAP_MIN, A gets CAP_MIN
        caps = [CAP_MIN, float(param) * 1000.0 - CAP_MIN]
    elif mode == "rot":
        # rot: open-loop; A is "on" at elapsed 0 (int(0/T) % 2 == 0), so start with A on
        caps = [ROT[param][0] * 1000.0 - CAP_MIN, CAP_MIN]
    elif mode in ("pidrot", "pidrotf", "vfrot"):
        # pidrot/pidrotf/vfrot: budget B starts at 10000 Mbit/s, allocated by rotation; A on at elapsed 0
        caps = [10000.0 - CAP_MIN, CAP_MIN]
    elif mode in ("pid", "lqr", "ff", "dfs", "dfs2", "dfs2np", "dfs3", "dfs3np", "dfs4", "dfs5"):
        caps = [shared / 2] * 2
    elif mode == "slo":
        caps = [INIT_CAP_MBIT] * 2
    else:
        caps = [CAP_MAX] * 2
    target = float(param) if mode in ("pid", "ff", "dfs") else None
    holt = {}                    # dfs: flooder -> [level, trend] of the offered rate, Mbit/s
    budget = None                # dfs: guarded budget, Mbit/s
    v_s = None                   # dfs2: EMA-smoothed 1 s victim rate, Mbit/s
    v_att = DFS2_VATT_INIT * ref_mbit   # dfs2: attainable victim rate estimate, Mbit/s
    delivered = {"A": 0.0, "B": 0.0}        # pidrotf: passed (delivered) bytes per flooder, current overlap window
    pf_both = False                         # pidrotf: were both flooders active on the previous tick
    pf_state = {"holder": None, "slot": None}   # pidrotf: current high-cap holder and its rotation slot
    vf_block = float("-inf")                # vfrot: budget increases blocked while tnow < vf_block
    vf_cutblock = float("-inf")             # vfrot: budget cuts blocked while tnow < vf_cutblock
    lq_e1 = None                            # lqr: previous tick's CPU error e (None -> d = 0 at the first tick)
    # caps first, then the eBPF baseline, then the flooders: no flooder byte can precede the baseline
    set_cap(IF["A"], caps[0]); set_cap(IF["B"], caps[1])
    hist = {g: [] for g in IF}
    offered = {g: [] for g in IF}   # (t, cumulative passed + dropped bytes), same counter read as hist
    def sample():
        for g in IF:
            c = counters(IF[g]); tn = time.monotonic()
            hist[g].append((tn, c[2])); offered[g].append((tn, c[2] + c[3]))
    sample()
    # temp files instead of pipes so large -J output can never block ssh/iperf3 mid-run
    fa, fb, vo = (tempfile.TemporaryFile("w+") for _ in range(3))
    alone = mode == "alone"   # victim only, no flooders; same timeline as the other modes
    t0 = time.monotonic()
    if alone:
        pa = pb = None
    elif SCENARIO == "burst":
        pa = ssh("A", burst_cmd("A", scheds["A"], A_PAR), fa)
        pb = ssh("B", burst_cmd("B", scheds["B"], B_PAR), fb)
    else:
        pa = ssh("A", iperf_cmd("A", A_DUR, A_PAR, A_START), fa)
        pb = ssh("B", iperf_cmd("B", B_DUR, B_PAR, B_START), fb)
    time.sleep(max(0.0, t0 + OFFSET - time.monotonic()))
    pv = ssh("V", iperf_cmd("V", T, rate_gbit=VICTIM_RATE_GBIT), vo)
    procs += [p for p in (pa, pb, pv) if p is not None]
    pt, pi = cpu(); cpus = []; e1 = 0.0; base = None; vfirst = None
    below = 0; last_cut = None   # slo: consecutive ticks below the floor, tick index of the last cut
    hold_ticks = round(HOLDOFF / DT)
    ts = []                      # one row per tick for ts_dyn17.csv
    k = 1
    while pv.poll() is None and time.monotonic() < t0 + OFFSET + T + 30:
        time.sleep(max(0.0, t0 + OFFSET + k * DT - time.monotonic())); k += 1
        prev = {g: hist[g][-1] for g in IF}
        prev_off = {g: offered[g][-1] for g in IF}
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
        elif mode == "lqr":
            # same host CPU measure (busy, this tick), tick DT, start budget INIT_CAP_MBIT, equal split and
            # clamp [CAP_MIN, CAP_MAX] as pid; only the control law differs: du = -K x, x = [e, d],
            # e = busy - target, d = e(k) - e(k-1)
            # (d = 0 at the first tick); du in Gbit/s; the clamped budget is kept (anti-windup, no extra state)
            target, _, K = LQR[param]
            e = busy - target
            d = 0.0 if lq_e1 is None else e - lq_e1; lq_e1 = e
            du = -float((K @ np.array([e, d]))[0])
            shared = max(CAP_MIN, min(CAP_MAX, shared + 1000.0 * du))
            budget = shared
            caps = [shared / 2] * 2
        elif mode == "ff":
            # feed-forward budget from the measured per-repeat victim-alone CPU (base_cpu): the headroom
            # (target - base_cpu) in CPU %, converted to a shared flooder rate via K_CPU
            shared = max(CAP_MIN, min(CAP_MAX, (target - base_cpu) / K_CPU * 1000.0))
            caps = [shared / 2] * 2
        elif mode == "dfs":
            # (1) budget: the ff feedforward budget, same base estimate and K_CPU
            b = max(0.0, busy - K_CPU * (tick["A"] + tick["B"]) / 1000.0)
            base = b if base is None else 0.5 * base + 0.5 * b
            ffb = max(CAP_MIN, min(CAP_MAX, (target - base) / K_CPU * 1000.0))
            # (4) victim guard on the 1 s victim rate, only once that window lies inside the victim's run
            if budget is None:
                budget = ffb
            elif vfirst is not None and tnow - vfirst >= SMOOTH and rate_mbit(hist["V"], tnow - SMOOTH, tnow) < floor:
                budget *= DFS_CUT
            else:
                budget += DFS_RECOVER * ffb
            budget = max(CAP_MIN, min(ffb, budget))
            # (2) demand: offered (passed + dropped) rate this tick, Holt one-tick-ahead forecast
            fc = {}
            for g in ("A", "B"):
                x = (offered[g][-1][1] - prev_off[g][1]) * 8 / 1e6 / max(1e-3, offered[g][-1][0] - prev_off[g][0])
                if g not in holt:
                    holt[g] = [x, 0.0]
                else:
                    lv, tr = holt[g]
                    nlv = HOLT_ALPHA * x + (1 - HOLT_ALPHA) * (lv + tr)
                    holt[g] = [nlv, HOLT_BETA * (nlv - lv) + (1 - HOLT_BETA) * tr]
                fc[g] = max(0.0, holt[g][0] + holt[g][1])
            # (3) inactive flooders get CAP_MIN, the rest of the budget is water-filled over the active ones
            act = {g: d for g, d in fc.items() if d >= DFS_ACTIVE_MBIT}
            alloc = waterfill(budget, act)
            caps = [max(CAP_MIN, min(CAP_MAX, alloc.get(g, CAP_MIN))) for g in ("A", "B")]
        elif mode in ("dfs2", "dfs2np"):
            # dfs2np: identical to dfs2 except the forecast is the current offered rate (no Holt trend)
            use_holt = mode == "dfs2"
            # (1) budget base: the ff feed-forward budget from the measured per-repeat base_cpu (same
            # headroom formula and K_CPU as the ff config), not an online CPU estimate
            ffb = max(CAP_MIN, min(CAP_MAX, (DFS2_TARGET - base_cpu) / K_CPU * 1000.0))
            # (2) demand: offered (passed + dropped) rate this tick, Holt one-tick-ahead forecast
            #     (dfs2np: the measured offered rate itself, no trend term)
            fc = {}
            for g in ("A", "B"):
                x = (offered[g][-1][1] - prev_off[g][1]) * 8 / 1e6 / max(1e-3, offered[g][-1][0] - prev_off[g][0])
                if not use_holt:
                    fc[g] = max(0.0, x)
                    continue
                if g not in holt:
                    holt[g] = [x, 0.0]
                else:
                    lv, tr = holt[g]
                    nlv = HOLT_ALPHA * x + (1 - HOLT_ALPHA) * (lv + tr)
                    holt[g] = [nlv, HOLT_BETA * (nlv - lv) + (1 - HOLT_BETA) * tr]
                fc[g] = max(0.0, holt[g][0] + holt[g][1])
            # modified victim guard (the ONLY change from dfs)
            # (a) EMA-smooth the 1 s eBPF victim rate (time constant DFS2_VS_TAU) -> v_s, every tick
            v1s = (rate_mbit(hist["V"], tnow - SMOOTH, tnow)
                   if vfirst is not None and tnow - vfirst >= SMOOTH else 0.0)
            av = 1.0 - math.exp(-dt / DFS2_VS_TAU)
            v_s = v1s if v_s is None else av * v1s + (1.0 - av) * v_s
            if budget is None:
                budget = ffb
            if tnow - t0 < DFS2_WARMUP:
                # (b) guard disabled for the first DFS2_WARMUP s: budget stays at the ff budget
                budget = ffb
            else:
                # (c) update v_att only when a flooder is active (forecast) and both caps near CAP_MIN;
                #     caps here are still those in effect while v_s was measured (set below this tick)
                if (any(d >= DFS_ACTIVE_MBIT for d in fc.values())
                        and caps[0] <= 1.5 * CAP_MIN and caps[1] <= 1.5 * CAP_MIN):
                    aa = 1.0 - math.exp(-dt / DFS2_VATT_TAU)
                    v_att = aa * v_s + (1.0 - aa) * v_att
                # (d) adjust the budget against the slack-relaxed attainable rate
                if v_s < v_att * (1.0 - float(param) / 100.0):
                    budget = max(2 * CAP_MIN, budget * DFS_CUT)
                else:
                    budget = min(ffb, budget + DFS_RECOVER * ffb)
            # (3) inactive flooders get CAP_MIN, the rest of the budget is water-filled over the active ones
            act = {g: d for g, d in fc.items() if d >= DFS_ACTIVE_MBIT}
            alloc = waterfill(budget, act)
            caps = [max(CAP_MIN, min(CAP_MAX, alloc.get(g, CAP_MIN))) for g in ("A", "B")]
        elif mode in ("dfs3", "dfs3np", "dfs4", "dfs5"):
            # identical to dfs2 EXCEPT the victim guard is a smooth controller (below) instead of the
            # dfs2 multiplicative x0.8 / +5 % guard; param is the slack in percent
            # dfs3np: identical to dfs3 except the forecast is the current offered rate (no Holt trend)
            # dfs4: identical to dfs3np except the warm-start budget and the per-flooder minimum share
            # dfs5: identical to dfs4 (warm DFS5_WARM) except rotation (as rot) replaces the allocation
            use_holt = mode == "dfs3"
            if mode == "dfs4":
                slack, warm, minshare = DFS4[param]
            elif mode == "dfs5":
                (slack, rot_t), warm, minshare = DFS5[param], DFS5_WARM, 0.0
            else:
                slack, warm, minshare = float(param), 1.0, 0.0
            # (1) budget base: the ff feed-forward budget from the measured per-repeat base_cpu (same
            # headroom formula and K_CPU as the ff config), not an online CPU estimate
            ffb = max(CAP_MIN, min(CAP_MAX, (DFS2_TARGET - base_cpu) / K_CPU * 1000.0))
            # (2) demand: offered (passed + dropped) rate this tick, Holt one-tick-ahead forecast
            #     (dfs3np: the measured offered rate itself, no trend term)
            fc = {}
            for g in ("A", "B"):
                x = (offered[g][-1][1] - prev_off[g][1]) * 8 / 1e6 / max(1e-3, offered[g][-1][0] - prev_off[g][0])
                if not use_holt:
                    fc[g] = max(0.0, x)
                    continue
                if g not in holt:
                    holt[g] = [x, 0.0]
                else:
                    lv, tr = holt[g]
                    nlv = HOLT_ALPHA * x + (1 - HOLT_ALPHA) * (lv + tr)
                    holt[g] = [nlv, HOLT_BETA * (nlv - lv) + (1 - HOLT_BETA) * tr]
                fc[g] = max(0.0, holt[g][0] + holt[g][1])
            # smooth victim guard (the ONLY change from dfs2)
            # (a) EMA-smooth the 1 s eBPF victim rate (time constant DFS3_VS_TAU) -> v_s, every tick
            v1s = (rate_mbit(hist["V"], tnow - SMOOTH, tnow)
                   if vfirst is not None and tnow - vfirst >= SMOOTH else 0.0)
            av = 1.0 - math.exp(-dt / DFS3_VS_TAU)
            v_s = v1s if v_s is None else av * v1s + (1.0 - av) * v_s
            # dfs4 warm start: initial budget warm * ffb, clamped to [2 * CAP_MIN, ffb] (dfs3: ffb)
            b0 = ffb if mode not in ("dfs4", "dfs5") else max(2 * CAP_MIN, min(ffb, warm * ffb))
            if budget is None:
                budget = b0
            if tnow - t0 < DFS2_WARMUP:
                # (b) guard disabled for the first DFS2_WARMUP s: budget stays at the initial budget
                budget = b0
            else:
                # (c) update v_att only when a flooder is active and both caps near CAP_MIN (same
                #     conditions as dfs2); caps here are still those in effect while v_s was measured
                if (any(d >= DFS_ACTIVE_MBIT for d in fc.values())
                        and caps[0] <= 1.5 * CAP_MIN and caps[1] <= 1.5 * CAP_MIN):
                    aa = 1.0 - math.exp(-dt / DFS2_VATT_TAU)
                    v_att = aa * v_s + (1.0 - aa) * v_att
                # (d) smooth controller: normalised error against the slack-relaxed attainable rate;
                #     below target -> continuous decrease proportional to the shortfall, else slow rise
                e = (v_s - v_att * (1.0 - slack / 100.0)) / v_att
                if e < 0:
                    budget += dt * ffb * DFS3_KI_DOWN * e
                else:
                    budget += dt * ffb * DFS3_UP_RATE
                budget = max(2 * CAP_MIN, min(ffb, budget))
            # (3) inactive flooders get CAP_MIN, the rest of the budget is water-filled over the active ones
            #     (dfs4: after a minimum share per active flooder when at least two are active)
            #     (dfs5: rotation as rot instead, the "on" flooder gets budget - CAP_MIN)
            act = {g: d for g, d in fc.items() if d >= DFS_ACTIVE_MBIT}
            if mode == "dfs5":
                caps = rot_caps([g for g in ("A", "B") if g in act], budget - CAP_MIN, tnow - t0, rot_t, caps)
            else:
                alloc = minshare_fill(budget, act, minshare) if mode == "dfs4" else waterfill(budget, act)
                caps = [max(CAP_MIN, min(CAP_MAX, alloc.get(g, CAP_MIN))) for g in ("A", "B")]
        elif mode == "rot":
            # open-loop time-rotation, no victim feedback; total budget and period parsed in main()
            total, rot_t = ROT[param]
            budget = total * 1000.0            # logged in the time series; rot writes no v_att
            on_cap = budget - CAP_MIN
            # active-flooder detection as in the dfs modes: offered (passed + dropped) rate this tick
            act = [g for g in ("A", "B")
                   if (offered[g][-1][1] - prev_off[g][1]) * 8 / 1e6
                      / max(1e-3, offered[g][-1][0] - prev_off[g][0]) >= DFS_ACTIVE_MBIT]
            # one active: it gets on_cap; both: rotation every rot_t s; none: keep the last caps
            caps = rot_caps(act, on_cap, tnow - t0, rot_t, caps)
        elif mode == "pidrot":
            # same PI controller on host CPU as the pid mode (same KP/KI, control tick DT, cpu measure),
            # but its output is a TOTAL flooder budget B (Mbit/s) clamped to [2*CAP_MIN, 16000], not equal
            # caps; B is then allocated by the rot rotation logic (budget logged in the time series)
            target, rot_t = PIDROT[param]
            if budget is None:
                budget = 10000.0            # start B at 10000 Mbit/s (first tick applies the PI update)
            e = target - busy
            budget += KP * (e - e1) + KI * dt * e
            budget = max(2 * CAP_MIN, min(16000.0, budget)); e1 = e
            on_cap = budget - CAP_MIN
            # active-flooder detection as in rot/dfs: offered (passed + dropped) rate this tick
            act = [g for g in ("A", "B")
                   if (offered[g][-1][1] - prev_off[g][1]) * 8 / 1e6
                      / max(1e-3, offered[g][-1][0] - prev_off[g][0]) >= DFS_ACTIVE_MBIT]
            # one active: it gets B - CAP_MIN; both: int(elapsed/T)%2 gets B - CAP_MIN, other CAP_MIN
            caps = rot_caps(act, on_cap, tnow - t0, rot_t, caps)
        elif mode in ("pidrotf", "vfrot"):
            # identical to pidrot (same PI controller on host CPU: same KP/KI, control tick DT, cpu
            # measure; same B start 10000 and clamp [2*CAP_MIN, 16000]) EXCEPT how the high cap is
            # assigned when BOTH flooders are active: instead of int(elapsed/T)%2, a cumulative
            # per-flooder delivered-byte counter is kept per overlap window (the per-tick eBPF passed
            # bytes) and, at each rotation boundary (holder changes at most once per rot_t s), the
            # high cap goes to the flooder with the LOWER cumulative delivered traffic (ties -> the
            # one that did not hold last).
            # vfrot: identical to pidrotf EXCEPT B is not set by the host-CPU PI controller (CPU is only
            # logged): v = EMA (tau VFROT_VS_TAU) of the 1 s eBPF victim rate; v < thresh x ref is a
            # violation -> B x beta and no increase for VFROT_HOLD s; else, unless blocked, B += add per s.
            # B holds until the 1 s window lies entirely inside the victim's run (as slo); v starts there.
            if mode == "pidrotf":
                target, rot_t = PIDROTF[param]
            else:
                beta, add, rot_t, thresh = VFROT[param]
            if budget is None:
                budget = 10000.0            # start B at 10000 Mbit/s (first tick applies the update)
            if mode == "pidrotf":
                e = target - busy
                budget += KP * (e - e1) + KI * dt * e; e1 = e
            elif vfirst is not None and tnow - vfirst >= SMOOTH:
                v1s = rate_mbit(hist["V"], tnow - SMOOTH, tnow)
                av = 1.0 - math.exp(-dt / VFROT_VS_TAU)
                v_s = v1s if v_s is None else av * v1s + (1.0 - av) * v_s
                if v_s < thresh * ref_mbit:
                    vf_block = tnow + VFROT_HOLD
                    if tnow >= vf_cutblock:   # inside the cut-block a violation neither cuts nor increases
                        budget *= beta; vf_cutblock = tnow + VFROT_CUT_HOLD
                elif tnow >= vf_block:
                    budget += add * 1000.0 * dt
            budget = max(2 * CAP_MIN, min(16000.0, budget))
            on_cap = budget - CAP_MIN
            # cumulative delivered (passed) bytes per flooder, from the per-tick eBPF passed counter
            # (hist holds passed bytes); accumulated every tick, reset at the start of each overlap
            for g in ("A", "B"):
                delivered[g] += hist[g][-1][1] - prev[g][1]
            # active-flooder detection as in rot/pidrot: offered (passed + dropped) rate this tick
            act = [g for g in ("A", "B")
                   if (offered[g][-1][1] - prev_off[g][1]) * 8 / 1e6
                      / max(1e-3, offered[g][-1][0] - prev_off[g][0]) >= DFS_ACTIVE_MBIT]
            # first tick with both active: zero the counters so they span only this overlap window
            # (they tie at 0, so the tie rule picks the flooder that did not hold the high cap last)
            if len(act) == 2 and not pf_both:
                delivered["A"] = delivered["B"] = 0.0
            pf_both = len(act) == 2
            # one active: it gets B - CAP_MIN; both: lower cumulative delivered gets B - CAP_MIN,
            # decided only at rotation boundaries; none: keep the last caps
            caps = pidrotf_caps(act, on_cap, tnow - t0, rot_t, caps, delivered, pf_state)
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
        if mode in ("pid", "lqr", "ff", "slo", "dfs", "dfs2", "dfs2np", "dfs3", "dfs3np", "dfs4", "dfs5", "rot", "pidrot", "pidrotf", "vfrot"):
            set_cap(IF["A"], caps[0]); set_cap(IF["B"], caps[1])
        # t relative to flooder launch; caps as set at this tick; flooder rates over this tick
        ts.append((tnow - t0, rate_mbit(hist["V"], tnow - SMOOTH, tnow) / 1000.0,
                   caps[0], caps[1], tick["A"] / 1000.0, tick["B"] / 1000.0,
                   v_att if mode in ("dfs2", "dfs2np", "dfs3", "dfs3np", "dfs4", "dfs5") else float("nan"),
                   budget if mode in ("dfs2", "dfs2np", "dfs3", "dfs3np", "dfs4", "dfs5", "rot", "pidrot", "pidrotf", "vfrot", "lqr") else float("nan")))
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
    configs = [("lqr", "76_4"), ("lqr", "80_4"), ("vfrot", "0.8_1_3_0.94"), ("pidrotf", "80_3"),
               ("rot", "10_3"), ("pid", "74"), ("pid", "78"), ("pid", "82")]
    if SCENARIO not in ("steady", "burst"):
        raise SystemExit(f"SCENARIO {SCENARIO!r} unknown: expected 'steady' or 'burst'")
    for m, p in configs:
        if m == "dfs4":
            DFS4[p] = parse_dfs4(p)
        elif m == "dfs5":
            DFS5[p] = parse_dfs5(p)
        elif m == "rot":
            ROT[p] = parse_rot(p)
        elif m == "pidrot":
            PIDROT[p] = parse_pidrot(p)
        elif m == "pidrotf":
            PIDROTF[p] = parse_pidrotf(p)
        elif m == "vfrot":
            VFROT[p] = parse_vfrot(p)
        elif m == "lqr":
            target, rwt = parse_lqr(p)
            LQR[p] = (target, rwt, lqr_gain(rwt))
    ALONE = ("alone", "na")   # victim-only control, run before the first config and after every config
    res = {c: [] for c in configs + [ALONE]}
    try:
        sudo(["true"])
    except subprocess.CalledProcessError:
        raise SystemExit("sudo needs a password: run 'sudo -v' first (run11.sh does this)")
    if not os.path.exists(OBJ):
        raise SystemExit(f"{OBJ} not found")
    new = not os.path.exists(CSV) or os.path.getsize(CSV) == 0
    if not new and open(CSV).readline().strip() != HEADER:
        raise SystemExit(f"{CSV} exists with a different header; not appending to it")
    ts_new = not os.path.exists(TS_CSV) or os.path.getsize(TS_CSV) == 0
    if not ts_new and open(TS_CSV).readline().strip() != TS_HEADER:
        raise SystemExit(f"{TS_CSV} exists with a different header; not appending to it")
    ensure_servers()
    print(f"SCENARIO {SCENARIO}" + (f": flooder A t={A_START}-{A_START + A_DUR} s ({A_PAR} streams), flooder B "
          f"t={B_START}-{B_START + B_DUR} s ({B_PAR} streams), no bursts" if SCENARIO == "steady" else
          f": seeded on/off bursts per repeat and flooder (on {BURST_ON[0]:g}-{BURST_ON[1]:g} s, off "
          f"{BURST_OFF[0]:g}-{BURST_OFF[1]:g} s, first start {BURST_START[0]:g}-{BURST_START[1]:g} s; A {A_PAR} "
          f"streams, B {B_PAR} streams; same schedule for all configs in a repeat; iperf columns nan, "
          f"eBPF columns valid)"), flush=True)
    print("CONFIGS: " + ", ".join(f"{m} {p}" + (f" (thresh {VFROT[p][3]:.2f})" if m == "vfrot" else "")
                                  for m, p in configs), flush=True)
    for p, (target, rwt, K) in LQR.items():
        print(f"LQR {p}: target {target:g}% host CPU, RWT {rwt:g}, budget clamp [{CAP_MIN:g}, "
              f"{CAP_MAX:g}] Mbit/s (as pid), start {INIT_CAP_MBIT:g} Mbit/s (equal split)", flush=True)
        for line in lqr_report(p, rwt, K):
            print(line, flush=True)
    # expected duration: per slot ~ OFFSET + T + ~2.5 s ssh/iperf3 overhead + PAUSE; per repeat the reference
    # (REF_RUNS x (REF_T + ~1 s + PAUSE)) plus (2N + 1) slots; SESSION_REST_S between repeats
    n_slots = 2 * len(configs) + 1
    per_rep = REF_RUNS * (REF_T + 1 + PAUSE) + n_slots * (OFFSET + T + 2.5 + PAUSE)
    total = REPS * per_rep + (REPS - 1) * SESSION_REST_S
    print(f"EXPECTED DURATION: {n_slots} slots/repeat, ~{per_rep / 60:.1f} min/repeat, {REPS} repeats + "
          f"{REPS - 1} x {SESSION_REST_S} s rest ~ {total / 3600:.2f} h", flush=True)
    procs = []
    refs = []         # reference used in each repeat
    base_cpus = []    # measured victim-alone host CPU mean used in each repeat
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
              f"writing {CSV} and {TS_CSV}; reference re-measured at the start of every repeat, victim "
              f"offered {VICTIM_RATE_GBIT} Gbit/s, {SESSION_REST_S} s rest after every repeat except the "
              f"last (the {COOLDOWN} s cool-down applies only to a repeat not preceded by a rest)", flush=True)
        rested = False
        for rep in range(1, REPS + 1):
            if rep > 1 and not rested:
                print(f"cool-down {COOLDOWN} s before repeat {rep}", flush=True)
                time.sleep(COOLDOWN)
            rested = False
            # reference first (consumes no random numbers), then this repeat's shuffled order
            ref, base_cpu = reference(rep, procs); refs.append(ref); base_cpus.append(base_cpu)
            print(f"REFERENCE rep {rep}: victim rate {ref:.3f} Gbit/s (median of {REF_RUNS} x {REF_T} s, eBPF), "
                  f"base_cpu {base_cpu:.1f}% (mean host CPU, victim alone), "
                  f"slo_ok threshold {SLO_FRAC * ref:.3f} Gbit/s, dfs victim floor "
                  f"{DFS_FLOOR}% = {DFS_FLOOR / 100 * ref:.3f} Gbit/s, saved to {REF_FILE}", flush=True)
            # burst: draw the per-flooder on/off schedule once per repeat (same for every config within
            # the repeat so configs stay paired); log it, then reuse it for all flooder runs this repeat
            scheds = None
            if SCENARIO == "burst":
                scheds = {"A": burst_schedule(rep, 0), "B": burst_schedule(rep, 1)}
                for g, par in (("A", A_PAR), ("B", B_PAR)):
                    bursts = ", ".join(f"[{s:.2f}-{s + d:.2f}]" for s, d in scheds[g])
                    print(f"rep {rep} burst schedule flooder {g} ({par} streams, t rel. flooder launch): "
                          f"{bursts}", flush=True)
            print("rep slot mode           param v_mean  v_p5  slo_ok A_iperf A_ebpf B_iperf B_ebpf  jain cpu_mean cpu_max", flush=True)
            order = configs[:]; random.shuffle(order)
            seq = [ALONE] + [x for c in order for x in (c, ALONE)]   # alone, c1, alone, ..., cN, alone
            for slot, (m, p) in enumerate(seq):
                r, ts = run(m, p, ref, base_cpu, procs, scheds); res[(m, p)].append(r)
                csv.write(f"{rep},{slot},{m},{p},{r[0]:.3f},{r[1]:.3f},{r[2]:.3f},{r[3]:.3f},{r[4]:.3f},"
                          f"{r[5]:.3f},{r[6]:.3f},{r[7]:.4f},{r[8]:.2f},{r[9]:.2f},{ref:.3f}\n"); csv.flush()
                os.fsync(csv.fileno())
                tsf.write("".join(f"{rep},{slot},{m},{p},{t:.3f},{v:.3f},{ca:.1f},{cb:.1f},{a:.3f},{b:.3f},{va:.3f},{bu:.1f}\n"
                                  for t, v, ca, cb, a, b, va, bu in ts)); tsf.flush()
                os.fsync(tsf.fileno())
                print(f"{rep:3d} {slot:4d} {m:7s} {p:>12s} {r[0]:6.2f} {r[1]:5.2f} {r[2]:6.2f} {r[3]:7.2f} {r[4]:6.2f} "
                      f"{r[5]:7.2f} {r[6]:6.2f} {r[7]:5.3f} {r[8]:8.1f} {r[9]:7.1f}", flush=True)
                time.sleep(PAUSE)
            if rep % SESSION_REPEATS == 0 and rep < REPS:
                print(f"session rest {SESSION_REST_S} s", flush=True)
                time.sleep(SESSION_REST_S)
                rested = True   # the rest replaces the next repeat's cool-down (no double thermal wait)
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
    print("\nREFERENCES per repeat (Gbit/s): " + ", ".join(f"rep {i + 1} = {r:.3f} (base_cpu {b:.1f}%)"
                                                           for i, (r, b) in enumerate(zip(refs, base_cpus))))
    print(f"\nSUMMARY (mean +- sd over {REPS} repeats; alone pooled over all {len(configs) + 1} slots per repeat)")
    print("mode           param    n " + " ".join(f"{n:>15s}" for n in names))
    for c in configs + [ALONE]:
        cols = [ms([r[j] for r in res[c]]) for j in range(len(names))]
        print(f"{c[0]:7s} {c[1]:>12s} {len(res[c]):4d} " + " ".join(f"{a:6.2f} +- {b:5.2f}" for a, b in cols))

if __name__ == "__main__":
    main()
