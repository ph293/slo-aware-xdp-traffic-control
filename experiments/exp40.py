import json, subprocess, time, struct, random, statistics as st, sys, os, math, tempfile, csv as csvmod
REPS = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 6
T, DT = 40, 0.25
OFFSET = 0.5            # victims start 0.5 s after the flooders are launched (as exp5)
# exp40: sensitivity study of vfrot on the 6 vCPU host (same scenario, victims, flooders, reference, SLO, slot
# interleave, seed and rests as exp39) plus vffair, a fairness-aware vfrot variant (per-flooder floor cap).
# Only the modes vfrot, vffair and alone are kept (static and lqr dropped, as exp38 dropped the unused exp37 modes).
# Config 1 (vfrot 0.8_1_2_0.94) runs exactly exp39's vfrot code path (checked in test_exp40.py).
VICTIMS = ["V1", "V2"]                  # two victims, never limited (counter only); keyed by this list
FLOODERS = ["A", "B", "C", "D"]         # every per-flooder structure below is keyed by this list
PAR = {"A": 2, "B": 2, "C": 2, "D": 2}  # iperf3 -P streams per flooder
STEADY = {                              # steady scenarios: flooder -> (start s, duration s), t rel. flooder launch
    "steady4": {"A": (0, 28), "B": (6, 28), "C": (12, 28), "D": (18, 22)},  # all four active during 18-28 s
}
SCENARIO = "steady4"    # the only scenario (continuous flooders, no bursts)
NV, NF = len(VICTIMS), len(FLOODERS)
CAP_MIN, CAP_MAX = 200.0, 70000.0
XDP_BURST_NS = 5000000  # token-bucket burst of xdp_limit.o (BURST_NS in xdp_limit.c, 5 ms at the cap rate),
                        # the same object and burst for every mode; only logged here, the program owns it
V_RATE = 9.0            # offered rate of EACH victim, Gbit/s, one stream, constant over [0, T) (as exp34)
SLO_FRAC = 0.95         # slo_ok threshold (all modes): SLO_FRAC * that victim's own per-repeat reference
SMOOTH = 1.0            # s, victim rate window (1 s) for the vfrot feedback and the logged 1 s rates
DFS_ACTIVE_MBIT = 50.0  # vfrot/vffair: a flooder whose offered rate this tick is below this is inactive (gets CAP_MIN)
VFROT = {}               # (mode, param) -> (beta, add Gbit/s per s, rotation period s, thresh, bmax Mbit/s, floor_frac)
VFROT_VS_TAU = 0.5       # s, vfrot: EMA time constant of each victim's smoothed 1 s rate v
VFROT_THRESH = 0.98      # vfrot: default violation threshold (3-part param)
VFROT_HOLD = 1.0         # s, vfrot: no budget increase for this long after a violation tick
VFROT_CUT_HOLD = 1.0     # s, vfrot: no further cut for this long after a cut (the 1 s victim window lags recovery)
VF_B0, VF_BMAX = 10000.0, 16000.0   # vfrot: budget start and DEFAULT upper clamp (exp39), Mbit/s; lower clamp NF * CAP_MIN
VF_BMAX_RANGE = (4000.0, 30000.0)   # exp40: allowed range of a B<mbit> param suffix
HIGH_CAP_FRAC = 0.5      # exp40: a flooder holds a "high cap" on a tick when its cap >= this x budget
TICK_WARN_S = 0.30       # s, a slot whose mean control tick is longer than this gets a WARNING line in the log
STEAL_WARN_MEAN, STEAL_WARN_MAX = 1.0, 5.0   # exp40: %, a slot with mean steal > 1 % or max steal > 5 % gets a WARNING
REF_RUNS, REF_T = 2, 10   # both victims together, no flooders, measured at the start of EVERY repeat
PAUSE = 8
COOLDOWN = 45           # s, extra cool-down pause between repeats (thermal), on top of PAUSE
SESSION_REPEATS = 1     # repeats per session; a long rest follows each session except the last
SESSION_REST_S = 600    # s, thermal rest between sessions, on top of COOLDOWN
SLOT_S_MEASURED = 48.8  # s, exp39's measured time per slot (672 s per repeat for 13 slots + reference)
REF_S_MEASURED = 38.0   # s, reference block per repeat (2 x (10 s + ~1 s + PAUSE))
HOST = "10.10.0.1"
GUESTS = {"V1": ("tenant1", "10.10.0.11", 5201),  # victim 1
          "V2": ("tenant5", "10.10.0.15", 5205),  # victim 2
          "A": ("tenant2", "10.10.0.12", 5202),   # flooder A
          "B": ("tenant3", "10.10.0.13", 5203),   # flooder B
          "C": ("tenant4", "10.10.0.14", 5204),   # flooder C
          "D": ("tenant6", "10.10.0.16", 5206)}   # flooder D
