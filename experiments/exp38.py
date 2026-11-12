import json, subprocess, time, struct, random, statistics as st, sys, os, math, tempfile
import numpy as np
REPS = int(sys.argv[1]) if len(sys.argv) > 1 else 6
T, DT = 40, 0.25
OFFSET = 0.5            # victims start 0.5 s after the flooders are launched (as exp5)
# exp38: scaling session, 6 tenants. Same scenario logic as exp37 steady3 with two victims and a fourth flooder;
# only the modes of the exp38 CONFIGS (static, lqr, vfrot) and alone are kept from exp37 (the code paths copied
# unchanged except where a comment says "exp38")
VICTIMS = ["V1", "V2"]                  # exp38: two victims, never limited (counter only); keyed by this list
FLOODERS = ["A", "B", "C", "D"]         # every per-flooder structure below is keyed by this list
PAR = {"A": 2, "B": 2, "C": 2, "D": 2}  # iperf3 -P streams per flooder (exp38: all 2; exp37 A had 4)
STEADY = {                              # steady scenarios: flooder -> (start s, duration s), t rel. flooder launch
    "steady4": {"A": (0, 28), "B": (6, 28), "C": (12, 28), "D": (18, 22)},  # exp38: all four active during 18-28 s
}
SCENARIO = "steady4"    # exp38: the only scenario (continuous flooders, no bursts)
NV, NF = len(VICTIMS), len(FLOODERS)
KP, KI = 200.0, 300.0
CAP_MIN, CAP_MAX = 200.0, 70000.0
INIT_CAP_MBIT = 2000.0  # lqr: initial shared budget (split equally)
XDP_BURST_NS = 5000000  # token-bucket burst of xdp_limit.o (BURST_NS in xdp_limit.c, 5 ms at the cap rate),
                        # the same object and burst for every mode; only logged here, the program owns it
K_CPU = 0.93            # % host CPU per Gbit/s (from the Step 19 measurement)
V_RATE = 9.0            # exp38: offered rate of EACH victim, Gbit/s, one stream, constant over [0, T) (as exp34)
SLO_FRAC = 0.95         # slo_ok threshold (all modes): SLO_FRAC * that victim's own per-repeat reference
SMOOTH = 1.0            # s, victim rate window (1 s) for the vfrot feedback and the logged 1 s rates
DFS_ACTIVE_MBIT = 50.0  # vfrot: a flooder whose offered rate this tick is below this is inactive (gets CAP_MIN)
VFROT = {}               # vfrot: param string -> (beta, add Gbit/s per s, rotation period s, thresh), parsed once in main()
VFROT_VS_TAU = 0.5       # s, vfrot: EMA time constant of each victim's smoothed 1 s rate v
VFROT_THRESH = 0.98      # vfrot: default violation threshold (3-part param)
VFROT_HOLD = 1.0         # s, vfrot: no budget increase for this long after a violation tick
VFROT_CUT_HOLD = 1.0     # s, vfrot: no further cut for this long after a cut (the 1 s victim window lags recovery)
VF_B0, VF_BMAX = 10000.0, 16000.0   # vfrot: budget start and upper clamp, Mbit/s (as exp37); lower clamp NF * CAP_MIN
LQR = {}                 # lqr: param string -> (host CPU target %, R weight multiplier, gain K 1x2), set in main()
LQR_TAU = 0.5            # s, lqr plant time constant: y(k+1) = a*y(k) + b*u(k), a = exp(-DT/LQR_TAU),
                         # b = K_CPU*(1-a) (steady-state gain K_CPU)
LQR_Q = (1.0, 1.0)       # lqr: state weights Q = diag(q_e, q_d)
LQR_R = 0.5              # lqr: input weight on du (Gbit/s); R = LQR_R * RWT
TICK_WARN_S = 0.30       # exp38: s, a slot whose mean control tick is longer than this gets a WARNING line in the log
REF_RUNS, REF_T = 2, 10   # both victims together, no flooders, measured at the start of EVERY repeat
PAUSE = 8
COOLDOWN = 45           # s, extra cool-down pause between repeats (thermal), on top of PAUSE
SESSION_REPEATS = 1     # repeats per session; a long rest follows each session except the last
SESSION_REST_S = 600    # s, thermal rest between sessions, on top of COOLDOWN
HOST = "10.10.0.1"
GUESTS = {"V1": ("tenant1", "10.10.0.11", 5201),  # victim 1
          "V2": ("tenant5", "10.10.0.15", 5205),  # victim 2 (exp38; port 5205 was exp37's victim extra group)
          "A": ("tenant2", "10.10.0.12", 5202),   # flooder A
          "B": ("tenant3", "10.10.0.13", 5203),   # flooder B
          "C": ("tenant4", "10.10.0.14", 5204),   # flooder C
          "D": ("tenant6", "10.10.0.16", 5206)}   # flooder D (exp38)
