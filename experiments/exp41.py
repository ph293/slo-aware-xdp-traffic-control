import json, subprocess, time, struct, random, statistics as st, sys, os, math, tempfile, re, bisect, csv as csvmod
import numpy as np
REPS = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 6
T, DT = 40, 0.25
OFFSET = 0.5            # victims start 0.5 s after the flooders are launched (as exp5)
# exp41: mixed ("realistic") traffic on the 6 vCPU host, same scenario windows, slot interleave, seed, rests,
# refusals and logging as exp40, with the controllers of exp39 (static, lqr, vfrot) and exp40 (vffair) UNCHANGED
# (checked tick by tick in test_exp41.py). What changes is only the traffic and the V2 latency measurement:
#   V1 = 9 Gbit/s TCP (as before); V2 = 2 Gbit/s TCP, one stream, plus ping to the host every 0.2 s;
#   A = TCP bulk 2 streams, B = UDP flood -b 6G, C = bursty TCP (2 s on / 2 s off, 7 separate iperf3 runs),
#   D = TCP 8 streams. The controllers' feedback stays throughput / host CPU only (no latency input).
VICTIMS = ["V1", "V2"]                  # two victims, never limited (counter only); keyed by this list
FLOODERS = ["A", "B", "C", "D"]         # every per-flooder structure below is keyed by this list
FLOOD = {"A": "tcp", "B": "udp", "C": "burst", "D": "tcp"}   # exp41: traffic type per flooder
PAR = {"A": 2, "B": 1, "C": 2, "D": 8}  # iperf3 -P streams per flooder (exp41: B one UDP stream, D 8)
UDP_GBIT = 6.0          # exp41: B's UDP target rate (iperf3 -u -b), Gbit/s
BURST_ON, BURST_OFF = 2, 2   # exp41: C's on/off pattern, s; one iperf3 -t BURST_ON per on period
STEADY = {                              # steady scenarios: flooder -> (start s, duration s), t rel. flooder launch
    "steady4": {"A": (0, 28), "B": (6, 28), "C": (12, 28), "D": (18, 22)},  # all four scheduled during 18-28 s
}                                       # exp41: C's entry is the ENVELOPE of its bursts (12-40 s)
SCENARIO = "steady4"    # the only scenario
NV, NF = len(VICTIMS), len(FLOODERS)
KP, KI = 200.0, 300.0
CAP_MIN, CAP_MAX = 200.0, 70000.0
INIT_CAP_MBIT = 2000.0  # lqr: initial shared budget (split equally)
XDP_BURST_NS = 5000000  # token-bucket burst of xdp_limit.o (BURST_NS in xdp_limit.c, 5 ms at the cap rate),
                        # the same object and burst for every mode; only logged here, the program owns it
K_CPU = 0.93            # % host CPU per Gbit/s (from the Step 19 measurement)
V_RATE = {"V1": 9.0, "V2": 2.0}   # exp41: offered rate of each victim, Gbit/s, one TCP stream, constant over [0, T)
SLO_FRAC = 0.95         # slo_ok threshold (all modes): SLO_FRAC * that victim's own per-repeat reference
SMOOTH = 1.0            # s, victim rate window (1 s) for the vfrot feedback and the logged 1 s rates
PING_V = "V2"           # exp41: the victim that pings the host
PING_I = 0.2            # exp41: s, ping interval (busybox ping -i)
RTT_FRAC = 2.0          # exp41: slo_ok_v2_rtt = 1 iff the run's p99 over received replies <= max(RTT_FRAC x reference
RTT_FLOOR_MS = 2.0      #        p99, RTT_FLOOR_MS) AND its ping loss <= RTT_LOSS_MAX %; lost pings count as RTT SLO
RTT_LOSS_MAX = 1.0      #        violations only through the loss term (loss > 1 % fails, up to 1 % is tolerated)
REF_LOSS_MAX = 5.0      # exp41: %, the reference refuses to continue if its pooled ping loss is above this
REF_RTT_MIN_N = 10      # exp41: ... or if it has fewer RTT samples than this
DFS_ACTIVE_MBIT = 50.0  # vfrot/vffair: a flooder whose offered rate this tick is below this is inactive (gets CAP_MIN)
VFROT = {}               # (mode, param) -> (beta, add Gbit/s per s, rotation period s, thresh, bmax Mbit/s, floor_frac)
VFROT_VS_TAU = 0.5       # s, vfrot: EMA time constant of each victim's smoothed 1 s rate v
VFROT_THRESH = 0.98      # vfrot: default violation threshold (3-part param)
VFROT_HOLD = 1.0         # s, vfrot: no budget increase for this long after a violation tick
VFROT_CUT_HOLD = 1.0     # s, vfrot: no further cut for this long after a cut (the 1 s victim window lags recovery)
VF_B0, VF_BMAX = 10000.0, 16000.0   # vfrot: budget start and DEFAULT upper clamp (exp39), Mbit/s; lower clamp NF * CAP_MIN
VF_BMAX_RANGE = (4000.0, 30000.0)   # allowed range of a B<mbit> param suffix
HIGH_CAP_FRAC = 0.5      # a flooder holds a "high cap" on a tick when its cap >= this x budget
LQR = {}                 # lqr: param string -> (host CPU target %, R weight multiplier, gain K 1x2), set in main()
LQR_TAU = 0.5            # s, lqr plant time constant: y(k+1) = a*y(k) + b*u(k), a = exp(-DT/LQR_TAU),
                         # b = K_CPU*(1-a) (steady-state gain K_CPU)
LQR_Q = (1.0, 1.0)       # lqr: state weights Q = diag(q_e, q_d)
LQR_R = 0.5              # lqr: input weight on du (Gbit/s); R = LQR_R * RWT
TICK_WARN_S = 0.30       # s, a slot whose mean control tick is longer than this gets a WARNING line in the log
STEAL_WARN_MEAN, STEAL_WARN_MAX = 1.0, 5.0   # %, a slot with mean steal > 1 % or max steal > 5 % gets a WARNING
REF_RUNS, REF_T = 2, 20   # exp41: both victims + ping together, no flooders, 2 x 20 s at the start of EVERY repeat
PAUSE = 8
COOLDOWN = 45           # s, extra cool-down pause between repeats (thermal), on top of PAUSE
SESSION_REPEATS = 1     # repeats per session; a long rest follows each session except the last
SESSION_REST_S = 600    # s, thermal rest between sessions, on top of COOLDOWN
SLOT_S_MEASURED = 48.8  # s, exp39/exp40 measured time per slot
REF_S_MEASURED = 58.0   # s, exp41 estimate of the reference block: exp40's measured 38 s + 2 x 10 s longer runs
HOST = "10.10.0.1"
GUESTS = {"V1": ("tenant1", "10.10.0.11", 5201),  # victim 1
          "V2": ("tenant5", "10.10.0.15", 5205),  # victim 2 (+ ping)
          "A": ("tenant2", "10.10.0.12", 5202),   # flooder A
          "B": ("tenant3", "10.10.0.13", 5203),   # flooder B
          "C": ("tenant4", "10.10.0.14", 5204),   # flooder C
          "D": ("tenant6", "10.10.0.16", 5206)}   # flooder D
MAKE_SCRIPT = {"tenant5": "make_tenant5.sh", "tenant6": "make_tenant6.sh"}
HERE = os.path.dirname(os.path.abspath(__file__))
OBJ = os.path.join(HERE, "xdp_limit.o")
CSV = os.path.join(HERE, "results_dyn41.csv")
TS_CSV = os.path.join(HERE, "ts_dyn41.csv")
REF_FILE = os.path.join(HERE, "ref_victim41.txt")
LOG = os.path.join(HERE, "exp41.log")   # written by run41.sh (stdout redirect), checked in main()
# exp41: 7 configs -> 15 slots/repeat (alone interleaved as exp39/exp40); order of definition, shuffled per repeat
# in main(). CONFIGS[0] is the reference of the paired comparison in the summary.
# static: param = per-flooder cap in Mbit/s, set once at the start of the slot, no feedback (as exp39)
CONFIGS = [("vfrot", "0.8_1_2_0.94"),          # reference, identical to exp39/exp40 vfrot (BMAX 16000)
           ("vffair", "0.8_1_2_0.94_F0.25"),   # as exp40
           ("lqr", "79_4"), ("lqr", "85_4"), ("lqr", "90_4"),   # as exp39
           ("static", "2500"), ("static", "3500")]              # as exp39