MAKE_SCRIPT = {"tenant5": "make_tenant5.sh", "tenant6": "make_tenant6.sh"}
HERE = os.path.dirname(os.path.abspath(__file__))
OBJ = os.path.join(HERE, "xdp_limit.o")
CSV = os.path.join(HERE, "results_dyn40.csv")
TS_CSV = os.path.join(HERE, "ts_dyn40.csv")
REF_FILE = os.path.join(HERE, "ref_victim40.txt")
LOG = os.path.join(HERE, "exp40.log")   # written by run40.sh (stdout redirect), checked in main()
# exp40: 8 configs -> 17 slots/repeat (alone interleaved as exp39); order of definition, shuffled per repeat in main().
# vfrot param BETA_ADD_T_THRESH[_B<bmax>]; vffair param BETA_ADD_T_THRESH_F<floor_frac>. CONFIGS[0] is the reference
# of the paired comparison in the summary.
CONFIGS = [("vfrot", "0.8_1_2_0.94"),          # 1 reference, identical to exp39 (BMAX 16000)
           ("vfrot", "0.8_1_2_0.94_B12000"),   # 2 BMAX 12000
           ("vfrot", "0.8_1_2_0.94_B20000"),   # 3 BMAX 20000
           ("vfrot", "0.9_1_2_0.94"),          # 4 BETA 0.9
           ("vfrot", "0.8_2_2_0.94"),          # 5 ADD 2 Gbit/s per s (500 Mbit per 0.25 s tick instead of 250)
           ("vfrot", "0.8_1_1_0.94"),          # 6 rotation T = 1 s
           ("vffair", "0.8_1_2_0.94_F0.5"),    # 7 vffair, floor 0.5 of an equal share
           ("vffair", "0.8_1_2_0.94_F0.25")]   # 8 vffair, floor 0.25 of an equal share
NPROC_REQUIRED = 6      # main() refuses to start unless `nproc` prints exactly this
MODES = ("vfrot", "vffair")
RESULT_NAMES = ([n for v in VICTIMS for n in (f"victim{v[1:]}_mean_gbps", f"victim{v[1:]}_p5_gbps", f"slo_ok_v{v[1:]}")]
                + ["slo_ok_worst"]
                + [n for g in FLOODERS for n in (f"flooder{g}_iperf_gbps", f"flooder{g}_ebpf_gbps")]
                + ["jain_fairness", "cpu_mean", "cpu_max", "steal_mean", "steal_max", "tick_mean_s", "tick_max_s"])
EXTRA_NAMES = (["flooder_total_gbps", "min_flooder_own_gbps", "min_flooder_ovl_gbps", "budget_ceiling_share",
                "budget_floor_share"] + [f"highcap{g}_share" for g in FLOODERS])   # exp40: appended after the refs