MAKE_SCRIPT = {"tenant5": "make_tenant5.sh", "tenant6": "make_tenant6.sh"}   # guests new in exp38
HERE = os.path.dirname(os.path.abspath(__file__))
OBJ = os.path.join(HERE, "xdp_limit.o")
CSV = os.path.join(HERE, "results_dyn38.csv")
TS_CSV = os.path.join(HERE, "ts_dyn38.csv")
REF_FILE = os.path.join(HERE, "ref_victim38.txt")
LOG = os.path.join(HERE, "exp38.log")   # written by run38.sh (stdout redirect), checked in main()
# exp38: 5 configs -> 11 slots/repeat (alone interleaved); order of definition, shuffled per repeat in main()
# static: param = per-flooder cap in Mbit/s, set once at the start of the slot, no feedback
# (4 flooders: 2500 -> 10 Gbit/s total, ~ exp37 static 3500 x 3 = 10.5; 3500 -> 14 Gbit/s total)
CONFIGS = [("static", "2500"), ("static", "3500"), ("lqr", "79_4"), ("lqr", "85_4"), ("vfrot", "0.8_1_2_0.94")]
MODES = ("static", "lqr", "vfrot")
HEADER = ("rep,slot,mode,param,"
          + "".join(f"victim{v[1:]}_mean_gbps,victim{v[1:]}_p5_gbps,slo_ok_v{v[1:]}," for v in VICTIMS)
          + "slo_ok_worst,"
          + "".join(f"flooder{g}_iperf_gbps,flooder{g}_ebpf_gbps," for g in FLOODERS)
          + "jain_fairness,cpu_mean,cpu_max,steal_mean,steal_max,tick_mean_s,tick_max_s,"
          + ",".join(f"ref_v{v[1:]}_gbps" for v in VICTIMS))
TS_HEADER = ("rep,slot,mode,param,t,tick_s,sample_s,cpu,steal,"
             + "".join(f"v{v[1:]}_gbps_1s," for v in VICTIMS) + "".join(f"v{v[1:]}_gbps_tick," for v in VICTIMS)
             + "".join(f"cap{g}_mbit," for g in FLOODERS) + "".join(f"rate{g}_gbps," for g in FLOODERS)
             + "budget_mbit,worst_ratio")

IF = {}           # each victim and each flooder -> {"dev", "prog", "cfg", "cnt"} (map ids, never names)
last_cap = {}     # cfg map id -> last bytes/s written

def sudo(args):
    return subprocess.run(["sudo", "-n"] + args, capture_output=True, text=True, check=True).stdout

# ---------- interfaces and eBPF maps, all addressed by id ----------

def vnet(dom):
    # names change after guest restarts, so always ask libvirt; '-' means the guest is not running
    try:
        out = sudo(["virsh", "domiflist", dom])
    except subprocess.CalledProcessError as e:
        hint = f"; create it with: sudo bash {MAKE_SCRIPT[dom]}" if dom in MAKE_SCRIPT else ""
        raise SystemExit(f"{dom}: 'virsh domiflist {dom}' failed ({(e.stderr or '').strip()}){hint}")
    rows = [l.split() for l in out.splitlines()[2:] if l.strip()]
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
    # aggregate cpu line of /proc/stat: (total, idle + iowait, steal) jiffies; total = sum of all fields as in
    # exp37 (so the lqr targets keep their meaning), steal = 8th field (None if the kernel does not report it)
    f = list(map(int, open("/proc/stat").readline().split()[1:]))
    return sum(f), f[3] + f[4], (f[7] if len(f) > 7 else None)

def busy_steal(c0, c1):
    # exp38: host CPU busy % (as exp37) and steal % of the same total between two cpu() readings
    dt = max(1, c1[0] - c0[0])
    busy = 100.0 * (1 - (c1[1] - c0[1]) / dt)
    steal = float("nan") if c0[2] is None or c1[2] is None else 100.0 * (c1[2] - c0[2]) / dt
    return busy, steal

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

def active_flooders(offered, prev_off):
    # active-flooder detection (vfrot): offered (passed + dropped) rate over this tick >= DFS_ACTIVE_MBIT;
    # returned in FLOODERS order
    return [g for g in FLOODERS
            if (offered[g][-1][1] - prev_off[g][1]) * 8 / 1e6
               / max(1e-3, offered[g][-1][0] - prev_off[g][0]) >= DFS_ACTIVE_MBIT]

def high_cap(budget, n_act):
    # cap of the holder: every other active flooder keeps CAP_MIN, so the holder gets
    # budget - (n_act - 1) * CAP_MIN; a single active flooder gets budget - CAP_MIN (as exp29)
    return max(CAP_MIN, budget - max(1, n_act - 1) * CAP_MIN)

def next_after(h, cands):
    # round-robin successor of h among cands, in FLOODERS order (h None or absent -> first after its slot)
    if h is None:
        return cands[0]
    i = FLOODERS.index(h)
    for g in FLOODERS[i + 1:] + FLOODERS[:i + 1]:
        if g in cands:
            return g

def hold_caps(act, budget, elapsed, rot_t, caps, state, pick):
    # one active flooder (the holder) gets high_cap, every other flooder CAP_MIN; the holder is
    # re-decided by pick(act, holder) when it is not active (immediately) or when it has held for
    # rot_t s (re-decision instants every rot_t s, so a voluntary hand-over is never faster than
    # rot_t); with no flooder active, keep the last caps
    if not act:
        return caps
    h = state["holder"]
    if h not in act or elapsed - state["since"] >= rot_t:
        state["holder"], state["since"] = pick(act, h), elapsed
    on = high_cap(budget, len(act))
    return [on if g == state["holder"] else CAP_MIN for g in FLOODERS]