NPROC_REQUIRED = 6      # main() refuses to start unless `nproc` prints exactly this
VF_MODES = ("vfrot", "vffair")
MODES = ("static", "lqr") + VF_MODES
CTRL_MODES = ("lqr",) + VF_MODES   # modes that rewrite the caps every tick (static writes them once)
RESULT_NAMES = ([n for v in VICTIMS for n in (f"victim{v[1:]}_mean_gbps", f"victim{v[1:]}_p5_gbps", f"slo_ok_v{v[1:]}")]
                + ["slo_ok_worst"]
                + [n for g in FLOODERS for n in (f"flooder{g}_iperf_gbps", f"flooder{g}_ebpf_gbps")]
                + ["jain_fairness", "cpu_mean", "cpu_max", "steal_mean", "steal_max", "tick_mean_s", "tick_max_s"])
EXTRA_NAMES = (["flooder_total_gbps", "min_flooder_own_gbps", "min_flooder_ovl_gbps", "budget_ceiling_share",
                "budget_floor_share"] + [f"highcap{g}_share" for g in FLOODERS])   # exp40: appended after the refs
NEW_NAMES = ([f"flooder{g}_offered_gbps" for g in FLOODERS]
             + ["v2_rtt_p50_ms", "v2_rtt_p99_ms", "v2_loss_pct", "slo_ok_v2_rtt", "slo_ok_worst_with_rtt"])  # exp41
REF_RTT_NAMES = ["ref_v2_rtt_p50_ms", "ref_v2_rtt_p99_ms"]   # exp41: last columns of the results CSV
# result tuple layout of run(): 3 per victim, slo_ok_worst, 2 per flooder, 7 tail values, EXTRA_NAMES, NEW_NAMES
R_OKW = 3 * NV
R_FL = R_OKW + 1
R_TAIL = R_FL + 2 * NF   # jain, cpu_mean, cpu_max, steal_mean, steal_max, tick_mean, tick_max
R_X = R_TAIL + 7         # exp40 extras
R_N = R_X + len(EXTRA_NAMES)   # exp41 extras
RESULT_NAMES = RESULT_NAMES + EXTRA_NAMES + NEW_NAMES
HEADER = ("rep,slot,mode,param," + ",".join(RESULT_NAMES[:R_X]) + ","
          + ",".join(f"ref_v{v[1:]}_gbps" for v in VICTIMS) + "," + ",".join(EXTRA_NAMES) + ","
          + ",".join(NEW_NAMES) + "," + ",".join(REF_RTT_NAMES))
TS_HEADER = ("rep,slot,mode,param,t,tick_s,sample_s,cpu,steal,"
             + "".join(f"v{v[1:]}_gbps_1s," for v in VICTIMS) + "".join(f"v{v[1:]}_gbps_tick," for v in VICTIMS)
             + "".join(f"cap{g}_mbit," for g in FLOODERS) + "".join(f"rate{g}_gbps," for g in FLOODERS)
             + "budget_mbit,worst_ratio,v2_rtt_ms_max,v2_ping_lost")
# ts row tuple (without rep,slot,mode,param): t, tick, sample, cpu, steal, 1 s per victim, tick per victim,
# cap per flooder, rate per flooder, budget, worst, then (exp41) max RTT and lost pings of the pings sent in the tick
TS_CAP = 5 + 2 * NV
TS_RATE = TS_CAP + NF
TS_BUDGET = TS_RATE + NF
TS_WORST = TS_BUDGET + 1
# summary metrics: (CSV column, short name, True = higher is better / False = lower is better / None = no winner)
SUMMARY_METRICS = [("slo_ok_worst", "ok_worst", True), ("slo_ok_worst_with_rtt", "ok_w_rtt", True),
                   ("slo_ok_v2_rtt", "rtt_ok", True), ("flooder_total_gbps", "fl_total", True),
                   ("min_flooder_own_gbps", "min_own", True), ("jain_fairness", "jain", True),
                   ("v2_rtt_p50_ms", "rtt_p50", False), ("v2_rtt_p99_ms", "rtt_p99", False),
                   ("v2_loss_pct", "loss_pct", False)]

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

def host_cpus():
    # (nproc, os.cpu_count()); nproc = CPUs usable by this process, None if nproc fails
    try:
        n = int(subprocess.run(["nproc"], capture_output=True, text=True, check=True).stdout.strip())
    except (OSError, ValueError, subprocess.CalledProcessError):
        n = None
    return n, os.cpu_count()

def headroom(base_cpu):
    # "lqr P +H" per lqr config, H = target - base_cpu (CPU percentage points)
    return ", ".join(f"lqr {p} {t:g} -> {t - base_cpu:+.1f}" for p, (t, _, _) in LQR.items())

def busy_steal(c0, c1):
    # host CPU busy % (as exp37) and steal % of the same total between two cpu() readings
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

# ---------- controllers (unchanged from exp39 / exp40) ----------

def parse_vfrot(mode, param):
    # "BETA_ADD_T[_THRESH][_B<bmax>]" for vfrot, "BETA_ADD_T[_THRESH]_F<floor_frac>" for vffair (as exp40)
    parts = param.split("_")
    bmax, ffrac = VF_BMAX, None
    try:
        if parts and parts[-1][:1] in ("B", "F"):
            suf = parts.pop()
            if suf[0] == "B":
                bmax = float(suf[1:])
            else:
                ffrac = float(suf[1:])
        if len(parts) not in (3, 4): raise ValueError
        beta, add, rot_t = (float(x) for x in parts[:3])
        thresh = float(parts[3]) if len(parts) == 4 else VFROT_THRESH
    except ValueError:
        raise SystemExit(f"{mode} param {param!r} is malformed: expected BETA_ADD_T[_THRESH][_B<bmax>] (vfrot) or "
                         f"BETA_ADD_T[_THRESH]_F<floor_frac> (vffair), e.g. 0.8_1_2_0.94_B12000 or 0.8_1_2_0.94_F0.5")
    if not (0 < beta < 1 and add > 0 and rot_t > 0 and 0.85 <= thresh <= 1.0):
        raise SystemExit(f"{mode} param {param!r} out of range: need 0 < BETA < 1, ADD > 0, T > 0 "
                         f"and 0.85 <= THRESH <= 1.0")
    if mode == "vfrot" and ffrac is not None:
        raise SystemExit(f"vfrot param {param!r}: the F<floor_frac> suffix is only for vffair")
    if mode == "vffair" and (ffrac is None or bmax != VF_BMAX):
        raise SystemExit(f"vffair param {param!r}: needs an F<floor_frac> suffix (and no B suffix)")
    if not VF_BMAX_RANGE[0] <= bmax <= VF_BMAX_RANGE[1]:
        raise SystemExit(f"{mode} param {param!r}: BMAX {bmax:g} outside [{VF_BMAX_RANGE[0]:g}, {VF_BMAX_RANGE[1]:g}]")
    if ffrac is not None and not 0 < ffrac < 1:
        raise SystemExit(f"vffair param {param!r}: need 0 < F < 1")
    return beta, add, rot_t, thresh, bmax, ffrac

def vf_params(mode, param):
    if (mode, param) not in VFROT:
        VFROT[(mode, param)] = parse_vfrot(mode, param)
    return VFROT[(mode, param)]

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
    for s in (0.33, 0.5, 0.67, 2.0, 3.0):
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

def fair_floor(budget, n_act, floor_frac):
    # vffair: cap of every active non-holder, floor_frac of an equal share of the budget, at least CAP_MIN
    return max(CAP_MIN, floor_frac * budget / n_act)

def fair_alloc(act, budget, holder, floor_frac):
    # vffair: every active non-holder gets fair_floor, the holder the rest of the budget (as exp40)
    n = len(act)
    fl = fair_floor(budget, n, floor_frac)
    on = high_cap(budget, n) if n == 1 else max(CAP_MIN, budget - (n - 1) * fl)
    return [on if g == holder else (fl if g in act else CAP_MIN) for g in FLOODERS]

def next_after(h, cands):
    # round-robin successor of h among cands, in FLOODERS order (h None or absent -> first after its slot)
    if h is None:
        return cands[0]
    i = FLOODERS.index(h)
    for g in FLOODERS[i + 1:] + FLOODERS[:i + 1]:
        if g in cands:
            return g

def hold_caps(act, budget, elapsed, rot_t, caps, state, pick, alloc=None):
    # one active flooder (the holder) gets high_cap, every other flooder CAP_MIN; the holder is
    # re-decided by pick(act, holder) when it is not active (immediately) or when it has held for
    # rot_t s; with no flooder active, keep the last caps; alloc (vffair) replaces the allocation
    if not act:
        return caps
    h = state["holder"]
    if h not in act or elapsed - state["since"] >= rot_t:
        state["holder"], state["since"] = pick(act, h), elapsed
    if alloc is not None:
        return alloc(act, budget, state["holder"])
    on = high_cap(budget, len(act))
    return [on if g == state["holder"] else CAP_MIN for g in FLOODERS]