# result tuple layout of run(): 3 per victim, slo_ok_worst, 2 per flooder, 7 tail values, then EXTRA_NAMES
R_OKW = 3 * NV
R_FL = R_OKW + 1
R_TAIL = R_FL + 2 * NF   # jain, cpu_mean, cpu_max, steal_mean, steal_max, tick_mean, tick_max
R_X = R_TAIL + 7         # exp40 extras
RESULT_NAMES = RESULT_NAMES + EXTRA_NAMES
HEADER = ("rep,slot,mode,param," + ",".join(RESULT_NAMES[:R_X]) + ","
          + ",".join(f"ref_v{v[1:]}_gbps" for v in VICTIMS) + "," + ",".join(EXTRA_NAMES))
TS_HEADER = ("rep,slot,mode,param,t,tick_s,sample_s,cpu,steal,"
             + "".join(f"v{v[1:]}_gbps_1s," for v in VICTIMS) + "".join(f"v{v[1:]}_gbps_tick," for v in VICTIMS)
             + "".join(f"cap{g}_mbit," for g in FLOODERS) + "".join(f"rate{g}_gbps," for g in FLOODERS)
             + "budget_mbit,worst_ratio")
# ts row tuple (without rep,slot,mode,param): t, tick, sample, cpu, steal, 1 s per victim, tick per victim,
# cap per flooder, rate per flooder, budget, worst
TS_CAP = 5 + 2 * NV
TS_RATE = TS_CAP + NF
# summary metrics: (CSV column, short name, True = higher is better / None = no winner)
SUMMARY_METRICS = [("slo_ok_worst", "ok_worst", True), ("flooder_total_gbps", "fl_total", True),
                   ("min_flooder_own_gbps", "min_own", True), ("min_flooder_ovl_gbps", "min_ovl", True),
                   ("jain_fairness", "jain", True), ("budget_ceiling_share", "ceil_sh", None)]

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
    # exp37, steal = 8th field (None if the kernel does not report it)
    f = list(map(int, open("/proc/stat").readline().split()[1:]))
    return sum(f), f[3] + f[4], (f[7] if len(f) > 7 else None)

def host_cpus():
    # (nproc, os.cpu_count()); nproc = CPUs usable by this process, None if nproc fails
    try:
        n = int(subprocess.run(["nproc"], capture_output=True, text=True, check=True).stdout.strip())
    except (OSError, ValueError, subprocess.CalledProcessError):
        n = None
    return n, os.cpu_count()

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

def parse_vfrot(mode, param):
    # exp40: "BETA_ADD_T[_THRESH][_B<bmax>]" for vfrot, "BETA_ADD_T[_THRESH]_F<floor_frac>" for vffair, e.g.
    # "0.8_1_2_0.94" (exp39), "0.8_1_2_0.94_B12000", "0.8_1_2_0.94_F0.5"; BETA cut factor, ADD additive increase
    # Gbit/s per s, T rotation period s, THRESH violation threshold (default VFROT_THRESH), BMAX budget ceiling
    # Mbit/s (default VF_BMAX), F floor = F x budget / n_active per active non-holder (vffair only, required)
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
    # vffair: every active non-holder gets fair_floor, the holder the rest of the budget; only ACTIVE flooders
    # count (inactive ones keep CAP_MIN outside the budget, exactly as vfrot); a single active flooder gets
    # budget - CAP_MIN as in vfrot. With fair_floor == CAP_MIN this is exactly vfrot's allocation. The active caps
    # sum to the budget: n_act x floor = max(n_act x CAP_MIN, floor_frac x budget) <= budget (budget >= NF x CAP_MIN)
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
    # rot_t s (re-decision instants every rot_t s, so a voluntary hand-over is never faster than
    # rot_t); with no flooder active, keep the last caps. exp40: alloc(act, budget, holder), if given,
    # replaces the high_cap / CAP_MIN allocation (vffair); the holder decision is the same
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