def pidrotf_caps(act, budget, elapsed, rot_t, caps, delivered, state):
    # vfrot: the high cap goes to the active flooder with the LOWEST cumulative delivered
    # traffic; ties go to a flooder that is not the current holder (the next one round-robin)
    def pick(a, h):
        low = min(delivered[g] for g in a)
        tied = [g for g in a if delivered[g] == low]
        others = [g for g in tied if g != h]
        return next_after(h, others) if others else h
    return hold_caps(act, budget, elapsed, rot_t, caps, state, pick)

def pf_update(delivered, passed, act, pf_all):
    # vfrot: add this tick's passed bytes per flooder, then zero the counters on the first tick
    # on which ALL flooders are active (so they span only that overlap window); returns the new pf_all
    for g in FLOODERS:
        delivered[g] += passed[g]
    all_on = len(act) == NF
    if all_on and not pf_all:
        for g in FLOODERS:
            delivered[g] = 0.0
    return all_on

def flooder_dur(g):
    # normalisation duration of flooder g's iperf/eBPF means: its steady window
    return STEADY[SCENARIO][g][1]

def overlap():
    # window in which all flooders are active (Jain fairness), t relative to the flooder launch
    w = STEADY[SCENARIO]
    return (float(max(w[g][0] for g in FLOODERS)), float(min(w[g][0] + w[g][1] for g in FLOODERS)))

def jain(xs):
    s, q = sum(xs), sum(x * x for x in xs)
    return s * s / (len(xs) * q) if q > 0 else float("nan")

def p5(xs):
    if len(xs) < 2: return xs[0] if xs else float("nan")
    return st.quantiles(xs, n=20, method="inclusive")[0]

def nanstat(xs, f):
    xs = [x for x in xs if not math.isnan(x)]
    return f(xs) if xs else float("nan")

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

def victim_cmds(secs):
    # exp38: one iperf3 client per victim (one stream, V_RATE), all launched together
    return {v: iperf_cmd(v, secs, rate_gbit=V_RATE) for v in VICTIMS}

def flooder_cmds():
    # one delayed (sleep) iperf3 client per flooder, as exp37 steady scenarios
    return {g: iperf_cmd(g, STEADY[SCENARIO][g][1], PAR[g], STEADY[SCENARIO][g][0]) for g in FLOODERS}

# ---------- setup ----------

def check_servers():
    # the host iperf3 servers are started by hand; abort if any of them is not listening
    # exp38: ports 5201 (victim 1), 5202-5204 (flooders A-C), 5205 (victim 2), 5206 (flooder D)
    out = subprocess.run(["ss", "-ltnH"], capture_output=True, text=True).stdout
    ports = sorted(GUESTS[g][2] for g in GUESTS)
    missing = [p for p in ports
               if not any(l.split()[3].endswith(f":{p}") for l in out.splitlines() if len(l.split()) > 3)]
    if missing:
        raise SystemExit("no iperf3 server listening on host port(s) " + ", ".join(map(str, missing))
                         + "; start them first: " + "; ".join(f"iperf3 -s -D -p {p}" for p in missing))
    print("iperf3 servers listening on ports " + ", ".join(map(str, ports)), flush=True)

def check_guests():
    # exp38: every guest must answer ssh non-interactively and have iperf3 (tenant5/tenant6 are new)
    bad = []
    for g, (dom, ip, _) in GUESTS.items():
        try:
            p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", f"alpine@{ip}",
                                "command -v iperf3"], capture_output=True, text=True, timeout=20)
            ok = p.returncode == 0 and p.stdout.strip() != ""
        except subprocess.TimeoutExpired:
            ok = False
        if not ok:
            bad.append(f"{dom} ({ip})" + (f" -- create it with: sudo bash {MAKE_SCRIPT[dom]}" if dom in MAKE_SCRIPT else ""))
    if bad:
        raise SystemExit("guest(s) not reachable by ssh alpine@IP or without iperf3: " + "; ".join(bad))
    print("guests reachable with iperf3: " + ", ".join(f"{g}={GUESTS[g][0]}" for g in GUESTS), flush=True)