def lowest_delivered_pick(delivered):
    # the active flooder with the LOWEST cumulative delivered traffic; ties go to a flooder that is not the
    # current holder (the next one round-robin)
    def pick(a, h):
        low = min(delivered[g] for g in a)
        tied = [g for g in a if delivered[g] == low]
        others = [g for g in tied if g != h]
        return next_after(h, others) if others else h
    return pick

def pidrotf_caps(act, budget, elapsed, rot_t, caps, delivered, state):
    # vfrot: the high cap goes to the active flooder with the lowest delivered traffic
    return hold_caps(act, budget, elapsed, rot_t, caps, state, lowest_delivered_pick(delivered))

def vffair_caps(act, budget, elapsed, rot_t, caps, delivered, state, floor_frac):
    # vffair: same holder decision as vfrot, allocation by fair_alloc
    return hold_caps(act, budget, elapsed, rot_t, caps, state, lowest_delivered_pick(delivered),
                     lambda a, b, h: fair_alloc(a, b, h, floor_frac))

def pf_update(delivered, passed, act, pf_all):
    # vfrot: add this tick's passed bytes per flooder, then zero the counters on the first tick
    # on which ALL flooders are active (so they span only that overlap window); returns the new pf_all.
    # exp41: unchanged; with the bursty C this reset happens at every C burst that makes all four active again
    for g in FLOODERS:
        delivered[g] += passed[g]
    all_on = len(act) == NF
    if all_on and not pf_all:
        for g in FLOODERS:
            delivered[g] = 0.0
    return all_on

# ---------- metrics ----------

def flooder_dur(g):
    # normalisation duration of flooder g's iperf/eBPF/offered means: its scheduled window (C: the envelope of
    # its bursts, off phases included)
    return STEADY[SCENARIO][g][1]

def overlap():
    # window in which all flooders are SCHEDULED (Jain fairness etc.), t relative to the flooder launch; exp41:
    # the same 18-28 s as exp40 (C is on only during 20-22 and 24-26 s inside it)
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

def budget_shares(ts, bmax):
    # share of ALL ticks of the slot with the budget at the ceiling bmax / at the lower clamp NF x CAP_MIN
    if not ts:
        return float("nan"), float("nan")
    b = [row[TS_BUDGET] for row in ts]
    return (sum(x >= bmax - 1e-6 for x in b) / len(b), sum(x <= NF * CAP_MIN + 1e-6 for x in b) / len(b))

def highcap_shares(ts):
    # per flooder, share of the ticks in the all-active window with cap >= HIGH_CAP_FRAC x budget
    ov = overlap()
    rows = [row for row in ts if ov[0] <= row[0] < ov[1]]
    if not rows:
        return [float("nan")] * NF
    return [sum(row[TS_CAP + i] >= HIGH_CAP_FRAC * row[TS_BUDGET] for row in rows) / len(rows) for i in range(NF)]

def holder_util(ts, bmax):
    # (log only): in the all-active window, ticks with the budget at the ceiling; there the holder (largest
    # cap) delivered this fraction of its cap on average; and the largest holder tick rate (Gbit/s)
    ov = overlap()
    u, r = [], []
    for row in ts:
        if ov[0] <= row[0] < ov[1] and row[TS_BUDGET] >= bmax - 1e-6:
            caps = row[TS_CAP:TS_CAP + NF]
            i = max(range(NF), key=lambda j: caps[j])
            u.append(row[TS_RATE + i] * 1000.0 / caps[i]); r.append(row[TS_RATE + i])
    return len(u), (st.mean(u) if u else float("nan")), (max(r) if r else float("nan"))

def q_incl(xs, pct):
    # exp41: inclusive (linear) percentile, integer pct, the same arithmetic as
    # statistics.quantiles(xs, n=100, method="inclusive")[pct - 1]; +inf values (lost pings) are allowed and
    # give +inf as soon as the interpolation touches one
    xs = sorted(xs); n = len(xs)
    if not n:
        return float("nan")
    m = n - 1
    j, delta = (pct * m) // 100, (pct * m) % 100
    if delta == 0 or j >= m:
        return xs[min(j, m)]
    a, b = xs[j], xs[j + 1]
    if math.isinf(b):
        return math.inf
    return (a * (100 - delta) + b * delta) / 100

PING_RE = re.compile(r"seq=(\d+) ttl=\d+ time=([0-9.]+) ms")
PING_TX_RE = re.compile(r"(\d+) packets transmitted")

def parse_ping(text, n_expected):
    # exp41: busybox ping output -> ({seq: rtt ms} of the first reply per seq, probes sent). Sent = the
    # "N packets transmitted" summary if present, else n_expected (ping killed, ssh failed, no output: every
    # missing seq is lost); duplicates (DUP!) and seq >= sent are ignored
    m = PING_TX_RE.search(text)
    sent = int(m.group(1)) if m else n_expected
    got = {}
    for s, t in PING_RE.findall(text):
        s = int(s)
        if s < sent and s not in got:
            got[s] = float(t)
    return got, sent

def rtt_stats(got, sent):
    # exp41: (p50 ms, p99 ms over the RECEIVED replies, loss %, p99 with every lost ping as +inf); the RTT SLO uses
    # p99 and loss, the +inf p99 is kept only for pilot41's log line
    rx = list(got.values())
    lost = max(0, sent - len(rx))
    loss = 100.0 * lost / sent if sent else float("nan")
    p99_slo = q_incl(rx + [math.inf] * lost, 99) if sent else float("nan")
    return q_incl(rx, 50), q_incl(rx, 99), loss, p99_slo

def rtt_thresh(ref_p99):
    # exp41: the RTT SLO bound on the run's p99, ms
    return max(RTT_FRAC * ref_p99, RTT_FLOOR_MS)

def rtt_ok(p99, ref_p99, loss=0.0):
    # exp41: 1.0 if p99 (over the received replies) <= rtt_thresh(ref_p99) and loss <= RTT_LOSS_MAX %, else 0.0
    # (no replies at all: 0.0); no reference: nan. loss defaults to 0 so pilot41's two-argument call still works
    if math.isnan(ref_p99):
        return float("nan")
    return 1.0 if (not math.isnan(p99) and p99 <= rtt_thresh(ref_p99)
                   and not loss > RTT_LOSS_MAX) else 0.0

def ping_ticks(t_rows, t_ping, got, sent):
    # exp41: per ts row i, (max RTT ms, lost count) of the pings SENT in (t_{i-1}, t_i]; send time of seq s =
    # t_ping + s x PING_I (approximate: the ssh setup before the first probe, ~0.1-0.3 s, is not corrected);
    # pings before the first row go to the first, after the last row to the last
    out = [[float("nan"), 0] for _ in t_rows]
    if not t_rows:
        return []
    for s in range(sent):
        i = min(bisect.bisect_left(t_rows, t_ping + s * PING_I), len(t_rows) - 1)
        if s in got:
            out[i][0] = got[s] if math.isnan(out[i][0]) else max(out[i][0], got[s])
        else:
            out[i][1] += 1
    return [tuple(x) for x in out]

def iperf_sum(f, secs=None):
    f.seek(0)
    try:
        s = json.loads(f.read())["end"]["sum_received"]
        return s["bytes"] * 8 / 1e9 / secs if secs else s["bits_per_second"] / 1e9
    except Exception:
        return float("nan")

def iperf_sum_files(fs, secs):
    # exp41: sum of the received bytes of several iperf3 -J documents (C's bursts), Gbit/s over secs;
    # nan if any of them is missing / unparsable (one file: exactly iperf_sum(f, secs))
    xs = [iperf_sum(f, secs) for f in fs]
    return float("nan") if not xs or any(math.isnan(x) for x in xs) else sum(xs)

def ssh(guest, cmd, out):
    return subprocess.Popen(["ssh", f"alpine@{GUESTS[guest][1]}", cmd], stdout=out, text=True)

def iperf_cmd(guest, secs, par=1, delay=0, rate_gbit=0, udp=False):
    pre = f"sleep {delay}; " if delay else ""
    p = f" -P {par}" if par > 1 else ""
    u = " -u" if udp else ""
    # -b is the per-stream target bitrate; iperf3 applies it to each -P stream, so the total
    # offered rate is split equally over the streams (rate_gbit / par per stream)
    b = f" -b {rate_gbit / par:g}G" if rate_gbit else ""
    return f"{pre}iperf3 -c {HOST} -p {GUESTS[guest][2]} -t {secs}{p}{u}{b} -J"