def budget_shares(ts, bmax):
    # exp40: share of ALL ticks of the slot with the budget at the ceiling bmax / at the lower clamp NF x CAP_MIN
    # (same definition as the 62 % ceiling share of exp39 vfrot)
    if not ts:
        return float("nan"), float("nan")
    b = [row[-2] for row in ts]
    return (sum(x >= bmax - 1e-6 for x in b) / len(b), sum(x <= NF * CAP_MIN + 1e-6 for x in b) / len(b))

def highcap_shares(ts):
    # exp40: per flooder, share of the ticks in the all-active window with cap >= HIGH_CAP_FRAC x budget
    ov = overlap()
    rows = [row for row in ts if ov[0] <= row[0] < ov[1]]
    if not rows:
        return [float("nan")] * NF
    return [sum(row[TS_CAP + i] >= HIGH_CAP_FRAC * row[-2] for row in rows) / len(rows) for i in range(NF)]

def holder_util(ts, bmax):
    # exp40 (log only): in the all-active window, ticks with the budget at the ceiling; there the holder (largest
    # cap) delivered this fraction of its cap on average; and the largest holder tick rate (Gbit/s)
    ov = overlap()
    u, r = [], []
    for row in ts:
        if ov[0] <= row[0] < ov[1] and row[-2] >= bmax - 1e-6:
            caps = row[TS_CAP:TS_CAP + NF]
            i = max(range(NF), key=lambda j: caps[j])
            u.append(row[TS_RATE + i] * 1000.0 / caps[i]); r.append(row[TS_RATE + i])
    return len(u), (st.mean(u) if u else float("nan")), (max(r) if r else float("nan"))

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
    # one iperf3 client per victim (one stream, V_RATE), all launched together
    return {v: iperf_cmd(v, secs, rate_gbit=V_RATE) for v in VICTIMS}

def flooder_cmds():
    # one delayed (sleep) iperf3 client per flooder, as exp37 steady scenarios
    return {g: iperf_cmd(g, STEADY[SCENARIO][g][1], PAR[g], STEADY[SCENARIO][g][0]) for g in FLOODERS}

# ---------- setup ----------

def check_servers():
    # the host iperf3 servers are started by hand; abort if any of them is not listening
    # ports 5201 (victim 1), 5202-5204 (flooders A-C), 5205 (victim 2), 5206 (flooder D)
    out = subprocess.run(["ss", "-ltnH"], capture_output=True, text=True).stdout
    ports = sorted(GUESTS[g][2] for g in GUESTS)
    missing = [p for p in ports
               if not any(l.split()[3].endswith(f":{p}") for l in out.splitlines() if len(l.split()) > 3)]
    if missing:
        raise SystemExit("no iperf3 server listening on host port(s) " + ", ".join(map(str, missing))
                         + "; start them first: " + "; ".join(f"iperf3 -s -D -p {p}" for p in missing))
    print("iperf3 servers listening on ports " + ", ".join(map(str, ports)), flush=True)