def reference(rep, procs):
    # exp38: both victims together (same conditions as the alone slots), flooder caps unlimited, no flooders;
    # each victim's reference = median of its own eBPF bytes on its own vnet over REF_RUNS x REF_T s;
    # base_cpu = mean host CPU during these runs (logged; no exp38 config uses it)
    for g in FLOODERS:
        set_cap(IF[g], 0)   # undo the caps left by the previous repeat's last run
    ebpf, ip = {v: [] for v in VICTIMS}, {v: [] for v in VICTIMS}
    cpus, steals = [], []
    for i in range(REF_RUNS):
        outs = {v: tempfile.TemporaryFile("w+") for v in VICTIMS}
        c0 = {v: counters(IF[v])[2] for v in VICTIMS}
        k0 = cpu()
        ps = [ssh(v, cmd, outs[v]) for v, cmd in victim_cmds(REF_T).items()]; procs += ps
        for p in ps: p.wait()
        k1 = cpu()
        c1 = {v: counters(IF[v])[2] for v in VICTIMS}
        busy, steal = busy_steal(k0, k1)
        cpus.append(busy); steals.append(steal)
        for v in VICTIMS:
            ebpf[v].append((c1[v] - c0[v]) * 8 / 1e9 / REF_T); ip[v].append(iperf_sum(outs[v])); outs[v].close()
        print(f"rep {rep} ref run {i + 1}: both victims together, " + ", ".join(
                  f"{v} {ebpf[v][-1]:.2f} Gbit/s (eBPF) {ip[v][-1]:.2f} (iperf3)" for v in VICTIMS)
              + f", host CPU {busy:.1f}%, steal {steal:.1f}%", flush=True)
        time.sleep(PAUSE)
    refs = {v: st.median(ebpf[v]) for v in VICTIMS}
    base_cpu = st.mean(cpus)
    for v in VICTIMS:
        if math.isnan(refs[v]) or refs[v] <= 0:
            raise SystemExit(f"reference measurement failed (rep {rep}, victim {v} {GUESTS[v][0]})")
    new = not os.path.exists(REF_FILE) or os.path.getsize(REF_FILE) == 0
    with open(REF_FILE, "a") as f:
        if new:
            f.write("# rep, time, victim, tenant, ref_gbps (median eBPF), mean host CPU %, mean steal %, eBPF runs, "
                    f"iperf3 runs; both victims together at {V_RATE:g} Gbit/s each, {REF_RUNS} x {REF_T} s, no "
                    f"flooders, at the start of each repeat\n")
        for v in VICTIMS:
            f.write(f"{rep}, {time.strftime('%Y-%m-%d %H:%M:%S')}, {v}, {GUESTS[v][0]}, {refs[v]:.3f}, "
                    f"{base_cpu:.1f}, {nanstat(steals, st.mean):.2f}, {' '.join(f'{x:.3f}' for x in ebpf[v])}, "
                    f"{' '.join(f'{x:.3f}' for x in ip[v])}\n")
        f.flush(); os.fsync(f.fileno())
    return refs, base_cpu

# ---------- one run ----------