def ping_cmd(secs):
    # exp41: secs / PING_I probes every PING_I s; -W 1: wait at most 1 s for a missing last reply; -w: hard deadline
    return f"ping -c {int(round(secs / PING_I))} -i {PING_I:g} -W 1 -w {int(secs) + 2} {HOST}"

def victim_cmds(secs):
    # one iperf3 client per victim (one TCP stream at V_RATE[v]), all launched together
    return {v: iperf_cmd(v, secs, rate_gbit=V_RATE[v]) for v in VICTIMS}

def burst_starts(g):
    # exp41: start times (s rel. flooder launch) of the on periods of a bursty flooder inside its window
    s, d = STEADY[SCENARIO][g]
    return list(range(s, s + d - BURST_ON + 1, BURST_ON + BURST_OFF))

def flooder_cmds():
    # exp41: flooder -> list of delayed (sleep) iperf3 commands, all launched at slot start (no drift); one per
    # flooder, except the bursty one: one per on period
    out = {}
    for g in FLOODERS:
        s, d = STEADY[SCENARIO][g]
        if FLOOD[g] == "burst":
            out[g] = [iperf_cmd(g, BURST_ON, PAR[g], t) for t in burst_starts(g)]
        elif FLOOD[g] == "udp":
            out[g] = [iperf_cmd(g, d, PAR[g], s, UDP_GBIT, udp=True)]
        else:
            out[g] = [iperf_cmd(g, d, PAR[g], s)]
    return out

# ---------- setup ----------

def check_servers():
    # the host iperf3 servers are started by hand; abort if any of them is not listening
    out = subprocess.run(["ss", "-ltnH"], capture_output=True, text=True).stdout
    ports = sorted(GUESTS[g][2] for g in GUESTS)
    missing = [p for p in ports
               if not any(l.split()[3].endswith(f":{p}") for l in out.splitlines() if len(l.split()) > 3)]
    if missing:
        raise SystemExit("no iperf3 server listening on host port(s) " + ", ".join(map(str, missing))
                         + "; start them first: " + "; ".join(f"iperf3 -s -D -p {p}" for p in missing))
    print("iperf3 servers listening on ports " + ", ".join(map(str, ports)), flush=True)

def guest_has(ip, tools):
    # True if ssh alpine@ip answers non-interactively and every tool is on the guest's PATH
    try:
        p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", f"alpine@{ip}",
                            " && ".join(f"command -v {t}" for t in tools)], capture_output=True, text=True, timeout=20)
        return p.returncode == 0 and len(p.stdout.split()) == len(tools)
    except subprocess.TimeoutExpired:
        return False

def check_guests():
    # every guest must answer ssh non-interactively and have iperf3; exp41: the ping victim also needs ping
    bad = []
    for g, (dom, ip, _) in GUESTS.items():
        tools = ["iperf3", "ping"] if g == PING_V else ["iperf3"]
        if not guest_has(ip, tools):
            bad.append(f"{dom} ({ip}) needs {'+'.join(tools)}"
                       + (f" -- create it with: sudo bash {MAKE_SCRIPT[dom]}" if dom in MAKE_SCRIPT else ""))
    if bad:
        raise SystemExit("guest(s) not reachable by ssh alpine@IP or missing tools: " + "; ".join(bad))
    print("guests reachable with iperf3: " + ", ".join(f"{g}={GUESTS[g][0]}" for g in GUESTS)
          + f"; ping on {GUESTS[PING_V][0]}", flush=True)

def reference(rep, procs):
    # both victims together + ping (same conditions as the alone slots), flooder caps unlimited, no flooders;
    # each victim's reference = median of its own eBPF bytes on its own vnet over REF_RUNS x REF_T s;
    # exp41: RTT reference = p50/p99 of ALL received replies of the REF_RUNS ping runs pooled (lost pings are
    # only counted: refuse if loss > REF_LOSS_MAX % or < REF_RTT_MIN_N replies); base_cpu logged only
    for g in FLOODERS:
        set_cap(IF[g], 0)   # undo the caps left by the previous repeat's last run
    ebpf, ip = {v: [] for v in VICTIMS}, {v: [] for v in VICTIMS}
    cpus, steals, rtts, sent_all = [], [], [], 0
    n_ping = int(round(REF_T / PING_I))
    for i in range(REF_RUNS):
        outs = {v: tempfile.TemporaryFile("w+") for v in VICTIMS}
        po = tempfile.TemporaryFile("w+")
        c0 = {v: counters(IF[v])[2] for v in VICTIMS}
        k0 = cpu()
        ps = [ssh(v, cmd, outs[v]) for v, cmd in victim_cmds(REF_T).items()]
        pp = ssh(PING_V, ping_cmd(REF_T), po)
        procs += ps + [pp]
        for p in ps: p.wait()
        k1 = cpu()
        c1 = {v: counters(IF[v])[2] for v in VICTIMS}
        pp.wait()
        po.seek(0); got, sent = parse_ping(po.read(), n_ping); po.close()
        rtts += list(got.values()); sent_all += sent
        busy, steal = busy_steal(k0, k1)
        cpus.append(busy); steals.append(steal)
        for v in VICTIMS:
            ebpf[v].append((c1[v] - c0[v]) * 8 / 1e9 / REF_T); ip[v].append(iperf_sum(outs[v])); outs[v].close()
        print(f"rep {rep} ref run {i + 1}: both victims together, " + ", ".join(
                  f"{v} {ebpf[v][-1]:.2f} Gbit/s (eBPF) {ip[v][-1]:.2f} (iperf3)" for v in VICTIMS)
              + f", host CPU {busy:.1f}%, steal {steal:.1f}%, ping {len(got)}/{sent} replies", flush=True)
        time.sleep(PAUSE)
    refs = {v: st.median(ebpf[v]) for v in VICTIMS}
    base_cpu = st.mean(cpus)
    for v in VICTIMS:
        if math.isnan(refs[v]) or refs[v] <= 0:
            raise SystemExit(f"reference measurement failed (rep {rep}, victim {v} {GUESTS[v][0]})")
    loss = 100.0 * (sent_all - len(rtts)) / sent_all if sent_all else 100.0
    if loss > REF_LOSS_MAX or len(rtts) < REF_RTT_MIN_N:
        raise SystemExit(f"reference ping failed (rep {rep}, {GUESTS[PING_V][0]}): {len(rtts)} replies of "
                         f"{sent_all} probes, loss {loss:.1f}% (limit {REF_LOSS_MAX:g}%, at least {REF_RTT_MIN_N} replies)")
    rref = {"p50": q_incl(rtts, 50), "p99": q_incl(rtts, 99), "loss": loss, "n": len(rtts)}
    new = not os.path.exists(REF_FILE) or os.path.getsize(REF_FILE) == 0
    with open(REF_FILE, "a") as f:
        if new:
            f.write("# rep, time, victim, tenant, ref_gbps (median eBPF), mean host CPU %, mean steal %, eBPF runs, "
                    f"iperf3 runs, rtt_p50_ms, rtt_p99_ms, ping_loss_pct, n_rtt, rtt samples ms (pooled, receive "
                    f"order); victims together at " + "/".join(f"{V_RATE[v]:g}" for v in VICTIMS) + f" Gbit/s, "
                    f"ping from {PING_V} every {PING_I:g} s, {REF_RUNS} x {REF_T} s, no flooders, at the start of "
                    f"each repeat\n")
        for v in VICTIMS:
            rt = (f"{rref['p50']:.3f}, {rref['p99']:.3f}, {loss:.2f}, {len(rtts)}, "
                  + " ".join(f"{x:.3f}" for x in rtts)) if v == PING_V else "na, na, na, na, na"
            f.write(f"{rep}, {time.strftime('%Y-%m-%d %H:%M:%S')}, {v}, {GUESTS[v][0]}, {refs[v]:.3f}, "
                    f"{base_cpu:.1f}, {nanstat(steals, st.mean):.2f}, {' '.join(f'{x:.3f}' for x in ebpf[v])}, "
                    f"{' '.join(f'{x:.3f}' for x in ip[v])}, {rt}\n")
        f.flush(); os.fsync(f.fileno())
    return refs, base_cpu, rref

# ---------- one run ----------