def check_guests():
    # every guest must answer ssh non-interactively and have iperf3
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
    # both victims together (same conditions as the alone slots), flooder caps unlimited, no flooders;
    # each victim's reference = median of its own eBPF bytes on its own vnet over REF_RUNS x REF_T s;
    # base_cpu = mean host CPU during these runs (logged only)
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
    # refs = {victim: its reference Gbit/s}; base_cpu only logged
    if mode in MODES:
        # budget B starts at VF_B0, allocated by rotation; first flooder on (vfrot and vffair alike)
        caps = [high_cap(VF_B0, 1) if g == FLOODERS[0] else CAP_MIN for g in FLOODERS]
    else:
        caps = [CAP_MAX] * NF
    budget = None
    v_s = {v: None for v in VICTIMS}        # vfrot: EMA-smoothed 1 s rate per victim, Mbit/s
    delivered = {g: 0.0 for g in FLOODERS}  # vfrot: passed (delivered) bytes per flooder, current overlap window
    pf_all = False                          # vfrot: were all flooders active on the previous tick
    pf_state = {"holder": None, "since": None}  # vfrot: high-cap holder, when decided
    vf_block = float("-inf")                # vfrot: budget increases blocked while tnow < vf_block
    vf_cutblock = float("-inf")             # vfrot: budget cuts blocked while tnow < vf_cutblock
    # caps first, then the eBPF baseline, then the flooders: no flooder byte can precede the baseline
    for g, c in zip(FLOODERS, caps): set_cap(IF[g], c)
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
    ts = []                      # one row per tick for ts_dyn40.csv
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
        if mode in MODES:
            # exp37 vfrot (CPU only logged): B x beta on a violation (then no further cut for VFROT_CUT_HOLD s,
            # no increase for VFROT_HOLD s), else B += add per s; B clamped [NF*CAP_MIN, bmax]; the high cap
            # rotates (every rot_t s) to the active flooder with the lowest delivered bytes since all were active.
            # One EMA (tau VFROT_VS_TAU) of the 1 s eBPF rate PER VICTIM; the feedback is the WORST victim,
            # worst = min over victims of v_s / its own reference; violation = worst < thresh. B holds until both
            # victims' 1 s windows lie entirely inside their runs; both EMAs start there.
            # exp40: bmax per config (default VF_BMAX = exp39); vffair differs only in the allocation (fair_alloc)
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
            for g, c in zip(FLOODERS, caps): set_cap(IF[g], c)
        # t relative to flooder launch; caps as set at this tick; rates over this tick
        ts.append((tnow - t0, dt, samp, busy, steal,
                   *(rate_mbit(hist[v], tnow - SMOOTH, tnow) / 1000.0 for v in VICTIMS),
                   *(tick[v] / 1000.0 for v in VICTIMS),
                   *caps, *(tick[g] / 1000.0 for g in FLOODERS),
                   budget if mode in MODES else float("nan"), worst))
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
    # exp40 extras: flooder total = sum of the eBPF means; min flooder over each flooder's own window (eBPF means)
    # and over the all-active window; budget ceiling/floor shares; per-flooder high-cap shares
    nan = float("nan")
    if alone:
        extras = (0.0, nan, nan, nan, nan) + (nan,) * NF
    else:
        own = per[1::2]
        ceil_sh, floor_sh = budget_shares(ts, vf_params(mode, param)[4]) if mode in MODES else (nan, nan)
        extras = (sum(own), min(own), min(rates) / 1000.0, ceil_sh, floor_sh,
                  *(highcap_shares(ts) if mode in MODES else (nan,) * NF))
    return (*vres, ok_worst, *per, jf,
            nanstat(cpus, st.mean), nanstat(cpus, max), nanstat(steals, st.mean), nanstat(steals, max),
            nanstat(tk, st.mean), nanstat(tk, max), *extras), ts

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
    # exp40: the same at exp39's measured rate (SLOT_S_MEASURED per slot, REF_S_MEASURED per reference)
    n_slots = 2 * n_configs + 1
    per_rep = REF_S_MEASURED + n_slots * SLOT_S_MEASURED
    return n_slots, per_rep, reps * per_rep + (reps - 1) * SESSION_REST_S

def csv_line(rep, slot, m, p, r, refs):
    vr, okw, fl, tail, ex = r[:R_OKW], r[R_OKW], r[R_FL:R_TAIL], r[R_TAIL:R_X], r[R_X:]
    return (f"{rep},{slot},{m},{p}," + "".join(f"{x:.3f}," for x in vr) + f"{okw:.3f},"
            + "".join(f"{x:.3f}," for x in fl)
            + f"{tail[0]:.4f}," + "".join(f"{x:.2f}," for x in tail[1:5])
            + "".join(f"{x:.4f}," for x in tail[5:7])
            + "".join(f"{refs[v]:.3f}," for v in VICTIMS)
            + ",".join([f"{x:.3f}" for x in ex[:3]] + [f"{x:.4f}" for x in ex[3:]]) + "\n")