def run(mode, param, refs, base_cpu, procs):
    # exp38: refs = {victim: its reference Gbit/s}; base_cpu only logged
    shared = INIT_CAP_MBIT
    if mode == "static":
        # param in Mbit/s; the same cap on every flooder, written below by set_cap before any flooder
        # starts and never again during the run (static is not in the per-tick set_cap list)
        caps = [float(param)] * NF
    elif mode == "vfrot":
        # budget B starts at VF_B0, allocated by rotation; first flooder on
        caps = [high_cap(VF_B0, 1) if g == FLOODERS[0] else CAP_MIN for g in FLOODERS]
    elif mode == "lqr":
        caps = [shared / NF] * NF
    else:
        caps = [CAP_MAX] * NF
    budget = None
    v_s = {v: None for v in VICTIMS}        # vfrot: EMA-smoothed 1 s rate per victim, Mbit/s
    delivered = {g: 0.0 for g in FLOODERS}  # vfrot: passed (delivered) bytes per flooder, current overlap window
    pf_all = False                          # vfrot: were all flooders active on the previous tick
    pf_state = {"holder": None, "since": None}  # vfrot: high-cap holder, when decided
    vf_block = float("-inf")                # vfrot: budget increases blocked while tnow < vf_block
    vf_cutblock = float("-inf")             # vfrot: budget cuts blocked while tnow < vf_cutblock
    lq_e1 = None                            # lqr: previous tick's CPU error e (None -> d = 0 at the first tick)
    # caps first, then the eBPF baseline, then the flooders: no flooder byte can precede the baseline
    for g, c in zip(FLOODERS, caps): set_cap(IF[g], c)
    if mode == "static":
        print(f"static {param}: cap set once for the whole run on " + ", ".join(
            f"{g} {IF[g]['dev']} (cfg map id {IF[g]['cfg']}) {last_cap[IF[g]['cfg']] * 8 / 1e6:g} Mbit/s"
            for g in FLOODERS), flush=True)
    hist = {g: [] for g in IF}
    offered = {g: [] for g in IF}   # (t, cumulative passed + dropped bytes), same counter read as hist
    def sample():
        # reads every interface's counters; returns how long the reads took (s)
        s0 = time.monotonic()
        for g in IF:
            c = counters(IF[g]); tn = time.monotonic()
            hist[g].append((tn, c[2])); offered[g].append((tn, c[2] + c[3]))
        return time.monotonic() - s0
    sample()
    # temp files instead of pipes so large -J output can never block ssh/iperf3 mid-run
    fo = {g: tempfile.TemporaryFile("w+") for g in FLOODERS}
    vo = {v: tempfile.TemporaryFile("w+") for v in VICTIMS}   # one -J document per victim
    alone = mode == "alone"   # victims only, no flooders; same timeline as the other modes
    t0 = time.monotonic()
    fp = {} if alone else {g: ssh(g, cmd, fo[g]) for g, cmd in flooder_cmds().items()}
    time.sleep(max(0.0, t0 + OFFSET - time.monotonic()))
    vp = {v: ssh(v, cmd, vo[v]) for v, cmd in victim_cmds(T).items()}   # both victims together
    procs += list(fp.values()) + list(vp.values())
    v0 = VICTIMS[0]   # its counter read time is the tick time (first interface sampled)
    c_prev = cpu(); cpus, steals, ticks = [], [], []
    vfirst = {v: None for v in VICTIMS}
    ts = []                      # one row per tick for ts_dyn38.csv
    k = 1
    while any(p.poll() is None for p in vp.values()) and time.monotonic() < t0 + OFFSET + T + 30:
        time.sleep(max(0.0, t0 + OFFSET + k * DT - time.monotonic())); k += 1
        prev = {g: hist[g][-1] for g in IF}
        prev_off = {g: offered[g][-1] for g in IF}
        samp = sample(); tnow = hist[v0][-1][0]; c_now = cpu()
        busy, steal = busy_steal(c_prev, c_now); c_prev = c_now
        cpus.append(busy); steals.append(steal)
        for v in VICTIMS:
            if vfirst[v] is None and hist[v][-1][1] > hist[v][0][1]:
                vfirst[v] = tnow   # first sample after this victim's bytes started flowing
        # rates over the real elapsed time between counter reads, not the nominal DT
        tick = {g: (hist[g][-1][1] - prev[g][1]) * 8 / 1e6 / max(1e-3, hist[g][-1][0] - prev[g][0]) for g in IF}
        dt = max(1e-3, tnow - prev[v0][0])   # exp38: real tick length, logged (the first tick spans the launch)
        ticks.append(dt)
        worst = float("nan")
        if mode == "lqr":
            # host CPU measure (busy, this tick), tick DT, start budget INIT_CAP_MBIT, equal split and
            # clamp [CAP_MIN, CAP_MAX]; du = -K x, x = [e, d], e = busy - target, d = e(k) - e(k-1)
            # (d = 0 at the first tick); du in Gbit/s; the clamped budget is kept (anti-windup, no extra state)
            target, _, K = LQR[param]
            e = busy - target
            d = 0.0 if lq_e1 is None else e - lq_e1; lq_e1 = e
            du = -float((K @ np.array([e, d]))[0])
            shared = max(CAP_MIN, min(CAP_MAX, shared + 1000.0 * du))
            budget = shared
            caps = [shared / NF] * NF   # exp38: NF = 4
        elif mode == "vfrot":
            # exp37 vfrot (CPU only logged): B x beta on a violation (then no further cut for VFROT_CUT_HOLD s,
            # no increase for VFROT_HOLD s), else B += add per s; B clamped [NF*CAP_MIN, VF_BMAX]; the high cap
            # rotates (every rot_t s) to the active flooder with the lowest delivered bytes since all were active.
            # exp38: one EMA (tau VFROT_VS_TAU) of the 1 s eBPF rate PER VICTIM; the feedback is the WORST victim,
            # worst = min over victims of v_s / its own reference; violation = worst < thresh. B holds until both
            # victims' 1 s windows lie entirely inside their runs; both EMAs start there.
            beta, add, rot_t, thresh = VFROT[param]
            if budget is None:
                budget = VF_B0              # start B (first tick applies the update)
            if all(vfirst[v] is not None and tnow - vfirst[v] >= SMOOTH for v in VICTIMS):
                av = 1.0 - math.exp(-dt / VFROT_VS_TAU)
                for v in VICTIMS:
                    v1s = rate_mbit(hist[v], tnow - SMOOTH, tnow)
                    v_s[v] = v1s if v_s[v] is None else av * v1s + (1.0 - av) * v_s[v]
                worst = min(v_s[v] / (refs[v] * 1000.0) for v in VICTIMS)
                if worst < thresh:
                    vf_block = tnow + VFROT_HOLD
                    if tnow >= vf_cutblock:   # inside the cut-block a violation neither cuts nor increases
                        budget *= beta; vf_cutblock = tnow + VFROT_CUT_HOLD
                elif tnow >= vf_block:
                    budget += add * 1000.0 * dt
            budget = max(NF * CAP_MIN, min(VF_BMAX, budget))
            act = active_flooders(offered, prev_off)
            pf_all = pf_update(delivered, {g: hist[g][-1][1] - prev[g][1] for g in FLOODERS}, act, pf_all)
            caps = pidrotf_caps(act, budget, tnow - t0, rot_t, caps, delivered, pf_state)
        if mode in ("lqr", "vfrot"):
            for g, c in zip(FLOODERS, caps): set_cap(IF[g], c)
        # t relative to flooder launch; caps as set at this tick; rates over this tick
        ts.append((tnow - t0, dt, samp, busy, steal,
                   *(rate_mbit(hist[v], tnow - SMOOTH, tnow) / 1000.0 for v in VICTIMS),
                   *(tick[v] / 1000.0 for v in VICTIMS),
                   *caps, *(tick[g] / 1000.0 for g in FLOODERS),
                   budget if mode in ("lqr", "vfrot") else float("nan"), worst))
    for p in vp.values(): p.wait()
    for p in fp.values(): p.wait()
    # end of the eBPF window: after all iperf3 sessions have stopped, same bytes iperf3 counts
    sample()
    vres = []         # per victim: iperf3 mean over T, p5 of the 1 s windows, slo_ok against its own reference
    for v in VICTIMS:
        wins = ([rate_mbit(hist[v], vfirst[v] + j, vfirst[v] + j + 1) / 1000.0 for j in range(int(T) - 1)]
                if vfirst[v] is not None else [])
        ok = sum(w >= SLO_FRAC * refs[v] for w in wins) / len(wins) if wins else float("nan")
        vres += [iperf_sum(vo[v], T), p5(wins), ok]
    oks = vres[2::3]
    ok_worst = float("nan") if any(math.isnan(x) for x in oks) else min(oks)   # the worse victim counts
    per = []          # per flooder: iperf3 mean, eBPF mean (Gbit/s), in FLOODERS order
    for g in FLOODERS:
        if alone:
            per += [0.0, 0.0]
        else:
            per += [iperf_sum(fo[g], flooder_dur(g)),
                    (hist[g][-1][1] - hist[g][0][1]) * 8 / 1e9 / flooder_dur(g)]
    for f in list(fo.values()) + list(vo.values()): f.close()
    # Jain over the eBPF (passed) rates of all flooders in the window where all are active (18-28 s)
    ov = overlap()
    rates = [rate_mbit(hist[g], t0 + ov[0], t0 + ov[1]) for g in FLOODERS]
    jf = float("nan") if alone else jain(rates)   # no flooders: fairness undefined
    tk = ticks[1:]    # tick statistics without the first tick (it spans the flooder/victim launch)
    return (*vres, ok_worst, *per, jf,
            nanstat(cpus, st.mean), nanstat(cpus, max), nanstat(steals, st.mean), nanstat(steals, max),
            nanstat(tk, st.mean), nanstat(tk, max)), ts