def run(mode, param, refs, base_cpu, procs, rref):
    # refs = {victim: its reference Gbit/s}; base_cpu only logged; rref = reference RTT {"p50", "p99", ...}
    shared = INIT_CAP_MBIT
    if mode == "static":
        # param in Mbit/s; the same cap on every flooder, written below by set_cap before any flooder
        # starts and never again during the run (static is not in the per-tick set_cap list)
        caps = [float(param)] * NF
    elif mode in VF_MODES:
        # budget B starts at VF_B0, allocated by rotation; first flooder on (vfrot and vffair alike)
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
    alone = mode == "alone"   # victims only (+ ping), no flooders; same timeline as the other modes
    fcmds = {} if alone else flooder_cmds()
    fo = {g: [tempfile.TemporaryFile("w+") for _ in cmds] for g, cmds in fcmds.items()}   # one -J doc per command
    vo = {v: tempfile.TemporaryFile("w+") for v in VICTIMS}   # one -J document per victim
    po = tempfile.TemporaryFile("w+")                        # exp41: ping output
    t0 = time.monotonic()
    fp = [ssh(g, cmd, f) for g, cmds in fcmds.items() for cmd, f in zip(cmds, fo[g])]
    time.sleep(max(0.0, t0 + OFFSET - time.monotonic()))
    vp = {v: ssh(v, cmd, vo[v]) for v, cmd in victim_cmds(T).items()}   # both victims together
    t_ping = time.monotonic() - t0
    pp = ssh(PING_V, ping_cmd(T), po)                                 # exp41: ping, right after the victims
    procs += fp + list(vp.values()) + [pp]
    v0 = VICTIMS[0]   # its counter read time is the tick time (first interface sampled)
    c_prev = cpu(); cpus, steals, ticks = [], [], []
    vfirst = {v: None for v in VICTIMS}
    ts = []                      # one row per tick for ts_dyn41.csv
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
        dt = max(1e-3, tnow - prev[v0][0])   # real tick length, logged (the first tick spans the launch)
        ticks.append(dt)
        worst = float("nan")
        if mode == "lqr":
            # (exp39) host CPU measure (busy, this tick), tick DT, start budget INIT_CAP_MBIT, equal split and
            # clamp [CAP_MIN, CAP_MAX]; du = -K x, x = [e, d], e = busy - target, d = e(k) - e(k-1)
            # (d = 0 at the first tick); du in Gbit/s; the clamped budget is kept (anti-windup, no extra state)
            target, _, K = LQR[param]
            e = busy - target
            d = 0.0 if lq_e1 is None else e - lq_e1; lq_e1 = e
            du = -float((K @ np.array([e, d]))[0])
            shared = max(CAP_MIN, min(CAP_MAX, shared + 1000.0 * du))
            budget = shared
            caps = [shared / NF] * NF
        elif mode in VF_MODES:
            # (exp37/39/40) vfrot: B x beta on a violation (then no further cut for VFROT_CUT_HOLD s, no increase
            # for VFROT_HOLD s), else B += add per s; B clamped [NF*CAP_MIN, bmax]; the high cap rotates (every
            # rot_t s) to the active flooder with the lowest delivered bytes since all were active. Feedback = the
            # WORST victim, min over victims of EMA(1 s eBPF rate) / its own reference (throughput only, exp41
            # too); vffair differs only in the allocation (fair_alloc)
            beta, add, rot_t, thresh, bmax, ffrac = vf_params(mode, param)
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
            budget = max(NF * CAP_MIN, min(bmax, budget))
            act = active_flooders(offered, prev_off)
            pf_all = pf_update(delivered, {g: hist[g][-1][1] - prev[g][1] for g in FLOODERS}, act, pf_all)
            if mode == "vffair":
                caps = vffair_caps(act, budget, tnow - t0, rot_t, caps, delivered, pf_state, ffrac)
            else:
                caps = pidrotf_caps(act, budget, tnow - t0, rot_t, caps, delivered, pf_state)
        if mode in CTRL_MODES:
            for g, c in zip(FLOODERS, caps): set_cap(IF[g], c)
        # t relative to flooder launch; caps as set at this tick; rates over this tick
        ts.append((tnow - t0, dt, samp, busy, steal,
                   *(rate_mbit(hist[v], tnow - SMOOTH, tnow) / 1000.0 for v in VICTIMS),
                   *(tick[v] / 1000.0 for v in VICTIMS),
                   *caps, *(tick[g] / 1000.0 for g in FLOODERS),
                   budget if mode in CTRL_MODES else float("nan"), worst))
    for p in vp.values(): p.wait()
    for p in fp: p.wait()
    pp.wait()
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
    per, off = [], []   # per flooder: iperf3 mean, eBPF mean (Gbit/s); offered (passed + dropped) mean
    for g in FLOODERS:
        if alone:
            per += [0.0, 0.0]; off.append(0.0)
        else:
            per += [iperf_sum_files(fo[g], flooder_dur(g)),
                    (hist[g][-1][1] - hist[g][0][1]) * 8 / 1e9 / flooder_dur(g)]
            off.append((offered[g][-1][1] - offered[g][0][1]) * 8 / 1e9 / flooder_dur(g))
    po.seek(0); got, sent = parse_ping(po.read(), int(round(T / PING_I)))
    for f in [f for fs in fo.values() for f in fs] + list(vo.values()) + [po]: f.close()
    # Jain over the eBPF (passed) rates of all flooders in the window where all are scheduled (18-28 s)
    ov = overlap()
    rates = [rate_mbit(hist[g], t0 + ov[0], t0 + ov[1]) for g in FLOODERS]
    jf = float("nan") if alone else jain(rates)   # no flooders: fairness undefined
    tk = ticks[1:]    # tick statistics without the first tick (it spans the flooder/victim launch)
    # exp40 extras: flooder total = sum of the eBPF means; min flooder over each flooder's own window (eBPF means)
    # and over the all-active window; budget ceiling/floor shares; per-flooder high-cap shares (vfrot/vffair)
    nan = float("nan")
    if alone:
        extras = (0.0, nan, nan, nan, nan) + (nan,) * NF
    else:
        own = per[1::2]
        ceil_sh, floor_sh = budget_shares(ts, vf_params(mode, param)[4]) if mode in VF_MODES else (nan, nan)
        extras = (sum(own), min(own), min(rates) / 1000.0, ceil_sh, floor_sh,
                  *(highcap_shares(ts) if mode in VF_MODES else (nan,) * NF))
    # exp41: V2 latency: p50/p99 of the received replies, loss, RTT SLO (p99 bound and loss <= RTT_LOSS_MAX %),
    # SLO in both dimensions
    p50, p99, loss, _ = rtt_stats(got, sent)
    ok_rtt = rtt_ok(p99, rref["p99"], loss)
    pt = ping_ticks([row[0] for row in ts], t_ping, got, sent)
    ts = [row + x for row, x in zip(ts, pt)]
    return (*vres, ok_worst, *per, jf,
            nanstat(cpus, st.mean), nanstat(cpus, max), nanstat(steals, st.mean), nanstat(steals, max),
            nanstat(tk, st.mean), nanstat(tk, max), *extras,
            *off, p50, p99, loss, ok_rtt, ok_worst * ok_rtt), ts

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

def measured_duration(n_configs, reps):
    # the same at exp39/exp40's measured rate (SLOT_S_MEASURED per slot, REF_S_MEASURED per reference)
    n_slots = 2 * n_configs + 1
    per_rep = REF_S_MEASURED + n_slots * SLOT_S_MEASURED
    return n_slots, per_rep, reps * per_rep + (reps - 1) * SESSION_REST_S

def csv_line(rep, slot, m, p, r, refs, rref):
    vr, okw, fl, tail, ex, nw = r[:R_OKW], r[R_OKW], r[R_FL:R_TAIL], r[R_TAIL:R_X], r[R_X:R_N], r[R_N:]
    return (f"{rep},{slot},{m},{p}," + "".join(f"{x:.3f}," for x in vr) + f"{okw:.3f},"
            + "".join(f"{x:.3f}," for x in fl)
            + f"{tail[0]:.4f}," + "".join(f"{x:.2f}," for x in tail[1:5])
            + "".join(f"{x:.4f}," for x in tail[5:7])
            + "".join(f"{refs[v]:.3f}," for v in VICTIMS)
            + ",".join([f"{x:.3f}" for x in ex[:3]] + [f"{x:.4f}" for x in ex[3:]]) + ","
            + ",".join([f"{x:.3f}" for x in nw[:NF + 2]] + [f"{nw[NF + 2]:.2f}", f"{nw[NF + 3]:.3f}",
                                                             f"{nw[NF + 4]:.3f}"])
            + f",{rref['p50']:.3f},{rref['p99']:.3f}\n")