# ---------- summary (also offline: python3 exp40.py --summary [results_dyn40.csv]) ----------

def summary_data(path):
    # per config (alone excluded): {metric: {rep: value}}; configs in CONFIGS order, then any others in file order
    vals = {}
    with open(path) as f:
        for row in csvmod.DictReader(f):
            c = (row["mode"], row["param"])
            if c[0] == "alone":
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
                w = sum(d > 1e-9 for d in ds); l = sum(d < -1e-9 for d in ds)
                diffs[c][m] = (*ms(ds), len(ds), (w, len(ds) - w - l, l) if better else None)
    return order, stats, diffs

def summarize(path):
    order, stats, diffs = summary_data(path)
    ref = CONFIGS[0]
    out = [f"SUMMARY40 from {os.path.basename(path)} (mean +- sd over repeats; alone excluded)",
           f"{'mode':7s} {'param':>20s} {'n':>3s} " + " ".join(f"{s:>15s}" for _, s, _ in SUMMARY_METRICS)]
    for c in order:
        n = max(v[2] for v in stats[c].values())
        out.append(f"{c[0]:7s} {c[1]:>20s} {n:3d} "
                   + " ".join(f"{stats[c][m][0]:6.3f} +- {stats[c][m][1]:5.3f}" for m, _, _ in SUMMARY_METRICS))
    out.append(f"PAIRED vs {ref[0]} {ref[1]} (per repeat: config - reference, mean +- sd; W/T/L = higher/equal/lower "
               f"than the reference in the same repeat, higher is better; ceiling share: difference only)")
    out.append(f"{'mode':7s} {'param':>20s} {'n':>3s} " + " ".join(f"{s:>23s}" for _, s, _ in SUMMARY_METRICS))
    for c, d in diffs.items():
        n = max(v[2] for v in d.values())
        out.append(f"{c[0]:7s} {c[1]:>20s} {n:3d} " + " ".join(
            f"{d[m][0]:+7.3f} +- {d[m][1]:5.3f} " + (f"{d[m][3][0]}/{d[m][3][1]}/{d[m][3][2]}" if d[m][3] else "-")
            .rjust(5) for m, _, _ in SUMMARY_METRICS))
    if not diffs:
        out.append(f"(reference config {ref[0]} {ref[1]} not in the file: no paired differences)")
    return "\n".join(out)