def ms(xs):
    xs = [x for x in xs if not math.isnan(x)]
    if not xs: return float("nan"), float("nan")
    return st.mean(xs), (st.stdev(xs) if len(xs) > 1 else 0.0)

def expected_duration(n_configs, reps):
    # per slot ~ OFFSET + T + ~2.5 s ssh/iperf3 overhead + PAUSE; per repeat the reference
    # (REF_RUNS x (REF_T + ~1 s + PAUSE), both victims together) plus (2N + 1) slots; SESSION_REST_S between repeats
    n_slots = 2 * n_configs + 1
    per_rep = REF_RUNS * (REF_T + 1 + PAUSE) + n_slots * (OFFSET + T + 2.5 + PAUSE)
    total = reps * per_rep + (reps - 1) * SESSION_REST_S
    return n_slots, per_rep, total

# result tuple layout of run(): 3 per victim, slo_ok_worst, 2 per flooder, then these 7
R_OKW = 3 * NV
R_FL = R_OKW + 1
R_TAIL = R_FL + 2 * NF   # jain, cpu_mean, cpu_max, steal_mean, steal_max, tick_mean, tick_max

def main():
    random.seed(7)
    configs = list(CONFIGS)
    if sorted(STEADY[SCENARIO]) != sorted(FLOODERS):
        raise SystemExit(f"SCENARIO {SCENARIO!r} defines flooders {sorted(STEADY[SCENARIO])}, FLOODERS is {FLOODERS}")
    if any(g not in GUESTS or g not in PAR for g in FLOODERS) or any(v not in GUESTS for v in VICTIMS):
        raise SystemExit(f"every flooder in {FLOODERS} needs a GUESTS and a PAR entry, every victim a GUESTS entry")
    for i in (0, 1, 2):
        if len({x[i] for x in GUESTS.values()}) != len(GUESTS):
            raise SystemExit(f"GUESTS: duplicate {('domain', 'IP', 'port')[i]}")
    for m, p in configs:
        if m not in MODES:
            raise SystemExit(f"mode {m!r} is not supported by exp38 (only {', '.join(MODES)})")
        if m == "static":
            try:
                x = float(p)
            except ValueError:
                raise SystemExit(f"{m} param {p!r} is malformed: expected a number")
            if not CAP_MIN <= x <= CAP_MAX:   # Mbit/s
                raise SystemExit(f"{m} param {p!r} out of range")
        elif m == "vfrot":
            VFROT[p] = parse_vfrot(p)
        elif m == "lqr":
            target, rwt = parse_lqr(p)
            LQR[p] = (target, rwt, lqr_gain(rwt))
    ALONE = ("alone", "na")   # victim-only control, run before the first config and after every config
    res = {c: [] for c in configs + [ALONE]}
    # never append to or overwrite earlier output: refuse to start if any output file already exists
    # (exp38.log is allowed only when it is this process's own stdout, i.e. just created by run38.sh)
    try:
        own_log = os.path.exists(LOG) and os.path.samestat(os.fstat(sys.stdout.fileno()), os.stat(LOG))
    except (OSError, ValueError, AttributeError):
        own_log = False
    existing = [f for f in (CSV, TS_CSV, REF_FILE) if os.path.exists(f)]
    if os.path.exists(LOG) and not own_log:
        existing.append(LOG)
    if existing:
        raise SystemExit("REFUSING TO START: output file(s) already exist: " + ", ".join(existing)
                         + " -- move or delete them first (exp38 never appends)")
    try:
        sudo(["true"])
    except subprocess.CalledProcessError:
        raise SystemExit("sudo needs a password: run 'sudo -v' first (run38.sh does this)")
    if not os.path.exists(OBJ):
        raise SystemExit(f"{OBJ} not found")
    check_servers()
    check_guests()
    w = STEADY[SCENARIO]
    print(f"SCENARIO {SCENARIO}: " + ", ".join(
              f"flooder {g} ({GUESTS[g][0]}) t={w[g][0]}-{sum(w[g])} s ({PAR[g]} streams)" for g in FLOODERS)
          + f", all active {overlap()[0]:g}-{overlap()[1]:g} s (Jain window), no bursts", flush=True)
    print("CONFIGS: " + ", ".join(f"{m} {p}" + (f" (thresh {VFROT[p][3]:.2f})" if m == "vfrot" else "")
                                  + (f" (Mbit/s per flooder, {NF * float(p) / 1000:g} Gbit/s total)" if m == "static" else "")
                                  for m, p in configs), flush=True)
    print(f"TOKEN BUCKET: all modes use the same xdp_limit.o on every flooder interface, burst "
          f"{XDP_BURST_NS / 1e6:g} ms at the cap rate; victim interfaces never limited (counter only)", flush=True)
    print(f"VICTIMS: " + ", ".join(f"{v} = {GUESTS[v][0]} {GUESTS[v][1]} port {GUESTS[v][2]}" for v in VICTIMS)
          + f", each {V_RATE:g} Gbit/s, one stream, constant for [0,{T}) s (launched together {OFFSET:g} s after the "
          f"flooders); slo_ok per victim = fraction of its {int(T) - 1} 1 s windows >= {SLO_FRAC * 100:g}% of its own "
          f"per-repeat reference; slo_ok_worst = the smaller of the two", flush=True)
    for v, cmd in victim_cmds(T).items():
        print(f"VICTIM CMD {v}: ssh alpine@{GUESTS[v][1]} '{cmd}'", flush=True)
    for g, cmd in flooder_cmds().items():
        print(f"FLOODER CMD {g}: ssh alpine@{GUESTS[g][1]} '{cmd}'", flush=True)
    for v, cmd in victim_cmds(REF_T).items():
        print(f"REF CMD {v}: ssh alpine@{GUESTS[v][1]} '{cmd}' (both together, {REF_RUNS} runs)", flush=True)
    print(f"vfrot: feedback = WORST victim, min over {'/'.join(VICTIMS)} of EMA(1 s rate)/own reference < thresh; "
          f"budget start {VF_B0:g}, clamp [{NF * CAP_MIN:g}, {VF_BMAX:g}] Mbit/s, rotation over {NF} holders "
          f"(holder gets B - (n_active - 1) x {CAP_MIN:g}); static/lqr never see the victims", flush=True)
    for p, (target, rwt, K) in LQR.items():
        print(f"LQR {p}: target {target:g}% host CPU, RWT {rwt:g}, budget clamp [{CAP_MIN:g}, {CAP_MAX:g}] Mbit/s, "
              f"start {INIT_CAP_MBIT:g} Mbit/s (equal split over {NF} flooders)", flush=True)
        for line in lqr_report(p, rwt, K):
            print(line, flush=True)
    c = cpu()
    print(f"HOST CPU: /proc/stat aggregate line, busy % = 1 - (idle + iowait)/total as exp37 (total = sum of all "
          f"fields); steal % = steal field / same total ({'reported' if c[2] is not None else 'NOT reported'} by "
          f"this kernel); {os.cpu_count()} host CPUs; tick length = time between consecutive reads of "
          f"{GUESTS[VICTIMS[0]][0]}'s counter (nominal {DT:g} s), WARNING if a slot's mean > {TICK_WARN_S:g} s "
          f"(first tick, which spans the launch, excluded)", flush=True)
    n_slots, per_rep, total = expected_duration(len(configs), REPS)
    print(f"EXPECTED DURATION: {n_slots} slots/repeat, ~{per_rep / 60:.1f} min/repeat, {REPS} repeats + "
          f"{REPS - 1} x {SESSION_REST_S} s rest ~ {total / 3600:.2f} h", flush=True)
    procs = []
    ref_hist = []     # references {victim: ref} used in each repeat
    base_cpus = []    # measured victims-alone host CPU mean in each repeat
    warnings = []     # (rep, slot, mode, param, tick mean)
    csv = open(CSV, "a")
    tsf = open(TS_CSV, "a")
    try:
        for g in VICTIMS + FLOODERS:
            IF[g] = attach(vnet(GUESTS[g][0]), 0)   # all unlimited; victims stay 0 (counter only)
        devs = [IF[g]["dev"] for g in IF]
        if len(set(devs)) != len(devs):
            raise SystemExit(f"two guests share a host interface: {devs}")
        print("INTERFACES: " + ", ".join(f"{g} = {GUESTS[g][0]} {IF[g]['dev']}" for g in IF), flush=True)
        for m, p in configs:
            if m == "static":
                burst_mb = float(p) / 8 * XDP_BURST_NS / 1e9   # MB the 5 ms burst allows at this cap
                print(f"STATIC {p}: per-flooder cap {float(p):g} Mbit/s (burst {burst_mb:.2f} MB), set once at the "
                      "start of each slot on " + ", ".join(
                          f"{g} = {GUESTS[g][0]} {IF[g]['dev']} (cfg map id {IF[g]['cfg']})" for g in FLOODERS)
                      + "; victims " + ", ".join(f"{GUESTS[v][0]} {IF[v]['dev']}" for v in VICTIMS) + " unlimited",
                      flush=True)
        csv.write(HEADER + "\n"); csv.flush()
        tsf.write(TS_HEADER + "\n"); tsf.flush()
        print(f"{REPS} repeats x ({len(configs)} configs + {len(configs) + 1} interleaved alone runs), "
              f"writing {CSV} and {TS_CSV}; references re-measured at the start of every repeat, {SESSION_REST_S} s "
              f"rest after every repeat except the last (the {COOLDOWN} s cool-down applies only to a repeat not "
              f"preceded by a rest)", flush=True)
        rested = False
        for rep in range(1, REPS + 1):
            if rep > 1 and not rested:
                print(f"cool-down {COOLDOWN} s before repeat {rep}", flush=True)
                time.sleep(COOLDOWN)
            rested = False
            # reference first (consumes no random numbers), then this repeat's shuffled order
            refs, base_cpu = reference(rep, procs); ref_hist.append(refs); base_cpus.append(base_cpu)
            print(f"REFERENCE rep {rep}: " + ", ".join(
                      f"{v} ({GUESTS[v][0]}) {refs[v]:.3f} Gbit/s (slo_ok threshold {SLO_FRAC * refs[v]:.3f})"
                      for v in VICTIMS)
                  + f" (median of {REF_RUNS} x {REF_T} s, both together, eBPF), base_cpu {base_cpu:.1f}%, saved to "
                  f"{REF_FILE}", flush=True)
            print("rep slot mode           param "
                  + "".join(f"{v}_mean {v}_p5 {v}_ok " for v in VICTIMS) + "ok_worst "
                  + "".join(f"{g}_iperf {g}_ebpf " for g in FLOODERS)
                  + " jain cpu_mean cpu_max steal_m steal_x tick_m tick_x", flush=True)
            order = configs[:]; random.shuffle(order)
            seq = [ALONE] + [x for c in order for x in (c, ALONE)]   # alone, c1, alone, ..., cN, alone
            for slot, (m, p) in enumerate(seq):
                r, ts = run(m, p, refs, base_cpu, procs); res[(m, p)].append(r)
                vr, okw, fl, tail = r[:R_OKW], r[R_OKW], r[R_FL:R_TAIL], r[R_TAIL:]
                csv.write(f"{rep},{slot},{m},{p}," + "".join(f"{x:.3f}," for x in vr) + f"{okw:.3f},"
                          + "".join(f"{x:.3f}," for x in fl)
                          + f"{tail[0]:.4f}," + "".join(f"{x:.2f}," for x in tail[1:5])
                          + "".join(f"{x:.4f}," for x in tail[5:7])
                          + ",".join(f"{refs[v]:.3f}" for v in VICTIMS) + "\n"); csv.flush()
                os.fsync(csv.fileno())
                # ts row = (t, tick, sample, cpu, steal, v 1 s per victim, v tick per victim, cap per flooder...,
                #           rate per flooder..., budget, worst)
                tsf.write("".join(f"{rep},{slot},{m},{p},{row[0]:.3f},{row[1]:.4f},{row[2]:.4f},{row[3]:.2f},"
                                  f"{row[4]:.2f},"
                                  + "".join(f"{x:.3f}," for x in row[5:5 + 2 * NV])
                                  + "".join(f"{c:.1f}," for c in row[5 + 2 * NV:5 + 2 * NV + NF])
                                  + "".join(f"{x:.3f}," for x in row[5 + 2 * NV + NF:5 + 2 * NV + 2 * NF])
                                  + f"{row[-2]:.1f},{row[-1]:.4f}\n" for row in ts)); tsf.flush()
                os.fsync(tsf.fileno())
                print(f"{rep:3d} {slot:4d} {m:7s} {p:>12s} "
                      + "".join(f"{a:7.2f} {b:5.2f} {c:5.2f} " for a, b, c in zip(vr[0::3], vr[1::3], vr[2::3]))
                      + f"{okw:8.2f} " + "".join(f"{a:7.2f} {b:6.2f} " for a, b in zip(fl[0::2], fl[1::2]))
                      + f"{tail[0]:5.3f} {tail[1]:8.1f} {tail[2]:7.1f} {tail[3]:7.2f} {tail[4]:7.2f} "
                      f"{tail[5]:6.3f} {tail[6]:6.3f}", flush=True)
                if tail[5] > TICK_WARN_S:
                    warnings.append((rep, slot, m, p, tail[5]))
                    print(f"WARNING: rep {rep} slot {slot} {m} {p}: mean control tick {tail[5]:.3f} s > "
                          f"{TICK_WARN_S:g} s (nominal {DT:g} s; max {tail[6]:.3f} s, steal mean {tail[3]:.2f}%)",
                          flush=True)
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

    names = ([f"{v}_{k}" for v in VICTIMS for k in ("mean", "p5", "ok")] + ["ok_worst"]
             + [f"{g}_{k}" for g in FLOODERS for k in ("iperf", "ebpf")]
             + ["jain", "cpu_mean", "cpu_max", "steal_mean", "steal_max", "tick_mean", "tick_max"])
    print("\nREFERENCES per repeat (Gbit/s): " + ", ".join(
        f"rep {i + 1} = " + "/".join(f"{v} {r[v]:.3f}" for v in VICTIMS) + f" (base_cpu {b:.1f}%)"
        for i, (r, b) in enumerate(zip(ref_hist, base_cpus))))
    print(f"TICK WARNINGS (mean tick > {TICK_WARN_S:g} s): {len(warnings)}" + "".join(
        f"\n  rep {a} slot {b} {c} {d}: {e:.3f} s" for a, b, c, d, e in warnings))
    print(f"\nSUMMARY (mean +- sd over {REPS} repeats; alone pooled over all {len(configs) + 1} slots per repeat)")
    print("mode           param    n " + " ".join(f"{n:>15s}" for n in names))
    for c in configs + [ALONE]:
        cols = [ms([r[j] for r in res[c]]) for j in range(len(names))]
        print(f"{c[0]:7s} {c[1]:>12s} {len(res[c]):4d} " + " ".join(f"{a:6.2f} +- {b:5.2f}" for a, b in cols))

if __name__ == "__main__":
    main()