def ts_line(rep, slot, m, p, row):
    # one ts_dyn41.csv line (exp40's layout + max RTT and lost pings of the tick)
    return (f"{rep},{slot},{m},{p},{row[0]:.3f},{row[1]:.4f},{row[2]:.4f},{row[3]:.2f},{row[4]:.2f},"
            + "".join(f"{x:.3f}," for x in row[5:TS_CAP])
            + "".join(f"{c:.1f}," for c in row[TS_CAP:TS_RATE])
            + "".join(f"{x:.3f}," for x in row[TS_RATE:TS_BUDGET])
            + f"{row[TS_BUDGET]:.1f},{row[TS_WORST]:.4f},{row[TS_WORST + 1]:.3f},{int(row[TS_WORST + 2])}\n")

# ---------- summary (also offline: python3 exp41.py --summary [results_dyn41.csv]) ----------

def _betacf(a, b, x):
    # continued fraction of the incomplete beta function (modified Lentz)
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny); h = d
    for m in range(1, 1000):
        m2 = 2 * m
        for aa in (m * (b - m) * x / ((qam + m2) * (a + m2)), -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))):
            d = 1.0 + aa * d; d = 1.0 / (d if abs(d) > tiny else tiny)
            c = 1.0 + aa / c; c = c if abs(c) > tiny else tiny
            h *= d * c
        if abs(d * c - 1.0) < 1e-15:
            break
    return h

def betainc(a, b, x):
    # regularized incomplete beta I_x(a, b)
    if x <= 0.0: return 0.0
    if x >= 1.0: return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b

def paired_p(ds):
    # two-sided paired t-test p-value of the differences ds (H0: mean 0), Student t with n - 1 df;
    # n < 2: nan; sd 0: 1.0 if the mean is 0, else 0.0 (printed with "(sd 0)")
    n = len(ds)
    if n < 2:
        return float("nan")
    m, sd = st.mean(ds), st.stdev(ds)
    if sd <= 1e-12:
        return 1.0 if abs(m) <= 1e-12 else 0.0
    t = m / (sd / math.sqrt(n)); df = n - 1
    return betainc(df / 2.0, 0.5, df / (df + t * t))

def summary_data(path):
    # per config (alone excluded): {metric: {rep: value}}; configs in CONFIGS order, then any others in file order;
    # alone: list of rows (RTT SLO false-alarm rate)
    vals, alone = {}, []
    with open(path) as f:
        for row in csvmod.DictReader(f):
            c = (row["mode"], row["param"])
            if c[0] == "alone":
                alone.append({m: float(row[m]) for m in ("slo_ok_v2_rtt", "v2_rtt_p50_ms", "v2_rtt_p99_ms",
                                                         "v2_loss_pct")})
                continue
            d = vals.setdefault(c, {m: {} for m, _, _ in SUMMARY_METRICS})
            for m, _, _ in SUMMARY_METRICS:
                d[m][int(row["rep"])] = float(row[m])
    order = [c for c in CONFIGS if c in vals] + [c for c in vals if c not in CONFIGS]
    stats = {c: {m: (*ms(list(vals[c][m].values())), len(vals[c][m])) for m, _, _ in SUMMARY_METRICS} for c in order}
    ref = CONFIGS[0]
    diffs = {}
    if ref in vals:
        for c in order:
            if c == ref:
                continue
            diffs[c] = {}
            for m, _, better in SUMMARY_METRICS:
                ds = [vals[c][m][r] - vals[ref][m][r] for r in sorted(vals[c][m])
                      if r in vals[ref][m] and not math.isnan(vals[c][m][r]) and not math.isnan(vals[ref][m][r])]
                hi = sum(d > 1e-9 for d in ds); lo = sum(d < -1e-9 for d in ds)
                wtl = None if better is None else ((hi, len(ds) - hi - lo, lo) if better else (lo, len(ds) - hi - lo, hi))
                sd0 = len(ds) > 1 and st.stdev(ds) <= 1e-12 and abs(st.mean(ds)) > 1e-12
                diffs[c][m] = (*ms(ds), len(ds), wtl, paired_p(ds), sd0)
    return order, stats, diffs, alone

def summarize(path):
    order, stats, diffs, alone = summary_data(path)
    ref = CONFIGS[0]
    out = [f"SUMMARY41 from {os.path.basename(path)} (mean +- sd over repeats; alone excluded)",
           f"{'mode':7s} {'param':>20s} {'n':>3s} " + " ".join(f"{s:>15s}" for _, s, _ in SUMMARY_METRICS)]
    for c in order:
        n = max(v[2] for v in stats[c].values())
        out.append(f"{c[0]:7s} {c[1]:>20s} {n:3d} "
                   + " ".join(f"{stats[c][m][0]:6.3f} +- {stats[c][m][1]:5.3f}" for m, _, _ in SUMMARY_METRICS))
    out.append(f"PAIRED vs {ref[0]} {ref[1]} (per repeat: config - reference, mean +- sd, W/T/L = better/equal/worse "
               f"than the reference in the same repeat (rtt_p50, rtt_p99, loss_pct: lower is better, all others "
               f"higher), p = two-sided "
               f"paired t-test)")
    out.append(f"{'mode':7s} {'param':>20s} {'n':>3s} " + " ".join(f"{s:>32s}" for _, s, _ in SUMMARY_METRICS))
    for c, d in diffs.items():
        n = max(v[2] for v in d.values())
        out.append(f"{c[0]:7s} {c[1]:>20s} {n:3d} " + " ".join(
            (f"{d[m][0]:+7.3f} +- {d[m][1]:5.3f} " + (f"{d[m][3][0]}/{d[m][3][1]}/{d[m][3][2]}" if d[m][3] else "-")
             + f" p={d[m][4]:.3f}" + ("(sd0)" if d[m][5] else "")).rjust(32) for m, _, _ in SUMMARY_METRICS))
    if not diffs:
        out.append(f"(reference config {ref[0]} {ref[1]} not in the file: no paired differences)")
    if alone:
        ok = [a["slo_ok_v2_rtt"] for a in alone if not math.isnan(a["slo_ok_v2_rtt"])]
        p50 = ms([a["v2_rtt_p50_ms"] for a in alone]); p99 = ms([a["v2_rtt_p99_ms"] for a in alone])
        ls = ms([a["v2_loss_pct"] for a in alone])
        out.append(f"ALONE controls: {len(alone)} slots, RTT SLO passed in {int(sum(ok))}/{len(ok)} (false-alarm rate "
                   f"of the RTT SLO with no flooders {1 - sum(ok) / len(ok) if ok else float('nan'):.3f}), "
                   f"rtt_p50 {p50[0]:.3f} +- {p50[1]:.3f} ms, rtt_p99 {p99[0]:.3f} +- {p99[1]:.3f} ms, "
                   f"loss {ls[0]:.2f} +- {ls[1]:.2f} %")
    return "\n".join(out)

def setup_configs(configs):
    # validate every config and fill VFROT / LQR; labels (mode, param) must be unique
    if len(set(configs)) != len(configs):
        raise SystemExit(f"duplicate config label(s) in {configs}")
    for m, p in configs:
        if m not in MODES:
            raise SystemExit(f"mode {m!r} is not supported by exp41 (only {', '.join(MODES)})")
        if m == "static":
            try:
                x = float(p)
            except ValueError:
                raise SystemExit(f"{m} param {p!r} is malformed: expected a number")
            if not CAP_MIN <= x <= CAP_MAX:   # Mbit/s
                raise SystemExit(f"{m} param {p!r} out of range")
        elif m == "lqr":
            target, rwt = parse_lqr(p)
            LQR[p] = (target, rwt, lqr_gain(rwt))
        else:
            VFROT[(m, p)] = parse_vfrot(m, p)