def setup_configs(configs):
    # validate every config and fill VFROT; labels (mode, param) must be unique
    if len(set(configs)) != len(configs):
        raise SystemExit(f"duplicate config label(s) in {configs}")
    for m, p in configs:
        if m not in MODES:
            raise SystemExit(f"mode {m!r} is not supported by exp40 (only {', '.join(MODES)})")
        VFROT[(m, p)] = parse_vfrot(m, p)

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
    setup_configs(configs)
    ALONE = ("alone", "na")   # victim-only control, run before the first config and after every config
    res = {c: [] for c in configs + [ALONE]}
    # never append to or overwrite earlier output: refuse to start if any output file already exists
    # (exp40.log is allowed only when it is this process's own stdout, i.e. just created by run40.sh)
    try:
        own_log = os.path.exists(LOG) and os.path.samestat(os.fstat(sys.stdout.fileno()), os.stat(LOG))
    except (OSError, ValueError, AttributeError):
        own_log = False
    existing = [f for f in (CSV, TS_CSV, REF_FILE) if os.path.exists(f)]
    if os.path.exists(LOG) and not own_log:
        existing.append(LOG)
    if existing:
        raise SystemExit("REFUSING TO START: output file(s) already exist: " + ", ".join(existing)
                         + " -- move or delete them first (exp40 never appends)")
    nproc, ncpu = host_cpus()
    if nproc != NPROC_REQUIRED:
        raise SystemExit(f"REFUSING TO START: nproc = {nproc} (os.cpu_count() = {ncpu}), exp40 requires exactly "
                         f"{NPROC_REQUIRED}: it continues exp39 on the 6 vCPU host VM and is only comparable there")
    try:
        sudo(["true"])
    except subprocess.CalledProcessError:
        raise SystemExit("sudo needs a password: run 'sudo -v' first (run40.sh does this)")
    if not os.path.exists(OBJ):
        raise SystemExit(f"{OBJ} not found")
    check_servers()
    check_guests()
    w = STEADY[SCENARIO]
    print(f"SCENARIO {SCENARIO}: " + ", ".join(
              f"flooder {g} ({GUESTS[g][0]}) t={w[g][0]}-{sum(w[g])} s ({PAR[g]} streams)" for g in FLOODERS)
          + f", all active {overlap()[0]:g}-{overlap()[1]:g} s (Jain window), no bursts", flush=True)
    print("CONFIGS: " + ", ".join(
        f"{m} {p} (beta {b:g}, add {a:g} Gbit/s per s, T {r:g} s, thresh {th:.2f}, BMAX {bm:g}"
        + (f", floor {ff:g} x B / n_active" if ff is not None else "") + ")"
        for m, p in configs for b, a, r, th, bm, ff in [VFROT[(m, p)]]), flush=True)
    print(f"REFERENCE CONFIG for the paired summary: {configs[0][0]} {configs[0][1]} (identical to exp39 vfrot)",
          flush=True)
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
          f"budget start {VF_B0:g}, clamp [{NF * CAP_MIN:g}, BMAX] Mbit/s (BMAX {VF_BMAX:g} unless a B suffix), "
          f"rotation over {NF} holders (holder gets B - (n_active - 1) x {CAP_MIN:g})", flush=True)
    print(f"vffair: as vfrot (same budget law, same holder = lowest delivered since all active, same rotation), but "
          f"every ACTIVE non-holder gets floor = max({CAP_MIN:g}, F x B / n_active) and the holder B - (n_active - 1) "
          f"x floor (one active: B - {CAP_MIN:g}); inactive flooders keep {CAP_MIN:g} outside the budget, as vfrot",
          flush=True)
    print(f"METRICS: flooder_total = sum of the eBPF means; min_flooder_own = smallest eBPF mean over each flooder's "
          f"own window; min_flooder_ovl = smallest rate over the all-active window {overlap()[0]:g}-{overlap()[1]:g} s; "
          f"budget_ceiling/floor_share = share of all ticks of the slot with budget >= BMAX / <= {NF * CAP_MIN:g}; "
          f"highcapX_share = share of ticks in {overlap()[0]:g}-{overlap()[1]:g} s with capX >= {HIGH_CAP_FRAC:g} x "
          f"budget; Jain over the eBPF rates in the all-active window (as exp39)", flush=True)
    c = cpu()
    print(f"HOST CPU: /proc/stat aggregate line, busy % = 1 - (idle + iowait)/total as exp37 (total = sum of all "
          f"fields); steal % = steal field / same total ({'reported' if c[2] is not None else 'NOT reported'} by "
          f"this kernel); host CPUs: nproc {nproc}, os.cpu_count() {ncpu}; tick length = time between consecutive reads of "
          f"{GUESTS[VICTIMS[0]][0]}'s counter (nominal {DT:g} s), WARNING if a slot's mean > {TICK_WARN_S:g} s "
          f"(first tick, which spans the launch, excluded) or its steal mean > {STEAL_WARN_MEAN:g}% or max > "
          f"{STEAL_WARN_MAX:g}%", flush=True)
    n_slots, per_rep, total = expected_duration(len(configs), REPS)
    _, per_rep_m, total_m = measured_duration(len(configs), REPS)
    print(f"EXPECTED DURATION: {n_slots} slots/repeat, ~{per_rep / 60:.1f} min/repeat (exp39 measured rate "
          f"~{per_rep_m / 60:.1f}), {REPS} repeats + {REPS - 1} x {SESSION_REST_S} s rest ~ {total / 3600:.2f} h "
          f"(measured rate ~{total_m / 3600:.2f} h)", flush=True)
    procs = []
    ref_hist = []     # references {victim: ref} used in each repeat
    base_cpus = []    # measured victims-alone host CPU mean in each repeat
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
            refs, base_cpu = reference(rep, procs); ref_hist.append(refs); base_cpus.append(base_cpu)
            print(f"REFERENCE rep {rep}: " + ", ".join(
                      f"{v} ({GUESTS[v][0]}) {refs[v]:.3f} Gbit/s (slo_ok threshold {SLO_FRAC * refs[v]:.3f})"
                      for v in VICTIMS)
                  + f" (median of {REF_RUNS} x {REF_T} s, both together, eBPF), base_cpu {base_cpu:.1f}%, saved to "
                  f"{REF_FILE}", flush=True)
            print("rep slot mode                  param "
                  + "".join(f"{v}_mean {v}_p5 {v}_ok " for v in VICTIMS) + "ok_worst "
                  + "".join(f"{g}_iperf {g}_ebpf " for g in FLOODERS)
                  + " jain cpu_mean cpu_max steal_m steal_x tick_m tick_x  fl_tot min_own min_ovl ceil_sh floor_sh "
                  + " ".join(f"hc{g}" for g in FLOODERS), flush=True)
            order = configs[:]; random.shuffle(order)
            seq = [ALONE] + [x for c in order for x in (c, ALONE)]   # alone, c1, alone, ..., cN, alone
            for slot, (m, p) in enumerate(seq):
                r, ts = run(m, p, refs, base_cpu, procs); res[(m, p)].append(r)
                vr, okw, fl, tail, ex = r[:R_OKW], r[R_OKW], r[R_FL:R_TAIL], r[R_TAIL:R_X], r[R_X:]
                csv.write(csv_line(rep, slot, m, p, r, refs)); csv.flush()
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
                print(f"{rep:3d} {slot:4d} {m:7s} {p:>20s} "
                      + "".join(f"{a:7.2f} {b:5.2f} {c:5.2f} " for a, b, c in zip(vr[0::3], vr[1::3], vr[2::3]))
                      + f"{okw:8.2f} " + "".join(f"{a:7.2f} {b:6.2f} " for a, b in zip(fl[0::2], fl[1::2]))
                      + f"{tail[0]:5.3f} {tail[1]:8.1f} {tail[2]:7.1f} {tail[3]:7.2f} {tail[4]:7.2f} "
                      f"{tail[5]:6.3f} {tail[6]:6.3f} {ex[0]:7.2f} {ex[1]:7.2f} {ex[2]:7.2f} {ex[3]:7.3f} {ex[4]:8.3f} "
                      + " ".join(f"{x:4.2f}" for x in ex[5:]), flush=True)
                if m in MODES:
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
                "fl_total", "min_own", "min_ovl", "ceil_sh", "floor_sh"] + [f"hc{g}" for g in FLOODERS])
    print("\nREFERENCES per repeat (Gbit/s): " + ", ".join(
        f"rep {i + 1} = " + "/".join(f"{v} {r[v]:.3f}" for v in VICTIMS) + f" (base_cpu {b:.1f}%)"
        for i, (r, b) in enumerate(zip(ref_hist, base_cpus))))
    print(f"WARNINGS (mean tick > {TICK_WARN_S:g} s, steal mean > {STEAL_WARN_MEAN:g}% or max > "
          f"{STEAL_WARN_MAX:g}%): {len(warnings)}" + "".join(f"\n  rep {a} slot {b} {c} {d}: {e}"
                                                            for a, b, c, d, e in warnings))
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