def main():
    random.seed(7)
    configs = list(CONFIGS)
    if sorted(STEADY[SCENARIO]) != sorted(FLOODERS):
        raise SystemExit(f"SCENARIO {SCENARIO!r} defines flooders {sorted(STEADY[SCENARIO])}, FLOODERS is {FLOODERS}")
    if any(g not in GUESTS or g not in PAR or g not in FLOOD for g in FLOODERS) or any(v not in GUESTS for v in VICTIMS):
        raise SystemExit(f"every flooder in {FLOODERS} needs a GUESTS, PAR and FLOOD entry, every victim a GUESTS entry")
    for i in (0, 1, 2):
        if len({x[i] for x in GUESTS.values()}) != len(GUESTS):
            raise SystemExit(f"GUESTS: duplicate {('domain', 'IP', 'port')[i]}")
    setup_configs(configs)
    ALONE = ("alone", "na")   # victim-only control, run before the first config and after every config
    res = {c: [] for c in configs + [ALONE]}
    # never append to or overwrite earlier output: refuse to start if any output file already exists
    # (exp41.log is allowed only when it is this process's own stdout, i.e. just created by run41.sh)
    try:
        own_log = os.path.exists(LOG) and os.path.samestat(os.fstat(sys.stdout.fileno()), os.stat(LOG))
    except (OSError, ValueError, AttributeError):
        own_log = False
    existing = [f for f in (CSV, TS_CSV, REF_FILE) if os.path.exists(f)]
    if os.path.exists(LOG) and not own_log:
        existing.append(LOG)
    if existing:
        raise SystemExit("REFUSING TO START: output file(s) already exist: " + ", ".join(existing)
                         + " -- move or delete them first (exp41 never appends)")
    nproc, ncpu = host_cpus()
    if nproc != NPROC_REQUIRED:
        raise SystemExit(f"REFUSING TO START: nproc = {nproc} (os.cpu_count() = {ncpu}), exp41 requires exactly "
                         f"{NPROC_REQUIRED}: it continues exp39/exp40 on the 6 vCPU host VM and is only comparable there")
    try:
        sudo(["true"])
    except subprocess.CalledProcessError:
        raise SystemExit("sudo needs a password: run 'sudo -v' first (run41.sh does this)")
    if not os.path.exists(OBJ):
        raise SystemExit(f"{OBJ} not found")
    check_servers()
    check_guests()
    w = STEADY[SCENARIO]
    kind = {"tcp": "TCP bulk", "udp": f"UDP flood -b {UDP_GBIT:g}G", "burst": f"bursty TCP {BURST_ON} s on / "
            f"{BURST_OFF} s off"}
    print(f"SCENARIO {SCENARIO} (exp41 mixed traffic): " + ", ".join(
              f"flooder {g} ({GUESTS[g][0]}) t={w[g][0]}-{sum(w[g])} s {kind[FLOOD[g]]} ({PAR[g]} stream"
              f"{'s' if PAR[g] > 1 else ''})" for g in FLOODERS)
          + f"; all scheduled {overlap()[0]:g}-{overlap()[1]:g} s (Jain / min_ovl / high-cap window, as exp40)",
          flush=True)
    for g in FLOODERS:
        if FLOOD[g] == "burst":
            print(f"BURSTS {g}: {len(burst_starts(g))} separate iperf3 -t {BURST_ON} runs, all launched at slot start "
                  f"with sleep {', '.join(map(str, burst_starts(g)))} s; own-window means over its envelope "
                  f"{w[g][0]}-{sum(w[g])} s ({flooder_dur(g)} s, off phases included); vfrot/vffair see it go "
                  f"inactive between bursts (offered < {DFS_ACTIVE_MBIT:g} Mbit/s) and, unchanged from exp40, reset "
                  f"the delivered counters each time all four become active again", flush=True)
    print("CONFIGS: " + ", ".join(
        f"{m} {p}" + (f" ({NF * float(p) / 1000:g} Gbit/s total)" if m == "static" else "")
        + (" (beta {:g}, add {:g}, T {:g}, thresh {:.2f}, BMAX {:g}{})".format(
            *VFROT[(m, p)][:5], f", floor {VFROT[(m, p)][5]:g}" if VFROT[(m, p)][5] is not None else "")
           if m in VF_MODES else "")
        for m, p in configs), flush=True)
    print(f"REFERENCE CONFIG for the paired summary: {configs[0][0]} {configs[0][1]} (identical to exp39/exp40 vfrot)",
          flush=True)
    print(f"TOKEN BUCKET: all modes use the same xdp_limit.o on every flooder interface, burst "
          f"{XDP_BURST_NS / 1e6:g} ms at the cap rate; victim interfaces never limited (counter only)", flush=True)
    print(f"VICTIMS: " + ", ".join(f"{v} = {GUESTS[v][0]} {GUESTS[v][1]} port {GUESTS[v][2]} {V_RATE[v]:g} Gbit/s"
                                   for v in VICTIMS)
          + f", one TCP stream each, constant for [0,{T}) s (launched together {OFFSET:g} s after the flooders); "
          f"slo_ok per victim = fraction of its {int(T) - 1} 1 s windows >= {SLO_FRAC * 100:g}% of its own per-repeat "
          f"reference; slo_ok_worst = the smaller of the two", flush=True)
    print(f"LATENCY: {GUESTS[PING_V][0]} pings {HOST} every {PING_I:g} s during each run ({int(round(T / PING_I))} "
          f"probes) and each reference run; reference p99 = p99 of all received reference replies of the repeat "
          f"({REF_RUNS} x {int(round(REF_T / PING_I))} probes; loss > {REF_LOSS_MAX:g}% refuses); per run: p50/p99 "
          f"over received replies, loss %; slo_ok_v2_rtt = 1 iff the run's p99 <= max({RTT_FRAC:g} x reference "
          f"p99, {RTT_FLOOR_MS:g} ms) AND loss <= {RTT_LOSS_MAX:g}% (lost pings fail the RTT SLO only through loss > "
          f"{RTT_LOSS_MAX:g}%, i.e. more than {int(RTT_LOSS_MAX / 100 * round(T / PING_I))} of "
          f"{int(round(T / PING_I))} probes); "
          f"slo_ok_worst_with_rtt = min(ok_v1, ok_v2) x slo_ok_v2_rtt; the controllers never see the RTT", flush=True)
    for v, cmd in victim_cmds(T).items():
        print(f"VICTIM CMD {v}: ssh alpine@{GUESTS[v][1]} '{cmd}'", flush=True)
    print(f"PING CMD {PING_V}: ssh alpine@{GUESTS[PING_V][1]} '{ping_cmd(T)}'", flush=True)
    for g, cmds in flooder_cmds().items():
        for cmd in cmds:
            print(f"FLOODER CMD {g}: ssh alpine@{GUESTS[g][1]} '{cmd}'", flush=True)
    for v, cmd in victim_cmds(REF_T).items():
        print(f"REF CMD {v}: ssh alpine@{GUESTS[v][1]} '{cmd}' (both together + ping, {REF_RUNS} runs)", flush=True)
    print(f"REF PING CMD {PING_V}: ssh alpine@{GUESTS[PING_V][1]} '{ping_cmd(REF_T)}'", flush=True)
    print(f"vfrot: feedback = WORST victim, min over {'/'.join(VICTIMS)} of EMA(1 s rate)/own reference < thresh; "
          f"budget start {VF_B0:g}, clamp [{NF * CAP_MIN:g}, BMAX] Mbit/s, rotation over {NF} holders (holder gets "
          f"B - (n_active - 1) x {CAP_MIN:g}); vffair as exp40 (active non-holders get max({CAP_MIN:g}, F x B / "
          f"n_active)); static/lqr never see the victims", flush=True)
    for p, (target, rwt, K) in LQR.items():
        print(f"LQR {p}: target {target:g}% host CPU on {nproc} CPUs (nproc), RWT {rwt:g}, budget clamp "
              f"[{CAP_MIN:g}, {CAP_MAX:g}] Mbit/s, start {INIT_CAP_MBIT:g} Mbit/s (equal split over {NF} flooders); "
              f"model a, b, K unchanged from exp39 (K_CPU {K_CPU:g} %/Gbit/s measured on 4 CPUs)", flush=True)
        for line in lqr_report(p, rwt, K):
            print(line, flush=True)
    print(f"METRICS: as exp40 (flooder_total = sum of the eBPF means over the own windows, min_flooder_own, "
          f"min_flooder_ovl over {overlap()[0]:g}-{overlap()[1]:g} s, budget shares and high-cap shares for "
          f"vfrot/vffair, Jain over the same window); exp41: flooderX_offered_gbps = eBPF passed + dropped over the "
          f"own window (what the guest really offered: B's UDP rate is not reduced by the cap)", flush=True)
    c = cpu()
    print(f"HOST CPU: /proc/stat aggregate line, busy % = 1 - (idle + iowait)/total as exp37 (total = sum of all "
          f"fields); steal % = steal field / same total ({'reported' if c[2] is not None else 'NOT reported'} by "
          f"this kernel); host CPUs: nproc {nproc}, os.cpu_count() {ncpu}; tick length = time between consecutive reads of "
          f"{GUESTS[VICTIMS[0]][0]}'s counter (nominal {DT:g} s), WARNING if a slot's mean > {TICK_WARN_S:g} s "
          f"(first tick, which spans the launch, excluded) or its steal mean > {STEAL_WARN_MEAN:g}% or max > "
          f"{STEAL_WARN_MAX:g}%, or a flooder's iperf3 report is missing", flush=True)
    n_slots, per_rep, total = expected_duration(len(configs), REPS)
    _, per_rep_m, total_m = measured_duration(len(configs), REPS)
    print(f"EXPECTED DURATION: {n_slots} slots/repeat, ~{per_rep / 60:.1f} min/repeat (exp39/40 measured rate "
          f"~{per_rep_m / 60:.1f}), {REPS} repeats + {REPS - 1} x {SESSION_REST_S} s rest ~ {total / 3600:.2f} h "
          f"(measured rate ~{total_m / 3600:.2f} h)", flush=True)
    procs = []
    ref_hist = []     # references ({victim: ref}, base_cpu, rref) used in each repeat
    warnings = []     # (rep, slot, mode, param, text)
    csv = open(CSV, "a")
    tsf = open(TS_CSV, "a")
    try:
        for g in VICTIMS + FLOODERS:
            IF[g] = attach(vnet(GUESTS[g][0]), 0)   # all unlimited; victims stay 0 (counter only)
        devs = [IF[g]["dev"] for g in IF]
        if len(set(devs)) != len(devs):
            raise SystemExit(f"two guests share a host interface: {devs}")
        print("INTERFACES: " + ", ".join(f"{g} = {GUESTS[g][0]} {IF[g]['dev']}" for g in IF), flush=True)
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
            refs, base_cpu, rref = reference(rep, procs); ref_hist.append((refs, base_cpu, rref))
            print(f"REFERENCE rep {rep}: " + ", ".join(
                      f"{v} ({GUESTS[v][0]}) {refs[v]:.3f} Gbit/s (slo_ok threshold {SLO_FRAC * refs[v]:.3f})"
                      for v in VICTIMS)
                  + f" (median of {REF_RUNS} x {REF_T} s, both together, eBPF), base_cpu {base_cpu:.1f}%; RTT "
                  f"{PING_V} p50 {rref['p50']:.3f} ms, p99 {rref['p99']:.3f} ms (RTT SLO threshold "
                  f"{rtt_thresh(rref['p99']):.3f} ms, loss <= {RTT_LOSS_MAX:g}%), loss {rref['loss']:.2f}% of "
                  f"{rref['n']} replies; saved to "
                  f"{REF_FILE}", flush=True)
            print(f"LQR TARGETS rep {rep}: base_cpu {base_cpu:.1f}% on {nproc} CPUs; target -> headroom: "
                  + headroom(base_cpu), flush=True)
            print("rep slot mode                  param "
                  + "".join(f"{v}_mean {v}_p5 {v}_ok " for v in VICTIMS) + "ok_worst "
                  + "".join(f"{g}_iperf {g}_ebpf " for g in FLOODERS)
                  + " jain cpu_mean cpu_max steal_m steal_x tick_m tick_x  fl_tot min_own min_ovl ceil_sh floor_sh "
                  + " ".join(f"hc{g}" for g in FLOODERS) + " " + " ".join(f"off{g}" for g in FLOODERS)
                  + " rtt_p50 rtt_p99 loss% rtt_ok okw_rtt", flush=True)
            order = configs[:]; random.shuffle(order)
            seq = [ALONE] + [x for c in order for x in (c, ALONE)]   # alone, c1, alone, ..., cN, alone
            for slot, (m, p) in enumerate(seq):
                r, ts = run(m, p, refs, base_cpu, procs, rref); res[(m, p)].append(r)
                vr, okw, fl, tail, ex, nw = r[:R_OKW], r[R_OKW], r[R_FL:R_TAIL], r[R_TAIL:R_X], r[R_X:R_N], r[R_N:]
                csv.write(csv_line(rep, slot, m, p, r, refs, rref)); csv.flush()
                os.fsync(csv.fileno())
                tsf.write("".join(ts_line(rep, slot, m, p, row) for row in ts)); tsf.flush()
                os.fsync(tsf.fileno())
                print(f"{rep:3d} {slot:4d} {m:7s} {p:>20s} "
                      + "".join(f"{a:7.2f} {b:5.2f} {c:5.2f} " for a, b, c in zip(vr[0::3], vr[1::3], vr[2::3]))
                      + f"{okw:8.2f} " + "".join(f"{a:7.2f} {b:6.2f} " for a, b in zip(fl[0::2], fl[1::2]))
                      + f"{tail[0]:5.3f} {tail[1]:8.1f} {tail[2]:7.1f} {tail[3]:7.2f} {tail[4]:7.2f} "
                      f"{tail[5]:6.3f} {tail[6]:6.3f} {ex[0]:7.2f} {ex[1]:7.2f} {ex[2]:7.2f} {ex[3]:7.3f} {ex[4]:8.3f} "
                      + " ".join(f"{x:4.2f}" for x in ex[5:]) + " " + " ".join(f"{x:5.2f}" for x in nw[:NF])
                      + f" {nw[NF]:7.3f} {nw[NF + 1]:7.3f} {nw[NF + 2]:5.1f} {nw[NF + 3]:6.0f} {nw[NF + 4]:7.2f}",
                      flush=True)
                if m in VF_MODES:
                    bmax = VFROT[(m, p)][4]
                    n_c, u_c, r_c = holder_util(ts, bmax)
                    print(f"HOLDER rep {rep} slot {slot} {m} {p}: {n_c} ticks at the {bmax:g} Mbit/s ceiling in "
                          f"{overlap()[0]:g}-{overlap()[1]:g} s; holder delivered/cap there mean {u_c:.3f}, max holder "
                          f"tick rate {r_c:.2f} Gbit/s", flush=True)
                if tail[5] > TICK_WARN_S:
                    txt = (f"mean control tick {tail[5]:.3f} s > {TICK_WARN_S:g} s (nominal {DT:g} s; max "
                           f"{tail[6]:.3f} s, steal mean {tail[3]:.2f}%)")
                    warnings.append((rep, slot, m, p, txt))
                    print(f"WARNING: rep {rep} slot {slot} {m} {p}: {txt}", flush=True)
                if tail[3] > STEAL_WARN_MEAN or tail[4] > STEAL_WARN_MAX:
                    txt = (f"steal mean {tail[3]:.2f}% / max {tail[4]:.2f}% above {STEAL_WARN_MEAN:g}% / "
                           f"{STEAL_WARN_MAX:g}%")
                    warnings.append((rep, slot, m, p, txt))
                    print(f"WARNING: rep {rep} slot {slot} {m} {p}: {txt}", flush=True)
                bad = [g for g, x in zip(FLOODERS, fl[0::2]) if math.isnan(x)] if m != "alone" else []
                if bad:
                    txt = f"iperf3 report missing or unparsable for flooder(s) {', '.join(bad)} (eBPF columns unaffected)"
                    warnings.append((rep, slot, m, p, txt))
                    print(f"WARNING: rep {rep} slot {slot} {m} {p}: {txt}", flush=True)
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
             + ["jain", "cpu_mean", "cpu_max", "steal_mean", "steal_max", "tick_mean", "tick_max",
                "fl_total", "min_own", "min_ovl", "ceil_sh", "floor_sh"] + [f"hc{g}" for g in FLOODERS]
             + [f"off{g}" for g in FLOODERS] + ["rtt_p50", "rtt_p99", "loss_pct", "rtt_ok", "okw_rtt"])
    print("\nREFERENCES per repeat: " + ", ".join(
        f"rep {i + 1} = " + "/".join(f"{v} {r[v]:.3f}" for v in VICTIMS) + f" Gbit/s, RTT p50/p99 "
        f"{q['p50']:.3f}/{q['p99']:.3f} ms (base_cpu {b:.1f}%; " + headroom(b) + ")"
        for i, (r, b, q) in enumerate(ref_hist)))
    print(f"WARNINGS (mean tick > {TICK_WARN_S:g} s, steal mean > {STEAL_WARN_MEAN:g}% or max > "
          f"{STEAL_WARN_MAX:g}%, missing flooder iperf3 report): {len(warnings)}"
          + "".join(f"\n  rep {a} slot {b} {c} {d}: {e}" for a, b, c, d, e in warnings))
    print(f"\nALL COLUMNS (mean +- sd over {REPS} repeats; alone pooled over all {len(configs) + 1} slots per repeat)")
    print("mode                   param    n " + " ".join(f"{n:>15s}" for n in names))
    for c in configs + [ALONE]:
        cols = [ms([r[j] for r in res[c]]) for j in range(len(names))]
        print(f"{c[0]:7s} {c[1]:>20s} {len(res[c]):4d} " + " ".join(f"{a:6.2f} +- {b:5.2f}" for a, b in cols))
    print("\n" + summarize(CSV), flush=True)

if __name__ == "__main__":
    if sys.argv[1:2] == ["--summary"]:
        print(summarize(sys.argv[2] if len(sys.argv) > 2 else CSV))
    else:
        main()
