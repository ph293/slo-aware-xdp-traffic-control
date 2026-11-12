import json, subprocess, time, struct, random, statistics as st, sys, os, math, tempfile, contextlib, csv as csvmod, signal
import re, bisect
import numpy as np
REPS = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 6
T, DT = 40, 0.25
OFFSET = 0.5            # victims start 0.5 s after the flooders are launched (as exp5)
# exp45: exp44 found that lqr_orig (fixed per-tenant CPU target) is as good as or better than vfrot in S1/S2 but
# collapses in S3 (SLO 0.39 / 0.32 vs vfrot 0.93, 6/6 repeats worse): its fixed target throttles the legitimate
# victims. exp45 adds controller "hyb": lqr_orig per tenant EXACTLY as ported (paper K, dt 0.5 s, same filter, soft
# limit, quantiser, xdp_dropfrac actuator, same tenants: S1 A, S2 A+B, S3 all six) whose per-tenant CPU targets are
# moved by a slow outer victim-feedback loop (hyb_step(); runs on every lqr_orig step while t < OFFSET + T):
#   the loop sees per tenant only: its SLO reference (refs[v] for a tenant with an SLO, 0 = best effort), its
#   delivered (XDP-passed) rate as vfrot's smoothed 1 s signal, the drop fraction WE applied to it, its CPU_eff.
#   ratio = smoothed rate / reference (+inf without an SLO); hurt = ratio < HYB_THRESH.
#   (a) hurt AND we dropped on it within the last HYB_T s -> our own throttling: its target x 1 / HYB_BETA, clamp CEIL
#   (b) hurt AND no drop of ours on it for >= HYB_T s (or not ours to drop) -> others hurt it: every DONOR's target
#       x HYB_BETA, clamp FLOOR, at most once per HYB_CUT_HOLD s; donor = controlled tenant with ratio > 1 / HYB_THRESH
#       (every best-effort tenant) whose target is not above its base
#   (c) otherwise hold; HYB_T s after its last change (and, upwards, HYB_T s after the last (b) condition) a target
#       moves back toward its base by HYB_ADD_FRAC x cpu_ref per s; a raised target never below min(CEIL, CPU_eff +
#       GUARD) (it only comes down once the tenant no longer needs it)
#   The loop never reads the FLOODERS / VICTIMS lists: only the reference table (test_exp45.py relabels tenants).
# WHY THE CEILING IS 6 x cpu_ref (~247 % of one core, i.e. above anything a vhost thread can use): exp44's ts data show
#   that lqr_orig with the paper K is a period-2 relay, not a target tracker: in every S3 slot both victims' u was
#   0.95, 0, 0.95, 0 ... on 100 % of controller steps (CPU_eff swinging -12 / +56 %, edot ~136 %/s), the flooders on
#   69-92 % of steps in S1-S3. With u = 0.2562 e + 0.3031 edot the up-swing gives u = 0.95 unless e < ~ -160, i.e.
#   target > ~217 %. A ceiling of 1.5 x cpu_ref (first design) leaves the victims in the cycle (simulated SLO 0.00);
#   6 x cpu_ref lets rule (a) take a victim out of it. EXEMPT flag (ts ex<g>, column exempt<g>_s = first time):
#   hyb has raised g's target above its base AND none of our drops has hit g for HYB_T s -- hyb has in effect LEARNED
#   TO EXEMPT g from lqr_orig. The cycle can break below the ceiling (the simulated 36-40 % victim leaves it at ~143 %),
#   so reaching the ceiling is logged separately (ceil<g>_s); a tenant at the ceiling but not exempt is still in the
#   flip-flop (the failure mode: its swing needs more than the ceiling).
# RULE (b) IS EXPECTED TO BE NEARLY A NO-OP: on the low half of the cycle CPU_eff goes negative, so u = 0 on every
#   second step whatever the (non-negative) target; lowering a flooder's target cannot raise its duty cycle (exp44:
#   T40 vs T60 flooder totals 59.7 vs 60.1 Gbit/s in S3). (b) is kept and logged (cuts, hyb_min_be_tgt), not tuned.
# ubelow<g> (ts) / ubelow<g>_frac: controller steps with u > 0 while the CPU sample the step used (vhost over its
#   0.5 s window) is below the target the step used -- the flip-flop's signature (lqr_orig and hyb).
# Everything else is exp44: scenarios S1/S2/S3, calibration, actuator, metrics, paired summary, safety rules, fail-fast
# checks (incl. the S3 vhost check), K sign checks. Configs per scenario: vfrot 0.8_1_2_0.94, lqr_orig T40_paperK,
# lqr_orig T60_paperK, hyb T60_paperK (start target T60) + alone: 15 slots per repeat (exp44's lqr 85_4 is dropped
# from the plan; its code stays for the equivalence test). New columns: per-tenant mean hyb target, raise / cut counts,
# first raise, min best-effort target; ts: per-tenant target + hyb event bits (1 raise, 2 cut, 4 restore up, 8 down).
# exp44: where should a FIXED per-tenant CPU target (the authors' lqr_orig, paper K, T40 / T60 of calib42's cpu_ref)
# be weak? It is not SLO-aware (it never looks at the victims) and not work-conserving (a tenant is held at its
# target even when the host has headroom). exp43 showed it beating vfrot on flooder throughput and fairness in the
# steady 4-flooder case at equal SLO; exp44 runs three scenarios, same 40 s runs, seed 7, SLO rule, RTT SLO and dip
# metrics as exp43:
#   S1  one flooder (A, 0-40 s), V1 / V2 at 9 Gbit/s; lqr_orig on A only (as exp43: active flooders only)
#   S2  two flooders (A 0-40 s, B 6-34 s), V1 / V2 at 9 Gbit/s; lqr_orig on A and B only
#   S3  exp43's four flooders (steady4 windows), V1 at 9, V2 at V2_RATE_S3 (heavier victim); lqr_orig on ALL SIX
#       tenants (victims too, as the authors' design: no knowledge of who is a flooder); vfrot and lqr unchanged
# Controllers, calibration (calib42_result.json, no new calibration), metrics, safety rules: exp43's code. Changes:
#   * per-scenario flooder windows / victim rates; per-flooder columns of a flooder not active in the scenario are
#     nan, flooder_total / min_own / Jain are over the ACTIVE flooders (Jain nan with fewer than two)
#   * vhost-net CPU of all six tenants (same schedstat code path as exp43's flooders), logged for every mode and in
#     each reference; lqr_orig in S3 runs one controller instance per tenant on that same signal
#   * S3 lqr_orig: xdp_dropfrac on the 6 vnets; the victims' drop is released when the victim window ends
#     (t >= OFFSET + T, before their iperf3 -J report crosses the dropping vnet; column victim_release_s; all 39
#     SLO windows end before it, else column victim_win_after_release > 0 + WARNING); victims' wait has the
#     LQO_WAIT_S timeout + WARNING like the flooders'. Pings of V2 cross the dropping vnet: lost pings are a real
#     effect of the design (the management-port exemption stays disabled, as exp43)
#   * per repeat and scenario: own reference (2 x 20 s, both victims at the scenario's rates + V2 ping), then one
#     alone slot, then the scenario's configs shuffled (seed 7); scenario order rotated per repeat (rep 1 starts with
#     S3, whose reference refuses in rep 1 if V2's vhost CPU < S3_VH_MIN_FRAC x the T40 target or a victim reaches
#     < REF_RATE_MIN_FRAC of its offered rate: fail fast, before any slot and the first 600 s rest)
#   * summary per scenario, paired vs vfrot per scenario, table scenario x controller
# exp43 header (still valid for the controllers):
# Changes against exp42 (everything else is exp42's code):
#   * SIGN FIX of the re-identified lqr_orig gain: exp42 solved the ARE on the authors' FORWARD-EULER model
#     A = [[1, dt], [-dt/tau, 1 - dt/tau]], B = [[0], [-b dt]]. With the identified tau = 0.081 s and dt = 0.5 s
#     (dt/tau = 6.2) that model has a spurious eigenvalue -4.65, and the ARE gain stabilising it has the WRONG sign.
#     exp43 discretises the SAME continuous model exactly (zero-order hold) -> K = [-0.0133, -0.0012] on calib42
#     (param suffix _zoh). The paper K (param suffix _paperK) is unchanged. A plain "T<pct>" is refused, and so is
#     any lqr_orig gain with K[0] >= 0 (checked before anything runs).
# lqr_orig, as the authors have it (nothing tuned):
#   one controller per tenant it is applied to, dt = LQO_DT = 0.5 s (runs on every 2nd 0.25 s tick); CPU = that
#   tenant's vhost-net thread (% of one core, /proc/<qemu>/task/<vhost>/schedstat ns over the 0.5 s window);
#   CPU_smooth = median of the last 5 samples, trend = CPU_smooth(k) - CPU_smooth(k-1),
#   CPU_eff = 0.7 CPU_smooth + 0.3 (CPU_smooth + trend), e = CPU_eff - target, edot = (e(k) - e(k-1)) / dt;
#   u = -K [e, edot]; soft limit (margin 0.5 CPU points); clip [0, 0.95]; round to the nearest of 20 levels;
#   u is the ABSOLUTE drop fraction, actuated by xdp_dropfrac.o (deterministic per-packet drop).
#   Left out: the PPS feed-forward term of the authors' public v2 code; their management-port exemption.
# DOCUMENTED DEVIATION (lqr_orig only): when the victims' window ends, drop_bp is set to 0 on all controlled
#   tenants BEFORE waiting for the iperf3 clients, with a LQO_WAIT_S timeout and a WARNING (column u_release_s).
VICTIMS = ["V1", "V2"]                  # two victims, never rate-limited (counter only; S3 lqr_orig drops on them)
FLOODERS = ["A", "B", "C", "D"]         # every per-flooder structure below is keyed by this list
TENANTS = VICTIMS + FLOODERS            # exp44: vhost CPU of all six; S3 lqr_orig controls all six
PAR = {"A": 2, "B": 2, "C": 2, "D": 2}  # iperf3 -P streams per flooder
STEADY = {                              # exp43's windows: flooder -> (start s, duration s), t rel. flooder launch
    "steady4": {"A": (0, 28), "B": (6, 28), "C": (12, 28), "D": (18, 22)},  # all four active during 18-28 s
}
V_RATE = 9.0            # offered rate of each victim, Gbit/s, one stream, constant over [0, T) (as exp34-exp43)
V2_RATE_S3 = 10.0       # exp44 S3: V2's offered rate (see the exp44 report: est. vhost ~33-44 % of one core vs T40
                        # 16.4 %; host CPU under vfrot with 4 flooders was 89.2 % at 9 Gbit/s in exp43 18-28 s)
SCENARIOS = {           # win: active flooders -> (start, duration); lqo_victims: lqr_orig also on V1 and V2
    "S1": {"win": {"A": (0, 40)}, "v_rate": {"V1": V_RATE, "V2": V_RATE}, "lqo_victims": False},
    "S2": {"win": {"A": (0, 40), "B": (6, 34)}, "v_rate": {"V1": V_RATE, "V2": V_RATE}, "lqo_victims": False},
    "S3": {"win": dict(STEADY["steady4"]), "v_rate": {"V1": V_RATE, "V2": V2_RATE_S3}, "lqo_victims": True},
}
SCEN_ORDER = ("S3", "S1", "S2")   # repeat r runs this order rotated left by (r - 1) % 3: every scenario in every
                                  # position twice in R = 6, and rep 1 starts with S3 (its reference fails fast)
SCENARIO = "S3"         # the current scenario (set_scenario(); main() sets it before each scenario block)
S3_VH_MIN_FRAC = 1.15   # S3 reference: V2's vhost CPU must be >= this x the T40 target (rep 1 refuses, later WARNING)
VH_MIN = {"frac": 0.40} # target fraction the check refers to (T40); the % value is set from calib42 in main()
REF_RATE_MIN_FRAC = 0.97   # each reference: a victim below this x its offered rate refuses in rep 1, WARNING later
CPU_WARN = 90.0         # %, a slot with mean host CPU above this gets a WARNING line (S3 headroom)
NV, NF = len(VICTIMS), len(FLOODERS)
KP, KI = 200.0, 300.0
CAP_MIN, CAP_MAX = 200.0, 70000.0
INIT_CAP_MBIT = 2000.0  # lqr: initial shared budget (split equally)
XDP_BURST_NS = 5000000  # token-bucket burst of xdp_limit.o (BURST_NS in xdp_limit.c, 5 ms at the cap rate),
                        # used by every mode except lqr_orig; only logged here, the program owns it
K_CPU = 0.93            # % host CPU per Gbit/s (from the Step 19 measurement)
SLO_FRAC = 0.95         # slo_ok threshold (all modes): SLO_FRAC * that victim's own per-repeat reference
SMOOTH = 1.0            # s, victim rate window (1 s) for the vfrot feedback and the logged 1 s rates
DFS_ACTIVE_MBIT = 50.0  # vfrot: a flooder whose offered rate this tick is below this is inactive (gets CAP_MIN)
VFROT = {}               # (mode, param) -> (beta, add Gbit/s per s, rotation period s, thresh, bmax Mbit/s, floor_frac)
VFROT_VS_TAU = 0.5       # s, vfrot: EMA time constant of each victim's smoothed 1 s rate v
VFROT_THRESH = 0.98      # vfrot: default violation threshold (3-part param)
VFROT_HOLD = 1.0         # s, vfrot: no budget increase for this long after a violation tick
VFROT_CUT_HOLD = 1.0     # s, vfrot: no further cut for this long after a cut (the 1 s victim window lags recovery)
VF_B0, VF_BMAX = 10000.0, 16000.0   # vfrot: budget start and DEFAULT upper clamp (exp39), Mbit/s; lower clamp NF * CAP_MIN
VF_BMAX_RANGE = (4000.0, 40000.0)   # allowed range of a B<mbit> param suffix (as exp43)
HIGH_CAP_FRAC = 0.5      # a flooder holds a "high cap" on a tick when its cap >= this x budget
LQR = {}                 # lqr: param string -> (host CPU target %, R weight multiplier, gain K 1x2), set in main()
LQR_TAU = 0.5            # s, lqr plant time constant: y(k+1) = a*y(k) + b*u(k), a = exp(-DT/LQR_TAU),
                         # b = K_CPU*(1-a) (steady-state gain K_CPU)
LQR_Q = (1.0, 1.0)       # lqr: state weights Q = diag(q_e, q_d)
LQR_R = 0.5              # lqr: input weight on du (Gbit/s); R = LQR_R * RWT
# ---- lqr_orig (the authors' values; do NOT tune) ----
LQO = {}                 # lqr_orig: param -> (target % of one core, K (k_e, k_edot), target fraction, paper K?)
LQO_DT = 0.5             # s, their control period
LQO_EVERY = 2            # lqr_orig runs on every LQO_EVERY-th DT tick (LQO_EVERY * DT == LQO_DT)
LQO_MED_N = 5            # median window (samples)
LQO_TREND_W = 0.3        # CPU_eff = (1 - w) CPU_smooth + w (CPU_smooth + trend)
LQO_R = 0.01             # ARE input weight
LQO_Q = (100.0, 10.0)    # ARE state weights x alpha, alpha = LQO_ALPHA_NUM / max(target, 1)
LQO_ALPHA_NUM = 10.0
LQO_TAU_PAPER, LQO_B_PAPER = 2.0, 10.0
K_PAPER = (-0.2562, -0.3031)   # their K for dt = 0.5 s (reproduced by lqo_gain in test_exp43.py and test_exp44.py)
LQO_MARGIN = 0.5         # soft limit margin, CPU percentage points
LQO_HARD_EXP_OUT, LQO_HARD_EXP_IN = 0.7, 0.5   # soft limit exponents above / inside the margin
LQO_UMAX = 0.95
LQO_LEVELS_BP = tuple(500 * i for i in range(20))   # 0, 0.05, ..., 0.95 as basis points (20 levels)
LQO_WAIT_S = 60.0        # s, lqr_orig: timeout of the iperf3 wait after the release (deviation, see above)
TICK_WARN_S = 0.30       # s, a slot whose mean control tick is longer than this gets a WARNING line in the log
STEAL_WARN_MEAN, STEAL_WARN_MAX = 1.0, 5.0   # %, a slot with mean steal > 1 % or max steal > 5 % gets a WARNING
REF_RUNS, REF_T = 2, 20   # per repeat AND scenario: both victims + ping together, no flooders, 2 x 20 s
PING_V = "V2"           # the victim that pings the host (as exp41/exp43)
PING_I = 0.2            # s, ping interval (busybox ping -i)
RTT_FRAC = 2.0          # slo_ok_v2_rtt = 1 iff the run's p99 over received replies <= max(RTT_FRAC x reference p99,
RTT_FLOOR_MS = 2.0      #   RTT_FLOOR_MS) AND its ping loss <= RTT_LOSS_MAX %
RTT_LOSS_MAX = 1.0
REF_LOSS_MAX = 5.0      # %, the reference refuses to continue if its pooled ping loss is above this
REF_RTT_MIN_N = 10      # ... or if it has fewer RTT samples than this
DIP_LEVELS = (0.95, 0.90, 0.80)   # victim dip metrics, fraction of 1 s windows with min(V1, V2) / ref below
PAUSE = 8
COOLDOWN = 45           # s, extra cool-down pause between repeats (thermal), on top of PAUSE
SESSION_REPEATS = 1     # repeats per session; a long rest follows each session except the last
SESSION_REST_S = 600    # s, thermal rest between sessions, on top of COOLDOWN
SLOT_S_MEASURED = 45.5  # s per slot, measured in exp43 (ref_victim43.txt: 845 s per repeat = 60 s reference +
                        # 17 slots + 4 swaps)
REF_S_MEASURED = 60.0   # s, one reference block (2 x (20 s + ssh + PAUSE))
SWAP_S_VNET = 0.75      # s, XDP swap in + out of one vnet around an lqr_orig slot (exp43: 3 s for 4 vnets)
BUDGET_MIN_PER_REP = 15.0   # min, the time budget per repeat (run45.sh / main() print whether the plan fits)
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
OBJ_DF = os.path.join(HERE, "xdp_dropfrac.o")
PROG = {"limit": ("xdp_limit", OBJ), "dropfrac": ("xdp_dropfrac", OBJ_DF)}   # kind -> (prog name, object)
CALIB = os.path.join(HERE, "calib42_result.json")   # written by calibrate42.sh; exp45 refuses without it / on FAIL
CSV = os.path.join(HERE, "results_dyn45.csv")
TS_CSV = os.path.join(HERE, "ts_dyn45.csv")
REF_FILE = os.path.join(HERE, "ref_victim45.txt")
LOG = os.path.join(HERE, "exp45.log")   # written by run45.sh (stdout redirect), checked in main()
# per scenario: 4 configs + 1 alone slot after the scenario's reference -> 15 slots per repeat (exp44's plan with
# lqr 85_4 replaced by hyb)
CONFIGS = [("vfrot", "0.8_1_2_0.94"),                # reference, identical to exp39-exp44 vfrot (BMAX 16000)
           ("lqr_orig", "T40_paperK"), ("lqr_orig", "T60_paperK"),   # the authors' K, per-tenant CPU target (exp44)
           ("hyb", "T60_paperK")]                    # exp45: lqr_orig T60 + outer victim-feedback loop on the targets
SCEN_CONFIGS = {s: list(CONFIGS) for s in SCENARIOS}
PAIRED_REF = ("vfrot", "0.8_1_2_0.94")   # paired differences per scenario against this config
NPROC_REQUIRED = 6      # main() refuses to start unless `nproc` prints exactly this
VF_MODES = ("vfrot",)
MODES = ("lqr", "lqr_orig", "hyb") + VF_MODES
LQO_MODES = ("lqr_orig", "hyb")   # exp45: modes that run the lqr_orig controller on xdp_dropfrac
# ---- hyb (exp45): outer victim-feedback loop on lqr_orig's per-tenant targets ----
HYB_THRESH = 0.94        # hurt iff smoothed delivered rate / SLO reference < this (vfrot 0.8_1_2_0.94's threshold)
HYB_BETA = 0.8           # (b) donor target x BETA; (a) hurt-by-us target x 1 / BETA (vfrot's BETA)
HYB_T = 2.0              # s, attribution window (drop of ours within HYB_T -> (a)) and hold before any restoration
HYB_CUT_HOLD = 1.0       # s, at most one (b) cut per this (vfrot's VFROT_CUT_HOLD)
HYB_ADD_FRAC = 1.0 / 16  # restoration speed, x cpu_ref per s (vfrot: +1 Gbit/s per s on its 16 Gbit/s ceiling)
HYB_CEIL_FRAC = 6.0      # target ceiling, x cpu_ref (246.6 % of one core on calib42): above the ~217 % needed to stop
                         # lqr_orig's 0.95 / 0 flip-flop on a victim (see the header); at the ceiling = "exempt"
HYB_FLOOR_FRAC = 0.2     # target floor, x cpu_ref (T20: half the lowest target lqr_orig ran with in exp43/exp44)
HYB_GUARD_FRAC = 0.4     # a raised target is not restored below min(CEIL, CPU_eff + this x cpu_ref)
CAP_MODES = ("lqr",) + VF_MODES   # modes that rewrite the caps every tick
RESULT_NAMES = ([n for v in VICTIMS for n in (f"victim{v[1:]}_mean_gbps", f"victim{v[1:]}_p5_gbps", f"slo_ok_v{v[1:]}")]
                + ["slo_ok_worst"]
                + [n for g in FLOODERS for n in (f"flooder{g}_iperf_gbps", f"flooder{g}_ebpf_gbps")]
                + ["jain_fairness", "cpu_mean", "cpu_max", "steal_mean", "steal_max", "tick_mean_s", "tick_max_s"])
EXTRA_NAMES = (["flooder_total_gbps", "min_flooder_own_gbps", "min_flooder_ovl_gbps", "budget_ceiling_share",
                "budget_floor_share"] + [f"highcap{g}_share" for g in FLOODERS])   # as exp40
NEW_NAMES = ([f"vhost{g}_cpu_mean" for g in FLOODERS] + [f"track_err{g}" for g in FLOODERS] + ["track_err_mean"]
             + [f"u{g}_mean" for g in FLOODERS] + ["target_cpu", "u_release_s", "flooder_wait_timeouts"])   # exp42
DIP_NAMES = ([f"dip{int(round(x * 100))}_ovl" for x in DIP_LEVELS]
             + [f"dip{int(round(x * 100))}_all" for x in DIP_LEVELS])
X43_NAMES = ([f"offered{g}_gbps" for g in FLOODERS] + DIP_NAMES
             + ["v2_rtt_p50_ms", "v2_rtt_p99_ms", "v2_loss_pct", "slo_ok_v2_rtt", "slo_ok_worst_with_rtt"])   # exp43
X44_NAMES = ([f"victim{v[1:]}_ebpf_gbps" for v in VICTIMS] + [f"vhost{v}_cpu_mean" for v in VICTIMS]
             + [f"track_err{v}" for v in VICTIMS] + [f"u{v}_mean" for v in VICTIMS]
             + ["n_active_flooders"] + [f"v{v[1:]}_rate_gbps" for v in VICTIMS]
             + ["victim_release_s", "victim_wait_timeouts", "victim_win_after_release"])   # exp44
X45_NAMES = ([f"tgt{g}_mean" for g in TENANTS] + ["hyb_raises", "hyb_cuts", "hyb_first_raise_s",
                                                   "hyb_min_be_tgt"]    # exp45 (hyb only, nan otherwise)
             + [f"exempt{g}_s" for g in TENANTS]          # hyb: first time (s) g was exempt (see header), else nan
             + [f"ceil{g}_s" for g in TENANTS]            # hyb: first time (s) g's target reached the ceiling, else nan
             + [f"ubelow{g}_frac" for g in TENANTS])      # lqr_orig / hyb: fraction of g's controller steps with u > 0
                                                          # while its CPU sample < its target (nan: not controlled)
REF_RTT_NAMES = ["ref_v2_rtt_p50_ms", "ref_v2_rtt_p99_ms"]
REF_VH_NAMES = [f"ref_vhost{v}_cpu" for v in VICTIMS]   # exp44: victims' vhost CPU in the scenario's reference
# result tuple layout of run(): 3 per victim, slo_ok_worst, 2 per flooder, 7 tail values, EXTRA_NAMES, NEW_NAMES,
# X43_NAMES, X44_NAMES
R_OKW = 3 * NV
R_FL = R_OKW + 1
R_TAIL = R_FL + 2 * NF   # jain, cpu_mean, cpu_max, steal_mean, steal_max, tick_mean, tick_max
R_X = R_TAIL + 7         # exp40 extras
R_N = R_X + len(EXTRA_NAMES)   # exp42 extras
R_M = R_N + len(NEW_NAMES)     # exp43 extras
R_X44 = R_M + len(X43_NAMES)   # exp44 extras
R_X45 = R_X44 + len(X44_NAMES)  # exp45 extras
RESULT_NAMES = RESULT_NAMES + EXTRA_NAMES + NEW_NAMES + X43_NAMES + X44_NAMES + X45_NAMES
HEADER = ("rep,scenario,slot,mode,param," + ",".join(RESULT_NAMES[:R_X]) + ","
          + ",".join(f"ref_v{v[1:]}_gbps" for v in VICTIMS) + "," + ",".join(EXTRA_NAMES) + "," + ",".join(NEW_NAMES)
          + "," + ",".join(X43_NAMES) + "," + ",".join(X44_NAMES) + "," + ",".join(REF_RTT_NAMES) + ","
          + ",".join(REF_VH_NAMES) + "," + ",".join(X45_NAMES))
TS_HEADER = ("rep,scenario,slot,mode,param,t,tick_s,sample_s,cpu,steal,"
             + "".join(f"v{v[1:]}_gbps_1s," for v in VICTIMS) + "".join(f"v{v[1:]}_gbps_tick," for v in VICTIMS)
             + "".join(f"cap{g}_mbit," for g in FLOODERS) + "".join(f"rate{g}_gbps," for g in FLOODERS)
             + "budget_mbit,worst_ratio,"
             + "".join(f"vh{g}_cpu," for g in FLOODERS) + "".join(f"cpueff{g}," for g in FLOODERS)
             + "".join(f"e{g}," for g in FLOODERS) + "".join(f"u{g}," for g in FLOODERS) + "ctl,"
             + "".join(f"vh{v}_cpu," for v in VICTIMS) + "".join(f"cpueff{v}," for v in VICTIMS)
             + "".join(f"e{v}," for v in VICTIMS) + "".join(f"u{v}," for v in VICTIMS)
             + "".join(f"tgt{g}," for g in TENANTS) + "hyb_ev,"
             + "".join(f"ex{g}," for g in TENANTS) + "".join(f"ubelow{g}," for g in TENANTS)
             + "v2_rtt_ms_max,v2_ping_lost")
# ts row tuple (without rep,scenario,slot,mode,param): exp43's row up to ctl, then (exp44) per victim vhost CPU % of
# the tick, CPU_eff, e, u (lqr_orig on the victims only, else nan), then max RTT ms and lost pings of the tick
TS_CAP = 5 + 2 * NV
TS_RATE = TS_CAP + NF
TS_BUDGET = TS_RATE + NF
TS_VH = TS_BUDGET + 2
TS_EFF = TS_VH + NF
TS_E = TS_EFF + NF
TS_U = TS_E + NF
TS_CTL = TS_U + NF
TS_VVH = TS_CTL + 1      # exp44 victim columns
TS_VEFF = TS_VVH + NV
TS_VE = TS_VEFF + NV
TS_VU = TS_VE + NV
TS_TGT = TS_VU + NV      # exp45: per-tenant lqr_orig target (hyb; nan otherwise), TENANTS order
TS_HEV = TS_TGT + len(TENANTS)   # exp45: hyb event bits of the tick (1 raise, 2 cut, 4 restore up, 8 restore down)
TS_EX = TS_HEV + 1       # exp45: per-tenant exempt flag (hyb: target > base and no drop of ours for HYB_T s; else nan)
TS_UB = TS_EX + len(TENANTS)     # exp45: per-tenant u > 0 while CPU sample < target (controller steps; nan otherwise)
TS_RTT = TS_UB + len(TENANTS)
# summary metrics: (CSV column, short name, True = higher is better / False = lower is better)
SUMMARY_METRICS = ([("slo_ok_worst", "ok_worst", True), ("slo_ok_v1", "ok_v1", True), ("slo_ok_v2", "ok_v2", True),
                    ("slo_ok_worst_with_rtt", "ok_w_rtt", True), ("v2_rtt_p99_ms", "rtt_p99", False),
                    ("flooder_total_gbps", "fl_total", True)]
                   + [(f"flooder{g}_ebpf_gbps", f"fl_{g}", True) for g in FLOODERS]
                   + [("min_flooder_own_gbps", "min_own", True), ("jain_fairness", "jain", True)]
                   + [(f"victim{v[1:]}_ebpf_gbps", f"{v.lower()}_gbps", True) for v in VICTIMS]
                   + [("cpu_mean", "host_cpu", False)]
                   + [(n, n, False) for n in DIP_NAMES] + [("track_err_mean", "trk_err", False)])

IF = {}           # each victim and each flooder -> {"dev", "prog", "cfg", "cnt", "kind"} (map ids, never names)
VH = {}           # each flooder -> schedstat path of its guest's vhost-net thread
last_cap = {}     # cfg map id -> last bytes/s written (xdp_limit)
last_drop = {}    # cfg map id -> last drop_bp written (xdp_dropfrac)

SUDO_STDERR_MAX = 4000   # chars of a failed command's stderr kept in the exception (a verifier log can be long)

class SudoError(subprocess.CalledProcessError):
    # a failed 'sudo -n ...' whose str() (traceback line, every "{ex}" log line) carries the command's stderr:
    # ip/bpftool print the actual reason (kernel extack, verifier log) only there
    def __str__(self):
        err = (self.stderr or "").strip()
        if len(err) > SUDO_STDERR_MAX:
            err = "...(truncated) " + err[-SUDO_STDERR_MAX:]
        return super().__str__() + f" -- stderr: {err if err else '(empty)'}"

def sudo(args):
    p = subprocess.run(["sudo", "-n"] + args, capture_output=True, text=True)
    if p.returncode != 0:
        raise SudoError(p.returncode, p.args, output=p.stdout, stderr=p.stderr)
    return p.stdout

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

def prog_maps(dev, kind):
    # the attached program of dev must be PROG[kind] in generic mode; returns its IF entry (map ids)
    pid, mode = xdp_prog(dev)
    prog = json.loads(sudo(["bpftool", "-j", "prog", "show", "id", str(pid)])) if pid is not None else {}
    if pid is None or prog.get("name") != PROG[kind][0] or mode != "generic":
        raise SystemExit(f"{dev}: XDP prog {pid} ({prog.get('name')}, mode {mode}) is not {PROG[kind][0]} "
                         f"in generic mode")
    maps = {}
    for m in prog.get("map_ids", []):
        maps[json.loads(sudo(["bpftool", "-j", "map", "show", "id", str(m)]))["name"]] = m
    if "cfg" not in maps or "cnt" not in maps:
        raise SystemExit(f"{dev}: prog {pid} has maps {maps}, expected cfg and cnt")
    return {"dev": dev, "prog": pid, "cfg": maps["cfg"], "cnt": maps["cnt"], "kind": kind}

def load(dev, kind):
    # attach (or atomically replace the attached generic program with) PROG[kind]; new maps, new map ids.
    # -force is REQUIRED for the replace: without it ip sets XDP_FLAGS_UPDATE_IF_NOEXIST, the kernel answers EBUSY
    # "XDP program already attached" and ip exits 2 (the first calibrate42 run, 2026-11-06); with it the kernel
    # swaps the program in one step (the old one is never detached first, so the vnet is never without a program)
    sudo(["ip", "-force", "link", "set", "dev", dev, "xdpgeneric", "obj", PROG[kind][1], "sec", "xdp"])
    return prog_maps(dev, kind)

def attach(dev, cap_mbit):
    # as exp40, plus: an xdp_dropfrac left attached by an interrupted exp42/calibrate42 is replaced by xdp_limit
    pid, mode = xdp_prog(dev)
    if pid is None:
        d = load(dev, "limit"); how = "attached"
    else:
        name = json.loads(sudo(["bpftool", "-j", "prog", "show", "id", str(pid)])).get("name")
        if name == PROG["dropfrac"][0] and mode == "generic":
            d = load(dev, "limit"); how = f"replaced a left-over xdp_dropfrac (prog {pid}) by"
        elif name != PROG["limit"][0] or mode != "generic":
            raise SystemExit(f"{dev}: XDP prog {pid} ({name}, mode {mode}) is not xdp_limit "
                             f"in generic mode; detach it yourself, then rerun")
        else:
            d = prog_maps(dev, "limit"); how = "reusing already attached"
    set_cap(d, cap_mbit)
    print(f"{dev}: {how} xdp_limit prog id {d['prog']}, cfg map id {d['cfg']}, cnt map id {d['cnt']}", flush=True)
    return d

def swap(g, kind):
    # replace flooder g's program by PROG[kind] (between slots, outside every measured window); the counters of
    # the new program start at 0 (run() only uses deltas from its own baseline sample)
    old = IF[g]
    last_cap.pop(old["cfg"], None); last_drop.pop(old["cfg"], None)
    d = load(old["dev"], kind)
    IF[g] = d
    if kind == "limit":
        set_cap(d, 0)
    else:
        set_drop(d, 0)
    return d

class _SafeOut:
    # stdout wrapper for restore_tenants(): after Ctrl+C / SIGHUP the reader of our pipe (tee) may be gone, and a
    # BrokenPipeError from a print must not cut the restore short
    def __init__(self, f): self.f = f
    def write(self, s):
        try: return self.f.write(s)
        except (OSError, ValueError): return len(s)
    def flush(self):
        try: self.f.flush()
        except (OSError, ValueError): pass

def restore_tenants():
    # exp43's restore_flooders() over ALL SIX tenants (exp44: S3's lqr_orig swaps the victims too): put xdp_limit
    # (cap 0) back on every vnet, whatever is attached there NOW (asked from the kernel, not taken from IF, which can
    # lag behind). attach() reuses an attached xdp_limit, attaches xdp_limit.o if nothing is attached, and replaces an
    # xdp_dropfrac atomically ('ip -force'); anything else is left alone and reported. Never raises; SIGINT is
    # ignored meanwhile (inherited by sudo/ip) so a second Ctrl+C cannot leave a tenant half-done. Returns
    # [(tenant, error text)] of the tenants it could not restore.
    try:
        old_int = signal.signal(signal.SIGINT, signal.SIG_IGN)
    except ValueError:            # not the main thread
        old_int = None
    failed = []
    try:
        with contextlib.redirect_stdout(_SafeOut(sys.stdout)):
            for g in TENANTS:
                dev = IF.get(g, {}).get("dev")
                try:
                    try:
                        dev = vnet(GUESTS[g][0])   # as exp39-exp43: ask libvirt for the current name
                    except BaseException:
                        if dev is None:
                            raise
                    if g in IF:
                        last_cap.pop(IF[g].get("cfg"), None); last_drop.pop(IF[g].get("cfg"), None)
                    IF[g] = attach(dev, 0)
                except BaseException as ex:
                    failed.append((g, f"{dev}: {ex}"))
                    print(f"WARNING: could not restore xdp_limit on tenant {g} ({dev}): {ex}", flush=True)
            if failed:
                print("WARNING: xdp_limit NOT restored on " + ", ".join(g for g, _ in failed) + "; check with: sudo "
                      "bpftool net show", flush=True)
            else:
                print("XDP RESTORE: xdp_limit (cap 0) on all six tenants: " + ", ".join(
                      f"{g} {IF[g]['dev']} prog {IF[g]['prog']}" for g in TENANTS), flush=True)
    finally:
        if old_int is not None:
            signal.signal(signal.SIGINT, old_int)
    return failed

def exit_on_term_signals():
    # SIGTERM (kill) and SIGHUP (terminal closed) -> SystemExit, so the finally blocks (restore_tenants) run as on
    # Ctrl+C; a signal that is already ignored (SIGHUP under run44.sh's nohup) stays ignored
    def stop(signum, frame):
        raise SystemExit(f"stopped by signal {signal.Signals(signum).name}")
    for s in (signal.SIGTERM, signal.SIGHUP):
        if signal.getsignal(s) != signal.SIG_IGN:
            signal.signal(s, stop)

@contextlib.contextmanager
def dropfrac_tenants(tenants):
    # xdp_dropfrac on the given tenants' vnets for one lqr_orig slot (S1/S2: the active flooders, S3: all six),
    # xdp_limit back afterwards on ALL SIX (also after a failed swap, any other error or Ctrl+C: restore_tenants())
    try:
        for g in tenants:
            swap(g, "dropfrac")
        yield
    finally:
        failed = restore_tenants()
        if failed:
            raise SystemExit("xdp_limit could not be restored on " + "; ".join(f"{g} {t}" for g, t in failed))

def counters(d):
    out = sudo(["bpftool", "-j", "map", "dump", "id", str(d["cnt"])])
    c = {}
    for e in json.loads(out):
        f = e.get("formatted", e); v = f["value"]
        c[f["key"]] = sum(v) if isinstance(v, list) else v
    return c

def write_u64(map_id, val):
    h = [f"{x:02x}" for x in struct.pack("<Q", val)]
    sudo(["bpftool", "map", "update", "id", str(map_id), "key", "0", "0", "0", "0", "value", "hex"] + h)

def set_cap(d, mbps):
    # 0 = unlimited; skip the bpftool call if this map already holds the value
    b = int(mbps * 1e6 / 8)
    if last_cap.get(d["cfg"]) == b:
        return
    write_u64(d["cfg"], b)
    last_cap[d["cfg"]] = b

def set_drop(d, bp):
    # xdp_dropfrac: drop_bp (0 = no drop); skip the bpftool call if this map already holds the value
    if d.get("kind") != "dropfrac":
        raise SystemExit(f"{d['dev']}: set_drop on a {d.get('kind')} program")
    bp = int(bp)
    if not 0 <= bp <= 10000:
        raise SystemExit(f"drop_bp {bp} out of range")
    if last_drop.get(d["cfg"]) == bp:
        return
    write_u64(d["cfg"], bp)
    last_drop[d["cfg"]] = bp

# ---------- per-tenant CPU: the guest's vhost-net thread ----------

def vhost_task(dom):
    # schedstat path of the "vhost-<qemu pid>" thread of dom's qemu process (no sudo: /proc files are 0444)
    pids = []
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            with open(f"/proc/{p}/cmdline", "rb") as f:
                a = f.read().split(b"\0")
        except OSError:
            continue
        if a and b"qemu-system" in a[0] and any(x.startswith(f"guest={dom},".encode()) for x in a):
            pids.append(p)
    if len(pids) != 1:
        raise SystemExit(f"{dom}: expected one qemu process with guest={dom}, found {pids}")
    pid, tids = pids[0], []
    for t in os.listdir(f"/proc/{pids[0]}/task"):
        try:
            with open(f"/proc/{pid}/task/{t}/comm") as f:
                if f.read().strip() == f"vhost-{pid}":
                    tids.append(t)
        except OSError:
            pass
    if len(tids) != 1:
        raise SystemExit(f"{dom}: expected one vhost-{pid} thread, found {tids} (multiqueue or no vhost-net?)")
    return f"/proc/{pid}/task/{tids[0]}/schedstat"

def vhost_ns(path):
    # cumulative run time of the thread, ns (schedstat field 1 = sum_exec_runtime; ns resolution, unlike utime/stime)
    try:
        with open(path) as f:
            return int(f.read().split()[0])
    except (OSError, ValueError, IndexError) as e:
        raise SystemExit(f"cannot read {path} ({e}): the guest's qemu/vhost thread is gone (guest restarted?)")

def vh_read():
    # {tenant: (monotonic t, ns)} for all six tenants (exp44; exp43 read the four flooders)
    return {g: (time.monotonic(), vhost_ns(VH[g])) for g in TENANTS}


def vh_pct(a, b):
    # % of one core between two (t, ns) readings
    return (b[1] - a[1]) / 1e9 / max(1e-3, b[0] - a[0]) * 100.0

def cpu():
    # aggregate cpu line of /proc/stat: (total, idle + iowait, steal) jiffies
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

# ---------- vfrot (exp40 code, unchanged) ----------

def parse_vfrot(mode, param):
    # "BETA_ADD_T[_THRESH][_B<bmax>]", e.g. "0.8_1_2_0.94" (exp39)
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
        raise SystemExit(f"{mode} param {param!r} is malformed: expected BETA_ADD_T[_THRESH][_B<bmax>], "
                         f"e.g. 0.8_1_2_0.94")
    if not (0 < beta < 1 and add > 0 and rot_t > 0 and 0.85 <= thresh <= 1.0):
        raise SystemExit(f"{mode} param {param!r} out of range: need 0 < BETA < 1, ADD > 0, T > 0 "
                         f"and 0.85 <= THRESH <= 1.0")
    if ffrac is not None:
        raise SystemExit(f"vfrot param {param!r}: the F<floor_frac> suffix is only for vffair (not in exp42)")
    if not VF_BMAX_RANGE[0] <= bmax <= VF_BMAX_RANGE[1]:
        raise SystemExit(f"{mode} param {param!r}: BMAX {bmax:g} outside [{VF_BMAX_RANGE[0]:g}, {VF_BMAX_RANGE[1]:g}]")
    return beta, add, rot_t, thresh, bmax, ffrac

def vf_params(mode, param):
    if (mode, param) not in VFROT:
        VFROT[(mode, param)] = parse_vfrot(mode, param)
    return VFROT[(mode, param)]

def active_flooders(offered, prev_off):
    # active-flooder detection (vfrot): offered (passed + dropped) rate over this tick >= DFS_ACTIVE_MBIT
    return [g for g in FLOODERS
            if (offered[g][-1][1] - prev_off[g][1]) * 8 / 1e6
               / max(1e-3, offered[g][-1][0] - prev_off[g][0]) >= DFS_ACTIVE_MBIT]

def high_cap(budget, n_act):
    # cap of the holder: every other active flooder keeps CAP_MIN
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
    # one active flooder (the holder) gets high_cap, every other flooder CAP_MIN; re-decided by pick(act, holder)
    # when the holder is not active or has held for rot_t s; with no flooder active, keep the last caps
    if not act:
        return caps
    h = state["holder"]
    if h not in act or elapsed - state["since"] >= rot_t:
        state["holder"], state["since"] = pick(act, h), elapsed
    on = high_cap(budget, len(act))
    return [on if g == state["holder"] else CAP_MIN for g in FLOODERS]

def lowest_delivered_pick(delivered):
    # the active flooder with the LOWEST cumulative delivered traffic; ties go to a non-holder (round-robin)
    def pick(a, h):
        low = min(delivered[g] for g in a)
        tied = [g for g in a if delivered[g] == low]
        others = [g for g in tied if g != h]
        return next_after(h, others) if others else h
    return pick

def pidrotf_caps(act, budget, elapsed, rot_t, caps, delivered, state):
    return hold_caps(act, budget, elapsed, rot_t, caps, state, lowest_delivered_pick(delivered))

def pf_update(delivered, passed, act, pf_all):
    # add this tick's passed bytes per flooder, then zero the counters on the first tick on which ALL are active
    for g in FLOODERS:
        delivered[g] += passed[g]
    all_on = len(act) == NF
    if all_on and not pf_all:
        for g in FLOODERS:
            delivered[g] = 0.0
    return all_on

# ---------- lqr (exp39 code, unchanged) ----------

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
    # incremental model on x = [e, d] (e = y - target, d = e(k) - e(k-1)), input du (Gbit/s)
    a = math.exp(-DT / LQR_TAU)
    b = gain_scale * K_CPU * (1.0 - a)
    return np.array([[1.0, a], [0.0, a]]), np.array([[b], [b]]), a, b

def riccati(A, B, Q, R, what, tol=1e-12, max_iter=100000):
    # discrete LQR gain K (1x2) by plain Riccati iteration (as exp39's lqr_gain)
    P = Q.copy()
    for _ in range(max_iter):
        Pn = Q + A.T @ P @ A - A.T @ P @ B @ np.linalg.solve(R + B.T @ P @ B, B.T @ P @ A)
        done = np.max(np.abs(Pn - P)) <= tol * max(1.0, np.max(np.abs(Pn)))
        P = Pn
        if done:
            return np.linalg.solve(R + B.T @ P @ B, B.T @ P @ A)
    raise SystemExit(f"{what}: Riccati iteration did not converge")

def lqr_gain(rwt):
    A, B, _, _ = lqr_plant()
    return riccati(A, B, np.diag(LQR_Q), np.array([[LQR_R * rwt]]), f"lqr (RWT {rwt})")

def lqr_report(param, rwt, K):
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

# ---------- lqr_orig (the authors' controller) ----------

def lqo_plant(tau, b, dt=LQO_DT):
    return np.array([[1.0, dt], [-dt / tau, 1.0 - dt / tau]]), np.array([[0.0], [-b * dt]])

def lqo_gain(tau, b, target, dt=LQO_DT):
    # K (k_e, k_edot) from the discrete ARE, Q = diag(100, 10) x alpha, alpha = 10 / max(target, 1), R = 0.01
    A, B = lqo_plant(tau, b, dt)
    alpha = LQO_ALPHA_NUM / max(target, 1.0)
    K = riccati(A, B, np.diag([LQO_Q[0] * alpha, LQO_Q[1] * alpha]), np.array([[LQO_R]]),
                f"lqr_orig (tau {tau:g}, b {b:g}, target {target:g})")
    return float(K[0, 0]), float(K[0, 1])

def expm_np(M):
    # matrix exponential by scaling and squaring of a 30-term Taylor series (no scipy needed at run time;
    # test_exp43.py cross-checks it against scipy.linalg.expm)
    n = max(0, int(math.ceil(math.log2(max(1e-300, np.max(np.abs(M))) * 2.0))) + 4)
    X = M / 2.0 ** n
    S, term = np.eye(M.shape[0]), np.eye(M.shape[0])
    for k in range(1, 30):
        term = term @ X / k
        S = S + term
    for _ in range(n):
        S = S @ S
    return S

def lqo_plant_zoh(tau, b, dt=LQO_DT):
    # exp43: exact zero-order-hold discretisation of the authors' continuous model, the one their forward-Euler
    # A, B approximate: x = [e, edot], xdot = [[0, 1], [-1/tau, -1/tau]] x + [[0], [-b]] u (DC gain e = -b tau u).
    # Euler (exp42) is only usable for dt/tau below ~1.5; at calib42's dt/tau = 6.2 it flips the ARE gain's sign.
    Ac = np.array([[0.0, 1.0], [-1.0 / tau, -1.0 / tau]])
    Bc = np.array([[0.0], [-b]])
    M = np.zeros((3, 3)); M[:2, :2] = Ac; M[:2, 2:] = Bc
    E_ = expm_np(M * dt)
    return E_[:2, :2], E_[:2, 2:]

def lqo_gain_zoh(tau, b, target, dt=LQO_DT):
    # exp43: the authors' ARE (same Q = diag(100, 10) x alpha, R = 0.01) on the ZOH plant
    A, B = lqo_plant_zoh(tau, b, dt)
    alpha = LQO_ALPHA_NUM / max(target, 1.0)
    K = riccati(A, B, np.diag([LQO_Q[0] * alpha, LQO_Q[1] * alpha]), np.array([[LQO_R]]),
                f"lqr_orig ZOH (tau {tau:g}, b {b:g}, target {target:g})")
    return float(K[0, 0]), float(K[0, 1])

def check_gain_sign(K, what):
    # exp43: u = -K x must RAISE the drop fraction when CPU is above target (e > 0): K[0] < 0, as the paper's K
    if not (math.isfinite(K[0]) and math.isfinite(K[1]) and K[0] < 0):
        raise SystemExit(f"REFUSING TO START: {what}: K = [{K[0]:.4f}, {K[1]:.4f}] has K[0] >= 0 (wrong sign: u > 0 "
                         f"when CPU < target, the exp42 latch)")
    return K

def lqo_eig(tau, b, K, gain_scale=1.0, zoh=False):
    # spectral radius of A - B K with the plant gain b scaled (Euler plant, or the ZOH plant with zoh=True)
    A, B = (lqo_plant_zoh if zoh else lqo_plant)(tau, b * gain_scale)
    return float(max(abs(np.linalg.eigvals(A - B @ np.array([K])))))

def lqo_new():
    return {"win": [], "smooth": None, "e": None}

def lqo_filter(state, cpu_pct):
    # their Algorithm 1 filter: median of the last LQO_MED_N samples (of those available at start-up), trend blend
    w = state["win"]; w.append(cpu_pct); del w[:-LQO_MED_N]
    sm = st.median(w)
    trend = 0.0 if state["smooth"] is None else sm - state["smooth"]
    state["smooth"] = sm
    return (1.0 - LQO_TREND_W) * sm + LQO_TREND_W * (sm + trend)

def lqo_soft(u, cpu_eff, target):
    # soft limit: e > margin: max(u, hard), hard = min((e / (2 target))^0.7 x 0.95, 0.95); 0 < e <= margin:
    # max(u, (1 - w) u + w hard) with w = e / margin and exponent 0.5; e <= 0: u unchanged
    e = cpu_eff - target
    if e <= 0:
        return u
    if e > LQO_MARGIN:
        return max(u, min((e / (2.0 * target)) ** LQO_HARD_EXP_OUT * LQO_UMAX, LQO_UMAX))
    hard = min((e / (2.0 * target)) ** LQO_HARD_EXP_IN * LQO_UMAX, LQO_UMAX)
    w = e / LQO_MARGIN
    return max(u, (1.0 - w) * u + w * hard)

def lqo_quant(u):
    # clip to [0, 0.95], nearest of the 20 levels (first on a tie, as numpy argmin), as drop_bp
    u = min(LQO_UMAX, max(0.0, u))
    return min(LQO_LEVELS_BP, key=lambda lv: abs(u * 10000.0 - lv))

def lqo_step(state, cpu_pct, target, K):
    # one controller step: returns (drop_bp, CPU_eff, e)
    eff = lqo_filter(state, cpu_pct)
    e = eff - target
    edot = 0.0 if state["e"] is None else (e - state["e"]) / LQO_DT   # nominal dt, as their code
    state["e"] = e
    u = -(K[0] * e + K[1] * edot)
    return lqo_quant(lqo_soft(u, eff, target)), eff, e

def drop_model(total, bp):
    # Python model of xdp_dropfrac.c's rule (tested against the C source text in test_exp42.py)
    return bp != 0 and (total * bp) % 10000 < bp

def parse_lqo(param):
    # "T<pct>_paperK" or "T<pct>_zoh": target = pct/100 x calib cpu_ref; returns (fraction, "paperK" | "zoh").
    # exp42's plain "T<pct>" (ARE on the Euler model of the calibrated plant, wrong sign) is refused
    parts = param.split("_")
    try:
        if not parts[0].startswith("T") or len(parts) != 2 or parts[1] not in ("paperK", "zoh"):
            raise ValueError
        pct = float(parts[0][1:])
    except ValueError:
        raise SystemExit(f"lqr_orig param {param!r} is malformed: expected T<pct>_paperK or T<pct>_zoh, e.g. "
                         f"T60_zoh (exp42's plain T<pct> = Euler ARE on calib42, wrong sign, is not allowed)")
    if not 10 <= pct <= 100:
        raise SystemExit(f"lqr_orig param {param!r} out of range: need 10 <= pct <= 100")
    return pct / 100.0, parts[1]

def load_calib(path=None):
    # calib42_result.json from calibrate42.sh; refuse if missing, malformed or verdict FAIL
    path = path or CALIB
    if not os.path.exists(path):
        raise SystemExit(f"REFUSING TO START: {os.path.basename(path)} not found -- run ./calibrate42.sh first")
    try:
        with open(path) as f:
            d = json.load(f)
        verdict = d["verdict"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise SystemExit(f"REFUSING TO START: {os.path.basename(path)} is malformed ({e!r})")
    if verdict not in ("PASS", "WARN", "FAIL"):
        raise SystemExit(f"REFUSING TO START: {os.path.basename(path)} has verdict {verdict!r}")
    if verdict == "FAIL":
        bad = [f"{c[0]}: {c[2]}" for c in d.get("checks", []) if c[1] == "FAIL"]
        raise SystemExit(f"REFUSING TO START: calibration verdict FAIL ({'; '.join(bad)}) -- fix and rerun "
                         f"./calibrate42.sh")
    try:
        vals = [float(d[k]) for k in ("cpu_ref", "tau", "b")]
    except (ValueError, KeyError, TypeError) as e:
        raise SystemExit(f"REFUSING TO START: {os.path.basename(path)} is malformed ({e!r})")
    if not all(math.isfinite(v) and v > 0 for v in vals):
        raise SystemExit(f"REFUSING TO START: {os.path.basename(path)}: cpu_ref, tau, b must be finite and > 0, "
                         f"got {vals}")
    return d

def setup_lqo(param, calib):
    # (target, K, fraction, kind); refuses (before anything runs) if K[0] >= 0
    frac, kind = parse_lqo(param)
    target = frac * float(calib["cpu_ref"])
    if kind == "paperK":
        K = K_PAPER
    else:
        K = lqo_gain_zoh(float(calib["tau"]), float(calib["b"]), target)
    check_gain_sign(K, f"lqr_orig {param}")
    return target, K, frac, kind

def calib_gain(calib=None, frac=0.6):
    # the re-identified (ZOH) K at frac x cpu_ref; run44.sh prints it and refuses if K[0] >= 0
    calib = calib or load_calib()
    return lqo_gain_zoh(float(calib["tau"]), float(calib["b"]), frac * float(calib["cpu_ref"]))

def lqo_report(param, calib):
    target, K, frac, kind = LQO[param]
    tau, b = float(calib["tau"]), float(calib["b"])
    ke = lqo_gain(tau, b, target)
    return (f"LQR_ORIG {param}: target {target:.2f} % of one core ({frac * 100:g} % of cpu_ref "
            f"{float(calib['cpu_ref']):.2f}), K = [{K[0]:.4f}, {K[1]:.4f}] "
            f"({'PAPER K, unchanged' if kind == 'paperK' else 'ARE on the ZOH plant of calib42'}); identified tau "
            f"{tau:.3f} s, b {b:.3f} (dt/tau {LQO_DT / tau:.2f}); spectral radius on the ZOH plant at gain "
            f"x0.5/1/1.5/2: " + "/".join(f"{lqo_eig(tau, b, K, s, zoh=True):.3f}" for s in (0.5, 1.0, 1.5, 2.0))
            + f"; for the record, exp42's Euler ARE gain here would be [{ke[0]:+.4f}, {ke[1]:+.4f}]")

# ---------- hyb (exp45): outer victim-feedback loop on lqr_orig's per-tenant targets ----------

def hyb_new(tenants, base, cpu_ref):
    # state of one hyb run: every controlled tenant starts at the base target (lqr_orig's T<pct>)
    return {"tgt": {g: float(base) for g in tenants}, "base": float(base),
            "ceil": HYB_CEIL_FRAC * cpu_ref, "floor": HYB_FLOOR_FRAC * cpu_ref,
            "add": HYB_ADD_FRAC * cpu_ref, "guard": HYB_GUARD_FRAC * cpu_ref,
            "u_pos": {g: float("-inf") for g in tenants},   # last time (s) a drop > 0 of ours was applied to g
            "last_act": {g: float("-inf") for g in tenants},  # last time (s) g's target was raised or cut
            "cut_block": float("-inf"), "other_t": float("-inf"), "raises": 0, "cuts": 0, "first_raise": float("nan")}

def hyb_classify(state, t, ratios):
    # (hurt by us, hurt by others, donors) from ratio = delivered / SLO reference (+inf without an SLO) and our own
    # drop history only; tenant names are opaque keys
    hurt = [g for g, r in ratios.items() if r < HYB_THRESH]
    ours = [g for g in hurt if g in state["tgt"] and t - state["u_pos"][g] <= HYB_T]
    others = [g for g in hurt if g not in ours]
    donors = [g for g in state["tgt"] if ratios.get(g, math.inf) > 1.0 / HYB_THRESH
              and state["tgt"][g] <= state["base"] + 1e-9]
    return ours, others, donors

def hyb_step(state, t, ratios, eff, dt=LQO_DT):
    # one outer-loop step (before the lqr_orig step at time t s): returns the event bits; mutates state["tgt"].
    # ratios: {tenant: delivered / reference} for every tenant with an SLO reference (+inf or absent: no SLO);
    # eff: {tenant: lqr_orig CPU_eff of the last step} (nan before the first)
    tgt, ev = state["tgt"], 0
    ours, others, donors = hyb_classify(state, t, ratios)
    acted = set()
    for g in ours:                                            # (a) our own drop hurts g: raise its target
        new = min(state["ceil"], tgt[g] / HYB_BETA)
        if new > tgt[g] + 1e-12:
            tgt[g] = new; state["last_act"][g] = t; acted.add(g); ev |= 1; state["raises"] += 1
            if math.isnan(state["first_raise"]):
                state["first_raise"] = t
    if others:                                                # (b) someone else hurts a tenant: cut the donors
        state["other_t"] = t
        if t >= state["cut_block"] and donors:
            for g in donors:
                new = max(state["floor"], tgt[g] * HYB_BETA)
                if new < tgt[g] - 1e-12:
                    tgt[g] = new; state["last_act"][g] = t; acted.add(g); ev |= 2
            if ev & 2:
                state["cuts"] += 1
            state["cut_block"] = t + HYB_CUT_HOLD
    hurt = set(ours) | set(others)
    for g in tgt:                                             # (c) hold, then restore slowly toward the base
        if g in acted or g in hurt or t - state["last_act"][g] < HYB_T:
            continue
        if tgt[g] < state["base"] - 1e-12 and t - state["other_t"] >= HYB_T:
            tgt[g] = min(state["base"], tgt[g] + state["add"] * dt); ev |= 4
        elif tgt[g] > state["base"] + 1e-12:
            e_ = eff.get(g, float("nan"))
            lo = state["base"] if math.isnan(e_) else max(state["base"], min(state["ceil"], e_ + state["guard"]))
            if tgt[g] > lo + 1e-12:
                tgt[g] = max(lo, tgt[g] - state["add"] * dt); ev |= 8
    return ev

def parse_hyb(param):
    # "T<pct>_paperK": hyb starts every controlled tenant at lqr_orig's T<pct> target, paper K only (as ported)
    frac, kind = parse_lqo(param)
    if kind != "paperK":
        raise SystemExit(f"hyb param {param!r}: only T<pct>_paperK (lqr_orig exactly as ported)")
    if not HYB_FLOOR_FRAC < frac < HYB_CEIL_FRAC:
        raise SystemExit(f"hyb param {param!r}: start target must lie between the floor T{HYB_FLOOR_FRAC * 100:g} and "
                         f"the ceiling T{HYB_CEIL_FRAC * 100:g}")
    return frac, kind

def hyb_report(param, calib):
    target, K, frac, kind = LQO[param]
    c = float(calib["cpu_ref"])
    return (f"HYB {param}: lqr_orig (paper K [{K[0]:.4f}, {K[1]:.4f}], dt {LQO_DT:g} s) per controlled tenant, start "
            f"target {target:.2f} % (T{frac * 100:g}); outer loop: hurt iff ratio < {HYB_THRESH:g}; (a) hurt + our drop "
            f"within {HYB_T:g} s -> target / {HYB_BETA:g} up to ceiling {HYB_CEIL_FRAC * c:.2f} % (T{HYB_CEIL_FRAC * 100:g}); "
            f"(b) hurt + no drop of ours for {HYB_T:g} s -> donors (ratio > {1 / HYB_THRESH:.4f}, no SLO = inf, target "
            f"<= base) x {HYB_BETA:g} down to floor {HYB_FLOOR_FRAC * c:.2f} % (T{HYB_FLOOR_FRAC * 100:g}), at most once "
            f"per {HYB_CUT_HOLD:g} s; (c) hold {HYB_T:g} s, then back toward the base at {HYB_ADD_FRAC * c:.2f} %/s, a "
            f"raised target not below min(ceiling, CPU_eff + {HYB_GUARD_FRAC * c:.2f})")

# ---------- metrics ----------

def set_scenario(name):
    # exp44: select the scenario every helper below refers to
    global SCENARIO
    if name not in SCENARIOS:
        raise SystemExit(f"unknown scenario {name!r} (have {', '.join(SCENARIOS)})")
    SCENARIO = name

def win():
    return SCENARIOS[SCENARIO]["win"]

def active():
    # the scenario's flooders, in FLOODERS order
    return [g for g in FLOODERS if g in win()]

def v_rate(v):
    return SCENARIOS[SCENARIO]["v_rate"][v]

def lqo_tenants():
    # tenants lqr_orig controls in the scenario: the active flooders (S1, S2, as exp43), or all six (S3)
    return (list(VICTIMS) if SCENARIOS[SCENARIO]["lqo_victims"] else []) + active()

def flooder_dur(g):
    return win()[g][1]

def overlap():
    # the window in which all of the scenario's flooders are active
    w = win()
    return (float(max(w[g][0] for g in w)), float(min(w[g][0] + w[g][1] for g in w)))

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
    if not ts:
        return float("nan"), float("nan")
    b = [row[TS_BUDGET] for row in ts]
    return (sum(x >= bmax - 1e-6 for x in b) / len(b), sum(x <= NF * CAP_MIN + 1e-6 for x in b) / len(b))

def highcap_shares(ts):
    ov = overlap()
    rows = [row for row in ts if ov[0] <= row[0] < ov[1]]
    if not rows:
        return [float("nan")] * NF
    return [sum(row[TS_CAP + i] >= HIGH_CAP_FRAC * row[TS_BUDGET] for row in rows) / len(rows) for i in range(NF)]

def own_window_stats(ts, ctl_only, col):
    # per flooder: mean of ts column col + i over rows inside its own window (t rel. flooder launch);
    # ctl_only: only lqr_orig controller ticks; col TS_E is averaged as |e|; nan for a flooder not in the scenario
    out = []
    for i, g in enumerate(FLOODERS):
        if g not in win():
            out.append(float("nan"))
            continue
        a, d = win()[g]
        xs = [abs(r[col + i]) if col == TS_E else r[col + i] for r in ts
              if a <= r[0] < a + d and (r[TS_CTL] == 1 or not ctl_only)]
        out.append(nanstat(xs, st.mean))
    return out

def victim_window_stats(ts, ctl_only, col):
    # exp44, per victim: mean of ts column col + i over rows with OFFSET + 1 <= t < OFFSET + T (inside every victim's
    # window, t rel. flooder launch); ctl_only: only lqr_orig controller ticks; col TS_VE is averaged as |e|
    lo, hi = OFFSET + 1.0, OFFSET + T
    out = []
    for i, v in enumerate(VICTIMS):
        xs = [abs(r[col + i]) if col == TS_VE else r[col + i] for r in ts
              if lo <= r[0] < hi and (r[TS_CTL] == 1 or not ctl_only)]
        out.append(nanstat(xs, st.mean))
    return out

def dip_metrics(ratios, starts, ovl=None, levels=DIP_LEVELS):
    # exp43: ratios[j] = min over victims of the 1 s window rate / that victim's reference, window j starting at
    # starts[j] s after the flooder launch. Returns (fractions below each level over the windows lying fully
    # inside ovl = (a, b), fractions below each level over all windows); nan where there is no window
    ovl = ovl or overlap()
    sel = [r for r, s0 in zip(ratios, starts) if s0 >= ovl[0] - 1e-9 and s0 + 1.0 <= ovl[1] + 1e-9]
    def frac(xs, lv):
        return sum(x < lv for x in xs) / len(xs) if xs else float("nan")
    return [frac(sel, lv) for lv in levels], [frac(list(ratios), lv) for lv in levels]

def q_incl(xs, pct):
    # (exp41) inclusive (linear) percentile, integer pct, the same arithmetic as
    # statistics.quantiles(xs, n=100, method="inclusive")[pct - 1]; +inf values are allowed
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
    # (exp41) busybox ping output -> ({seq: rtt ms} of the first reply per seq, probes sent). Sent = the
    # "N packets transmitted" summary if present, else n_expected (every missing seq is lost); DUP and seq >= sent
    # are ignored
    m = PING_TX_RE.search(text)
    sent = int(m.group(1)) if m else n_expected
    got = {}
    for s_, t_ in PING_RE.findall(text):
        s_ = int(s_)
        if s_ < sent and s_ not in got:
            got[s_] = float(t_)
    return got, sent

def rtt_stats(got, sent):
    # (exp41) (p50 ms, p99 ms over the RECEIVED replies, loss %, p99 with every lost ping as +inf)
    rx = list(got.values())
    lost = max(0, sent - len(rx))
    loss = 100.0 * lost / sent if sent else float("nan")
    p99_slo = q_incl(rx + [math.inf] * lost, 99) if sent else float("nan")
    return q_incl(rx, 50), q_incl(rx, 99), loss, p99_slo

def rtt_thresh(ref_p99):
    # (exp41 after the fix) the RTT SLO bound on the run's p99, ms
    return max(RTT_FRAC * ref_p99, RTT_FLOOR_MS)

def rtt_ok(p99, ref_p99, loss=0.0):
    # (exp41) 1.0 if p99 <= rtt_thresh(ref_p99) and loss <= RTT_LOSS_MAX %, else 0.0 (no replies: 0.0);
    # no reference: nan
    if math.isnan(ref_p99):
        return float("nan")
    return 1.0 if (not math.isnan(p99) and p99 <= rtt_thresh(ref_p99) and not loss > RTT_LOSS_MAX) else 0.0

def ping_ticks(t_rows, t_ping, got, sent):
    # (exp41) per ts row i, (max RTT ms, lost count) of the pings SENT in (t_{i-1}, t_i]; send time of seq s =
    # t_ping + s x PING_I (approximate)
    out = [[float("nan"), 0] for _ in t_rows]
    if not t_rows:
        return []
    for s_ in range(sent):
        i = min(bisect.bisect_left(t_rows, t_ping + s_ * PING_I), len(t_rows) - 1)
        if s_ in got:
            out[i][0] = got[s_] if math.isnan(out[i][0]) else max(out[i][0], got[s_])
        else:
            out[i][1] += 1
    return [tuple(x) for x in out]

def ping_cmd(secs):
    # (exp41) secs / PING_I probes every PING_I s; -W 1: wait at most 1 s for a missing last reply; -w: deadline
    return f"ping -c {int(round(secs / PING_I))} -i {PING_I:g} -W 1 -w {int(secs) + 2} {HOST}"

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
    b = f" -b {rate_gbit / par:g}G" if rate_gbit else ""
    return f"{pre}iperf3 -c {HOST} -p {GUESTS[guest][2]} -t {secs}{p}{b} -J"

def victim_cmds(secs):
    return {v: iperf_cmd(v, secs, rate_gbit=v_rate(v)) for v in VICTIMS}   # exp44: the scenario's rate per victim

def flooder_cmds():
    return {g: iperf_cmd(g, win()[g][1], PAR[g], win()[g][0]) for g in active()}   # exp44: active flooders only

def wait_all(procs, timeout_s):
    # wait for every process with one shared deadline; terminate the ones still running; returns how many timed out
    deadline, n = time.monotonic() + timeout_s, 0
    for p in procs:
        try:
            p.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            p.terminate(); n += 1
    return n

# ---------- setup ----------

def check_servers():
    out = subprocess.run(["ss", "-ltnH"], capture_output=True, text=True).stdout
    ports = sorted(GUESTS[g][2] for g in GUESTS)
    missing = [p for p in ports
               if not any(l.split()[3].endswith(f":{p}") for l in out.splitlines() if len(l.split()) > 3)]
    if missing:
        raise SystemExit("no iperf3 server listening on host port(s) " + ", ".join(map(str, missing))
                         + "; start them first: " + "; ".join(f"iperf3 -s -D -p {p}" for p in missing))
    print("iperf3 servers listening on ports " + ", ".join(map(str, ports)), flush=True)

def check_guests():
    bad = []
    for g, (dom, ip, _) in GUESTS.items():
        try:
            tools = ["iperf3", "ping"] if g == PING_V else ["iperf3"]   # exp43: the ping victim needs ping
            p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", f"alpine@{ip}",
                                " && ".join(f"command -v {x}" for x in tools)], capture_output=True, text=True,
                               timeout=20)
            ok = p.returncode == 0 and len(p.stdout.split()) == len(tools)
        except subprocess.TimeoutExpired:
            ok = False
        if not ok:
            bad.append(f"{dom} ({ip})" + (f" -- create it with: sudo bash {MAKE_SCRIPT[dom]}" if dom in MAKE_SCRIPT else ""))
    if bad:
        raise SystemExit("guest(s) not reachable by ssh alpine@IP or without iperf3: " + "; ".join(bad))
    print("guests reachable with iperf3: " + ", ".join(f"{g}={GUESTS[g][0]}" for g in GUESTS)
          + f"; ping on {GUESTS[PING_V][0]}", flush=True)

def resolve_vhost():
    # exp44: all six tenants (same code path as exp43's flooders: the guest's single vhost-<qemu pid> thread)
    for g in TENANTS:
        VH[g] = vhost_task(GUESTS[g][0])
        vhost_ns(VH[g])
    print("VHOST THREADS (per-tenant CPU of lqr_orig, logged for every mode, all six tenants): "
          + ", ".join(f"{g} = {GUESTS[g][0]} {VH[g]}" for g in TENANTS), flush=True)

def reference(rep, procs):
    # exp43's reference with the CURRENT scenario's victim rates: both victims together + ping from PING_V, flooder
    # caps unlimited, no flooders; victim reference = median eBPF rate over REF_RUNS x REF_T s; RTT reference =
    # p50/p99 of ALL received replies pooled; refuses if loss > REF_LOSS_MAX % or < REF_RTT_MIN_N replies.
    # exp44: also each victim's vhost CPU (% of one core, mean over the runs) and two checks, refusing in rep 1
    # (fail fast, before the first 600 s rest), a WARNING in later repeats: every victim reaches
    # REF_RATE_MIN_FRAC x its offered rate, and (S3) V2's vhost CPU >= VH_MIN["pct"] (S3_VH_MIN_FRAC x T40 target).
    # Returns (refs, base_cpu, rref, vh_ref, warnings)
    for g in FLOODERS:
        if IF[g].get("kind", "limit") == "limit":
            set_cap(IF[g], 0)
    ebpf, ip, vhc = {v: [] for v in VICTIMS}, {v: [] for v in VICTIMS}, {v: [] for v in VICTIMS}
    cpus, steals, rtts, sent_all = [], [], [], 0
    n_ping = int(round(REF_T / PING_I))
    for i in range(REF_RUNS):
        outs = {v: tempfile.TemporaryFile("w+") for v in VICTIMS}
        po = tempfile.TemporaryFile("w+")
        c0 = {v: counters(IF[v])[2] for v in VICTIMS}
        k0 = cpu()
        h0 = vh_read()
        ps = [ssh(v, cmd, outs[v]) for v, cmd in victim_cmds(REF_T).items()]
        pp = ssh(PING_V, ping_cmd(REF_T), po)
        procs += ps + [pp]
        for p in ps: p.wait()
        k1 = cpu()
        h1 = vh_read()
        c1 = {v: counters(IF[v])[2] for v in VICTIMS}
        pp.wait()
        po.seek(0); got, sent = parse_ping(po.read(), n_ping); po.close()
        rtts += list(got.values()); sent_all += sent
        busy, steal = busy_steal(k0, k1)
        cpus.append(busy); steals.append(steal)
        for v in VICTIMS:
            ebpf[v].append((c1[v] - c0[v]) * 8 / 1e9 / REF_T); ip[v].append(iperf_sum(outs[v])); outs[v].close()
            vhc[v].append(vh_pct(h0[v], h1[v]))
        print(f"rep {rep} {SCENARIO} ref run {i + 1}: both victims together, " + ", ".join(
                  f"{v} {ebpf[v][-1]:.2f} Gbit/s (eBPF) {ip[v][-1]:.2f} (iperf3) vhost {vhc[v][-1]:.1f}%"
                  for v in VICTIMS)
              + f", host CPU {busy:.1f}%, steal {steal:.1f}%, ping {len(got)}/{sent} replies", flush=True)
        time.sleep(PAUSE)
    refs = {v: st.median(ebpf[v]) for v in VICTIMS}
    vh_ref = {v: st.mean(vhc[v]) for v in VICTIMS}
    base_cpu = st.mean(cpus)
    for v in VICTIMS:
        if math.isnan(refs[v]) or refs[v] <= 0:
            raise SystemExit(f"reference measurement failed (rep {rep} {SCENARIO}, victim {v} {GUESTS[v][0]})")
    loss = 100.0 * (sent_all - len(rtts)) / sent_all if sent_all else 100.0
    if loss > REF_LOSS_MAX or len(rtts) < REF_RTT_MIN_N:
        raise SystemExit(f"reference ping failed (rep {rep} {SCENARIO}, {GUESTS[PING_V][0]}): {len(rtts)} replies of "
                         f"{sent_all} probes, loss {loss:.1f}% (limit {REF_LOSS_MAX:g}%, at least {REF_RTT_MIN_N} "
                         f"replies)")
    rref = {"p50": q_incl(rtts, 50), "p99": q_incl(rtts, 99), "loss": loss, "n": len(rtts)}
    bad = [f"victim {v} reached {refs[v]:.3f} Gbit/s < {REF_RATE_MIN_FRAC:g} x its offered {v_rate(v):g} Gbit/s"
           for v in VICTIMS if refs[v] < REF_RATE_MIN_FRAC * v_rate(v)]
    if SCENARIOS[SCENARIO]["lqo_victims"]:
        print(f"S3 VHOST CHECK rep {rep}: V2 vhost CPU {vh_ref['V2']:.2f} % of one core (runs "
              + " ".join(f"{x:.2f}" for x in vhc["V2"]) + f"), V1 {vh_ref['V1']:.2f} %; threshold {VH_MIN['pct']:.2f} % "
              f"({S3_VH_MIN_FRAC:g} x T{int(round(VH_MIN['frac'] * 100))} target "
              f"{VH_MIN['pct'] / S3_VH_MIN_FRAC:.2f} %): {'PASS' if vh_ref['V2'] >= VH_MIN['pct'] else 'FAIL'}",
              flush=True)
    if SCENARIOS[SCENARIO]["lqo_victims"] and not vh_ref["V2"] >= VH_MIN["pct"]:
        bad.append(f"V2's vhost CPU {vh_ref['V2']:.2f}% of one core < {VH_MIN['pct']:.2f}% ({S3_VH_MIN_FRAC:g} x the "
                   f"T{int(round(VH_MIN['frac'] * 100))} target): V2 is not clearly above the fixed target")
    new = not os.path.exists(REF_FILE) or os.path.getsize(REF_FILE) == 0
    with open(REF_FILE, "a") as f:
        if new:
            f.write("# rep, scenario, time, victim, tenant, offered_gbps, ref_gbps (median eBPF), mean host CPU %, "
                    "mean steal %, vhost CPU % of one core (mean), eBPF runs, iperf3 runs, vhost runs, rtt_p50_ms, "
                    "rtt_p99_ms, ping_loss_pct, n_rtt, rtt samples ms (pooled, receive order); both victims together "
                    f"at the scenario's rates, ping from {PING_V} every {PING_I:g} s, {REF_RUNS} x {REF_T} s, no "
                    f"flooders, at the start of each scenario block of each repeat\n")
        for v in VICTIMS:
            rt = (f"{rref['p50']:.3f}, {rref['p99']:.3f}, {loss:.2f}, {len(rtts)}, "
                  + " ".join(f"{x:.3f}" for x in rtts)) if v == PING_V else "na, na, na, na, na"
            f.write(f"{rep}, {SCENARIO}, {time.strftime('%Y-%m-%d %H:%M:%S')}, {v}, {GUESTS[v][0]}, {v_rate(v):g}, "
                    f"{refs[v]:.3f}, {base_cpu:.1f}, {nanstat(steals, st.mean):.2f}, {vh_ref[v]:.2f}, "
                    f"{' '.join(f'{x:.3f}' for x in ebpf[v])}, {' '.join(f'{x:.3f}' for x in ip[v])}, "
                    f"{' '.join(f'{x:.2f}' for x in vhc[v])}, {rt}\n")
        f.flush(); os.fsync(f.fileno())
    if bad and rep == 1:
        raise SystemExit(f"REFUSING TO CONTINUE: reference rep 1 {SCENARIO}: " + "; ".join(bad))
    return refs, base_cpu, rref, vh_ref, [f"reference {SCENARIO}: {b}" for b in bad]

# ---------- one run ----------

def run(mode, param, refs, base_cpu, procs, rref):
    # exp43's run() in the CURRENT scenario. refs = {victim: its reference Gbit/s}; base_cpu only logged; rref =
    # reference RTT. lqr_orig expects xdp_dropfrac on lqo_tenants() and xdp_limit on every other tenant (main() wraps
    # the slot in dropfrac_tenants()); every other mode xdp_limit on all six
    nan = float("nan")
    act = active()
    lqt = lqo_tenants() if mode in LQO_MODES else []
    lqv = [v for v in lqt if v in VICTIMS]   # S3: lqr_orig controls the victims too
    shared = INIT_CAP_MBIT
    if mode in VF_MODES:
        caps = [high_cap(VF_B0, 1) if g == FLOODERS[0] else CAP_MIN for g in FLOODERS]
    elif mode == "lqr":
        caps = [shared / NF] * NF
    elif mode in LQO_MODES:
        caps = [nan] * NF   # no rate caps: the actuator is the drop fraction
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
    lqo = {g: lqo_new() for g in lqt}       # lqr_orig: one controller instance per controlled tenant
    u_bp = {g: 0 for g in TENANTS}
    l_eff, l_e = {g: nan for g in TENANTS}, {g: nan for g in TENANTS}
    v_rel = nan                             # exp44: when the victims' drop was released (s after flooder launch)
    hyb = None                              # exp45: hyb state (None for every other mode)
    h_rat = {}                              # exp45: hyb ratios of the last tick ({} before the warm-up)
    h_ev = 0
    ex_t = {g: nan for g in TENANTS}        # exp45: first time g was exempt (hyb)
    ce_t = {g: nan for g in TENANTS}        # exp45: first time g's hyb target reached the ceiling
    ex_now = {g: 0.0 for g in TENANTS}      # exp45: exempt flag, updated on every hyb controller step
    if mode in LQO_MODES:
        target, K = LQO[param][:2]
        if mode == "hyb":
            hyb = hyb_new(lqt, target, target / LQO[param][2])
            slo = {g: refs.get(g, 0.0) for g in TENANTS}   # SLO reference per tenant, 0 = best effort
        for g in TENANTS:
            want = "dropfrac" if g in lqt else "limit"
            if IF[g].get("kind", "limit") != want:
                raise SystemExit(f"{mode} slot ({SCENARIO}) without {PROG[want][0]} on tenant {g} ({IF[g]['dev']})")
        for g in lqt:
            set_drop(IF[g], 0)
        for g in FLOODERS:
            if g not in lqt:
                set_cap(IF[g], 0)   # idle flooders of S1/S2 keep xdp_limit, unlimited
    else:
        target = nan
        for g in TENANTS:
            if IF[g].get("kind", "limit") != "limit":
                raise SystemExit(f"{mode} slot without xdp_limit on tenant {g} ({IF[g]['dev']})")
    # caps first, then the eBPF baseline, then the flooders: no flooder byte can precede the baseline
    if mode not in LQO_MODES:
        for g, c in zip(FLOODERS, caps): set_cap(IF[g], c)
    hist = {g: [] for g in IF}
    offered = {g: [] for g in IF}   # (t, cumulative passed + dropped bytes), same counter read as hist
    def sample():
        s0 = time.monotonic()
        for g in IF:
            c = counters(IF[g]); tn = time.monotonic()
            hist[g].append((tn, c[2])); offered[g].append((tn, c[2] + c[3]))
        return time.monotonic() - s0
    sample()
    fo = {g: tempfile.TemporaryFile("w+") for g in FLOODERS}
    vo = {v: tempfile.TemporaryFile("w+") for v in VICTIMS}
    po = tempfile.TemporaryFile("w+")      # ping output
    alone = mode == "alone"
    t0 = time.monotonic()
    fp = {} if alone else {g: ssh(g, cmd, fo[g]) for g, cmd in flooder_cmds().items()}
    time.sleep(max(0.0, t0 + OFFSET - time.monotonic()))
    vp = {v: ssh(v, cmd, vo[v]) for v, cmd in victim_cmds(T).items()}
    t_ping = time.monotonic() - t0
    pp = ssh(PING_V, ping_cmd(T), po)      # ping right after the victims, for the victim window
    procs += list(fp.values()) + list(vp.values()) + [pp]
    v0 = VICTIMS[0]
    c_prev = cpu(); cpus, steals, ticks = [], [], []
    vh_prev = vh_read(); ctl_prev = vh_prev   # vhost readings: last tick / last lqr_orig controller step
    vfirst = {v: None for v in VICTIMS}
    ts = []
    k = 1
    while any(p.poll() is None for p in vp.values()) and time.monotonic() < t0 + OFFSET + T + 30:
        time.sleep(max(0.0, t0 + OFFSET + k * DT - time.monotonic())); k += 1
        n = k - 1   # tick number 1, 2, ...
        prev = {g: hist[g][-1] for g in IF}
        prev_off = {g: offered[g][-1] for g in IF}
        samp = sample(); tnow = hist[v0][-1][0]; c_now = cpu()
        busy, steal = busy_steal(c_prev, c_now); c_prev = c_now
        vh_now = vh_read()
        vh_tick = [vh_pct(vh_prev[g], vh_now[g]) for g in FLOODERS]
        vh_vtick = [vh_pct(vh_prev[v], vh_now[v]) for v in VICTIMS]; vh_prev = vh_now
        cpus.append(busy); steals.append(steal)
        for v in VICTIMS:
            if vfirst[v] is None and hist[v][-1][1] > hist[v][0][1]:
                vfirst[v] = tnow
        tick = {g: (hist[g][-1][1] - prev[g][1]) * 8 / 1e6 / max(1e-3, hist[g][-1][0] - prev[g][0]) for g in IF}
        dt = max(1e-3, tnow - prev[v0][0])
        ticks.append(dt)
        worst = nan
        ctl = 0
        if mode == "lqr":
            # exp39: host CPU measure (busy, this tick), du = -K x, x = [e, d]; du in Gbit/s; clamp = anti-windup
            target_l, _, K_l = LQR[param]
            e = busy - target_l
            d = 0.0 if lq_e1 is None else e - lq_e1; lq_e1 = e
            du = -float((K_l @ np.array([e, d]))[0])
            shared = max(CAP_MIN, min(CAP_MAX, shared + 1000.0 * du))
            budget = shared
            caps = [shared / NF] * NF
        elif mode in VF_MODES:
            # exp39/exp40 vfrot, unchanged
            beta, add, rot_t, thresh, bmax, _ = vf_params(mode, param)
            if budget is None:
                budget = VF_B0
            if all(vfirst[v] is not None and tnow - vfirst[v] >= SMOOTH for v in VICTIMS):
                av = 1.0 - math.exp(-dt / VFROT_VS_TAU)
                for v in VICTIMS:
                    v1s = rate_mbit(hist[v], tnow - SMOOTH, tnow)
                    v_s[v] = v1s if v_s[v] is None else av * v1s + (1.0 - av) * v_s[v]
                worst = min(v_s[v] / (refs[v] * 1000.0) for v in VICTIMS)
                if worst < thresh:
                    vf_block = tnow + VFROT_HOLD
                    if tnow >= vf_cutblock:
                        budget *= beta; vf_cutblock = tnow + VFROT_CUT_HOLD
                elif tnow >= vf_block:
                    budget += add * 1000.0 * dt
            budget = max(NF * CAP_MIN, min(bmax, budget))
            act_t = active_flooders(offered, prev_off)
            pf_all = pf_update(delivered, {g: hist[g][-1][1] - prev[g][1] for g in FLOODERS}, act_t, pf_all)
            caps = pidrotf_caps(act_t, budget, tnow - t0, rot_t, caps, delivered, pf_state)
        elif mode in LQO_MODES:
            # exp44 (S3): the victims' window is over -> release their drop before their iperf3 -J report
            if lqv and math.isnan(v_rel) and tnow - t0 >= OFFSET + T:
                for v in lqv:
                    set_drop(IF[v], 0); u_bp[v] = 0
                v_rel = tnow - t0
            h_ev = 0
            ub = {g: nan for g in TENANTS}
            if hyb is not None:
                # exp45: vfrot's victim signal (EMA of the 1 s delivered rate, after every SLO tenant's warm-up) for
                # every tenant with an SLO reference; ratio +inf for best-effort tenants
                sl = [g for g in TENANTS if slo[g] > 0]
                if all(vfirst[g] is not None and tnow - vfirst[g] >= SMOOTH for g in sl):
                    av = 1.0 - math.exp(-dt / VFROT_VS_TAU)
                    for g in sl:
                        v1s = rate_mbit(hist[g], tnow - SMOOTH, tnow)
                        v_s[g] = v1s if v_s[g] is None else av * v1s + (1.0 - av) * v_s[g]
                    h_rat = {g: (v_s[g] / (slo[g] * 1000.0) if slo[g] > 0 else math.inf) for g in TENANTS}
                    worst = min(h_rat[g] for g in sl) if sl else nan
            if n % LQO_EVERY == 0:
                # every 2nd tick (LQO_DT): per controlled tenant, CPU = its vhost thread over the window since the
                # last step (same code path for flooders and, in S3, victims)
                ctl = 1
                if hyb is not None and h_rat and tnow - t0 < OFFSET + T:
                    h_ev = hyb_step(hyb, tnow - t0, h_rat, {g: l_eff[g] for g in lqt})
                    for g in lqt:
                        if math.isnan(ce_t[g]) and hyb["tgt"][g] >= hyb["ceil"] - 1e-9:
                            ce_t[g] = tnow - t0
                for g in lqt:
                    if g in lqv and not math.isnan(v_rel):
                        continue
                    c_in = vh_pct(ctl_prev[g], vh_now[g])
                    tg_ = hyb["tgt"][g] if hyb is not None else target
                    u_bp[g], l_eff[g], l_e[g] = lqo_step(lqo[g], c_in, tg_, K)
                    set_drop(IF[g], u_bp[g])
                    ub[g] = 1.0 if (u_bp[g] > 0 and c_in < tg_) else 0.0
                    if hyb is not None and u_bp[g] > 0:
                        hyb["u_pos"][g] = tnow - t0
                    if hyb is not None and tnow - t0 < OFFSET + T:
                        ex_now[g] = 1.0 if (hyb["tgt"][g] > hyb["base"] + 1e-9
                                            and tnow - t0 - hyb["u_pos"][g] >= HYB_T) else 0.0
                        if ex_now[g] and math.isnan(ex_t[g]):
                            ex_t[g] = tnow - t0
                ctl_prev = vh_now
        if mode in CAP_MODES:
            for g, c in zip(FLOODERS, caps): set_cap(IF[g], c)
        ts.append((tnow - t0, dt, samp, busy, steal,
                   *(rate_mbit(hist[v], tnow - SMOOTH, tnow) / 1000.0 for v in VICTIMS),
                   *(tick[v] / 1000.0 for v in VICTIMS),
                   *caps, *(tick[g] / 1000.0 for g in FLOODERS),
                   budget if mode in CAP_MODES else nan, worst,
                   *vh_tick, *(l_eff[g] for g in FLOODERS), *(l_e[g] for g in FLOODERS),
                   *((u_bp[g] / 10000.0 if g in lqt else nan) for g in FLOODERS), ctl,
                   *vh_vtick, *(l_eff[v] for v in VICTIMS), *(l_e[v] for v in VICTIMS),
                   *((u_bp[v] / 10000.0 if v in lqt else nan) for v in VICTIMS),
                   *((hyb["tgt"][g] if hyb is not None and g in lqt else nan) for g in TENANTS), h_ev,
                   *((ex_now[g] if hyb is not None and g in lqt else nan) for g in TENANTS),
                   *((ub[g] if mode in LQO_MODES else nan) for g in TENANTS)))
    u_release, timeouts, v_timeouts = nan, 0, 0
    if mode in LQO_MODES:
        # DOCUMENTED DEVIATION: release the drop before waiting for the iperf3 clients (their -J report / control
        # channel); exp44: the victims too if the loop ended before their release
        for g in lqt:
            set_drop(IF[g], 0)
        u_release = time.monotonic() - t0
        if lqv and math.isnan(v_rel):
            v_rel = u_release
    if lqv:
        v_timeouts = wait_all(list(vp.values()), LQO_WAIT_S)
    else:
        for p in vp.values(): p.wait()
    if mode in LQO_MODES:
        timeouts = wait_all(list(fp.values()), LQO_WAIT_S)
    else:
        for p in fp.values(): p.wait()
    pp.wait()
    # end of the eBPF window: after all iperf3 sessions have stopped, same bytes iperf3 counts
    sample()
    vres, vwins = [], {}
    for v in VICTIMS:
        wins = ([rate_mbit(hist[v], vfirst[v] + j, vfirst[v] + j + 1) / 1000.0 for j in range(int(T) - 1)]
                if vfirst[v] is not None else [])
        vwins[v] = wins
        ok = sum(w >= SLO_FRAC * refs[v] for w in wins) / len(wins) if wins else nan
        vres += [iperf_sum(vo[v], T), p5(wins), ok]
    # dip metrics on window j = min over victims of (its j-th 1 s window / its reference); window start
    # (s after the flooder launch) = the later victim's first-byte time + j
    nwin = min(len(w) for w in vwins.values())
    if nwin and all(vfirst[v] is not None for v in VICTIMS):
        ratios = [min(vwins[v][j] / refs[v] for v in VICTIMS) for j in range(nwin)]
        starts = [max(vfirst[v] for v in VICTIMS) - t0 + j for j in range(nwin)]
        dip_ovl, dip_all = dip_metrics(ratios, starts)
    else:
        dip_ovl, dip_all = [nan] * len(DIP_LEVELS), [nan] * len(DIP_LEVELS)
    po.seek(0); got, sent = parse_ping(po.read(), int(round(T / PING_I))); po.close()
    oks = vres[2::3]
    ok_worst = nan if any(math.isnan(x) for x in oks) else min(oks)
    per, off = [], []
    for g in FLOODERS:
        if alone:
            per += [0.0, 0.0]; off.append(0.0)
        elif g not in act:
            per += [nan, nan]; off.append(nan)   # exp44: not in this scenario
        else:
            per += [iperf_sum(fo[g], flooder_dur(g)),
                    (hist[g][-1][1] - hist[g][0][1]) * 8 / 1e9 / flooder_dur(g)]
            off.append((offered[g][-1][1] - offered[g][0][1]) * 8 / 1e9 / flooder_dur(g))   # passed + dropped
    for f in list(fo.values()) + list(vo.values()): f.close()
    ov = overlap()
    rates = [rate_mbit(hist[g], t0 + ov[0], t0 + ov[1]) for g in act]
    jf = nan if alone or len(act) < 2 else jain(rates)   # exp44: over the active flooders, nan with fewer than 2
    tk = ticks[1:]
    if alone:
        extras = (0.0, nan, nan, nan, nan) + (nan,) * NF
    else:
        own = [per[2 * i + 1] for i, g in enumerate(FLOODERS) if g in act]
        ceil_sh, floor_sh = budget_shares(ts, vf_params(mode, param)[4]) if mode in VF_MODES else (nan, nan)
        extras = (sum(own), min(own), min(rates) / 1000.0, ceil_sh, floor_sh,
                  *(highcap_shares(ts) if mode in VF_MODES else (nan,) * NF))
    lq = mode in LQO_MODES
    terr = own_window_stats(ts, True, TS_E) if lq else [nan] * NF
    news = (*own_window_stats(ts, False, TS_VH), *terr, nanstat(terr, st.mean),
            *(own_window_stats(ts, True, TS_U) if lq else [nan] * NF), target, u_release, float(timeouts))
    # V2 latency p50/p99 of the received replies, loss, RTT SLO, SLO in both dimensions (as exp43)
    p50, p99, loss, _ = rtt_stats(got, sent)
    ok_rtt = rtt_ok(p99, rref["p99"], loss)
    # exp44 victim columns
    late = (sum(1 for v in VICTIMS if vfirst[v] is not None and vfirst[v] - t0 + (int(T) - 1) > v_rel + 1e-9)
            if not math.isnan(v_rel) else 0)
    x44 = (*((hist[v][-1][1] - hist[v][0][1]) * 8 / 1e9 / T for v in VICTIMS),
           *victim_window_stats(ts, False, TS_VVH),
           *(victim_window_stats(ts, True, TS_VE) if lqv else [nan] * NV),
           *(victim_window_stats(ts, True, TS_VU) if lqv else [nan] * NV),
           float(0 if alone else len(act)), *(v_rate(v) for v in VICTIMS), v_rel, float(v_timeouts), float(late))
    pt = ping_ticks([row[0] for row in ts], t_ping, got, sent)
    ts = [row + x for row, x in zip(ts, pt)]
    x43 = (*off, *dip_ovl, *dip_all, p50, p99, loss, ok_rtt, ok_worst * ok_rtt)
    # exp45 hyb columns: mean target per tenant over its window (controller steps; victims: the victim window,
    # flooders: their own window), raise / cut counts, first raise (s), lowest best-effort target of the run
    if hyb is not None:
        tv = victim_window_stats(ts, True, TS_TGT)
        tf = own_window_stats(ts, True, TS_TGT + NV)
        be = [row[TS_TGT + i] for row in ts for i, g in enumerate(TENANTS)
              if g in lqt and refs.get(g, 0.0) <= 0 and row[TS_CTL] == 1]
        x45 = (*tv, *tf, float(hyb["raises"]), float(hyb["cuts"]), hyb["first_raise"], nanstat(be, min))
    else:
        x45 = (nan,) * 10
    x45 += tuple(ex_t[g] for g in TENANTS) + tuple(ce_t[g] for g in TENANTS)
    if lq:
        x45 += (*victim_window_stats(ts, True, TS_UB), *own_window_stats(ts, True, TS_UB + NV))
    else:
        x45 += (nan,) * len(TENANTS)
    return (*vres, ok_worst, *per, jf,
            nanstat(cpus, st.mean), nanstat(cpus, max), nanstat(steals, st.mean), nanstat(steals, max),
            nanstat(tk, st.mean), nanstat(tk, max), *extras, *news, *x43, *x44, *x45), ts

def ms(xs):
    xs = [x for x in xs if not math.isnan(x)]
    if not xs: return float("nan"), float("nan")
    return st.mean(xs), (st.stdev(xs) if len(xs) > 1 else 0.0)

def slots_per_repeat():
    # per scenario: one alone slot + its configs
    return sum(1 + len(SCEN_CONFIGS[s]) for s in SCENARIOS)

def swap_vnets_per_repeat():
    # vnets swapped to xdp_dropfrac and back per repeat (lqr_orig slots x tenants it controls in that scenario)
    return sum((len(SCENARIOS[s]["win"]) + (NV if SCENARIOS[s]["lqo_victims"] else 0))
               * sum(m in LQO_MODES for m, _ in SCEN_CONFIGS[s]) for s in SCENARIOS)

def expected_duration(reps):
    # nominal: per slot OFFSET + T + ~2.5 s ssh/iperf3 overhead + PAUSE; per scenario one reference block; swaps;
    # between repeats COOLDOWN + SESSION_REST_S. Returns (slots per repeat, s per repeat, s total)
    n_slots = slots_per_repeat()
    per_rep = (len(SCENARIOS) * REF_RUNS * (REF_T + 1 + PAUSE) + n_slots * (OFFSET + T + 2.5 + PAUSE)
               + swap_vnets_per_repeat() * SWAP_S_VNET)
    return n_slots, per_rep, reps * per_rep + (reps - 1) * (SESSION_REST_S + COOLDOWN)

def measured_duration(reps):
    # at exp43's measured rates (SLOT_S_MEASURED per slot, REF_S_MEASURED per reference block)
    n_slots = slots_per_repeat()
    per_rep = len(SCENARIOS) * REF_S_MEASURED + n_slots * SLOT_S_MEASURED + swap_vnets_per_repeat() * SWAP_S_VNET
    return n_slots, per_rep, reps * per_rep + (reps - 1) * (SESSION_REST_S + COOLDOWN)

def csv_line(rep, scen, slot, m, p, r, refs, rref, vh_ref):
    vr, okw, fl, tail, ex, nw, x3, x4, x5 = (r[:R_OKW], r[R_OKW], r[R_FL:R_TAIL], r[R_TAIL:R_X], r[R_X:R_N],
                                             r[R_N:R_M], r[R_M:R_X44], r[R_X44:R_X45], r[R_X45:])
    return (f"{rep},{scen},{slot},{m},{p}," + "".join(f"{x:.3f}," for x in vr) + f"{okw:.3f},"
            + "".join(f"{x:.3f}," for x in fl)
            + f"{tail[0]:.4f}," + "".join(f"{x:.2f}," for x in tail[1:5])
            + "".join(f"{x:.4f}," for x in tail[5:7])
            + "".join(f"{refs[v]:.3f}," for v in VICTIMS)
            + ",".join([f"{x:.3f}" for x in ex[:3]] + [f"{x:.4f}" for x in ex[3:]]) + ","
            + ",".join(f"{x:.3f}" for x in nw[:-1]) + f",{int(nw[-1])},"
            + ",".join(f"{x:.3f}" for x in x3[:NF]) + ","
            + ",".join(f"{x:.4f}" for x in x3[NF:NF + len(DIP_NAMES)]) + ","
            + ",".join(f"{x:.3f}" for x in x3[NF + len(DIP_NAMES):]) + ","
            + ",".join(f"{x:.3f}" for x in x4) + ","
            + f"{rref['p50']:.3f},{rref['p99']:.3f}," + ",".join(f"{vh_ref[v]:.2f}" for v in VICTIMS) + ","
            + ",".join(f"{x:.3f}" for x in x5) + "\n")

def ts_line(rep, scen, slot, m, p, row):
    return (f"{rep},{scen},{slot},{m},{p},{row[0]:.3f},{row[1]:.4f},{row[2]:.4f},{row[3]:.2f},{row[4]:.2f},"
            + "".join(f"{x:.3f}," for x in row[5:TS_CAP])
            + "".join(f"{c:.1f}," for c in row[TS_CAP:TS_RATE])
            + "".join(f"{x:.3f}," for x in row[TS_RATE:TS_BUDGET])
            + f"{row[TS_BUDGET]:.1f},{row[TS_BUDGET + 1]:.4f},"
            + "".join(f"{x:.3f}," for x in row[TS_VH:TS_U])
            + "".join(f"{x:.2f}," for x in row[TS_U:TS_CTL]) + f"{int(row[TS_CTL])},"
            + "".join(f"{x:.3f}," for x in row[TS_VVH:TS_VU])
            + "".join(f"{x:.2f}," for x in row[TS_VU:TS_TGT])
            + "".join(f"{x:.3f}," for x in row[TS_TGT:TS_HEV]) + f"{int(row[TS_HEV])},"
            + "".join(f"{x:.0f}," for x in row[TS_EX:TS_RTT])
            + f"{row[TS_RTT]:.3f},{int(row[TS_RTT + 1])}\n")

# ---------- summary (also offline: python3 exp45.py --summary [results_dyn45.csv]) ----------

def _betacf(a, b, x):
    # continued fraction of the incomplete beta function (modified Lentz), as exp41
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
    if x <= 0.0: return 0.0
    if x >= 1.0: return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b

def paired_p(ds):
    # two-sided paired t-test p-value (as exp41); n < 2: nan; sd 0: 1.0 if the mean is 0, else 0.0
    n = len(ds)
    if n < 2:
        return float("nan")
    m, sd = st.mean(ds), st.stdev(ds)
    if sd <= 1e-12:
        return 1.0 if abs(m) <= 1e-12 else 0.0
    t = m / (sd / math.sqrt(n)); df = n - 1
    return betainc(df / 2.0, 0.5, df / (df + t * t))

def summary_data(path):
    # per (scenario, mode, param) (alone excluded): {metric: {rep: value}}; paired differences per scenario against
    # (scenario, PAIRED_REF); alone: {scenario: list of its RTT / victim columns}
    vals, alone = {}, {}
    with open(path) as f:
        for row in csvmod.DictReader(f):
            s = row["scenario"]
            if row["mode"] == "alone":
                alone.setdefault(s, []).append({m: float(row[m]) for m in (
                    "slo_ok_worst", "slo_ok_v2_rtt", "v2_rtt_p50_ms", "v2_rtt_p99_ms", "v2_loss_pct", "cpu_mean")})
                continue
            c = (s, row["mode"], row["param"])
            d = vals.setdefault(c, {m: {} for m, _, _ in SUMMARY_METRICS})
            for m, _, _ in SUMMARY_METRICS:
                d[m][int(row["rep"])] = float(row[m])
    known = [(s, m, p) for s in SCENARIOS for m, p in SCEN_CONFIGS[s]]
    order = [c for c in known if c in vals] + sorted(c for c in vals if c not in known)
    stats = {c: {m: (*ms(list(vals[c][m].values())), len(vals[c][m])) for m, _, _ in SUMMARY_METRICS} for c in order}
    diffs = {}
    for s in dict.fromkeys(c[0] for c in order):
        ref = (s, *PAIRED_REF)
        if ref not in vals:
            continue
        diffs[s] = {}
        for c in order:
            if c[0] != s or c == ref:
                continue
            diffs[s][c] = {}
            for m, _, better in SUMMARY_METRICS:
                ds = [vals[c][m][r] - vals[ref][m][r] for r in sorted(vals[c][m])
                      if r in vals[ref][m] and not math.isnan(vals[c][m][r]) and not math.isnan(vals[ref][m][r])]
                hi = sum(d > 1e-9 for d in ds); lo = sum(d < -1e-9 for d in ds)
                wtl = (hi, len(ds) - hi - lo, lo) if better else (lo, len(ds) - hi - lo, hi)
                sd0 = len(ds) > 1 and st.stdev(ds) <= 1e-12 and abs(st.mean(ds)) > 1e-12
                diffs[s][c][m] = (*ms(ds), len(ds), wtl, paired_p(ds), sd0)
    return order, stats, diffs, alone

def summarize(path):
    order, stats, diffs, alone = summary_data(path)
    out = [f"SUMMARY45 from {os.path.basename(path)} (mean +- sd over repeats; alone excluded; ok_w_rtt = "
           f"slo_ok_worst x slo_ok_v2_rtt; rtt_p99 ms over received replies; fl_X = flooder X's eBPF Gbit/s over its "
           f"own window (nan: not in the scenario); jain over the active flooders (nan in S1); v1_gbps / v2_gbps = "
           f"victim eBPF Gbit/s over T; host_cpu = mean host CPU %; dipXX_ovl / dipXX_all = fraction of 1 s windows "
           f"with min(V1, V2) / ref < 0.XX inside the all-flooders-active window / over the whole run; trk_err = "
           f"mean |e| of lqr_orig / hyb over the controlled flooders, nan for other modes)",
           f"{'scen':4s} {'mode':8s} {'param':>14s} {'n':>3s} " + " ".join(f"{s:>15s}" for _, s, _ in SUMMARY_METRICS)]
    for c in order:
        n = max(v[2] for v in stats[c].values())
        out.append(f"{c[0]:4s} {c[1]:8s} {c[2]:>14s} {n:3d} "
                   + " ".join(f"{stats[c][m][0]:6.3f} +- {stats[c][m][1]:5.3f}" for m, _, _ in SUMMARY_METRICS))
    for s in dict.fromkeys(c[0] for c in order):
        if s not in diffs:
            out.append(f"({s}: reference config {PAIRED_REF[0]} {PAIRED_REF[1]} not in the file: no paired "
                       f"differences)")
            continue
        out.append(f"PAIRED {s} vs {PAIRED_REF[0]} {PAIRED_REF[1]} (per repeat: config - reference, mean +- sd, W/T/L "
                   f"= better/equal/worse than the reference in the same repeat (rtt_p99, host_cpu, dips and trk_err: "
                   f"lower is better, all others higher), p = two-sided paired t-test; nan where a value is missing)")
        for m, short, _ in SUMMARY_METRICS:
            for c, d in diffs[s].items():
                x = d[m]
                if x[2] == 0:
                    continue   # metric undefined for both (e.g. fl_C in S1, jain in S1)
                out.append(f"  {short:>9s} {c[1]:8s} {c[2]:>14s} n={x[2]:d} {x[0]:+8.3f} +- {x[1]:6.3f} "
                           f"W/T/L {x[3][0]}/{x[3][1]}/{x[3][2]} p={x[4]:.3f}" + (" (sd0)" if x[5] else ""))
    out.append("TABLE scenario x controller (means over repeats; fl_total = sum over the active flooders, Gbit/s; "
               "per-flooder A/B/C/D Gbit/s; SLO = slo_ok_worst, SLO+RTT = slo_ok_worst_with_rtt; V1 / V2 = victim "
               "eBPF Gbit/s; host CPU %)")
    out.append(f"{'scen':4s} {'mode':8s} {'param':>14s} {'fl_total':>8s} {'A/B/C/D':>23s} {'SLO':>6s} {'SLO+RTT':>7s} "
               f"{'V1':>6s} {'V2':>6s} {'jain':>6s} {'rtt_p99':>7s} {'cpu':>5s}")
    for c in order:
        S = lambda m: stats[c][m][0]
        per = "/".join("-" if math.isnan(S(f"flooder{g}_ebpf_gbps")) else f"{S(f'flooder{g}_ebpf_gbps'):.2f}"
                       for g in FLOODERS)
        out.append(f"{c[0]:4s} {c[1]:8s} {c[2]:>14s} {S('flooder_total_gbps'):8.2f} {per:>23s} "
                   f"{S('slo_ok_worst'):6.3f} {S('slo_ok_worst_with_rtt'):7.3f} {S('victim1_ebpf_gbps'):6.2f} "
                   f"{S('victim2_ebpf_gbps'):6.2f} {S('jain_fairness'):6.3f} {S('v2_rtt_p99_ms'):7.3f} "
                   f"{S('cpu_mean'):5.1f}")
    for s, al in alone.items():
        ok = [a["slo_ok_v2_rtt"] for a in al if not math.isnan(a["slo_ok_v2_rtt"])]
        p50 = ms([a["v2_rtt_p50_ms"] for a in al]); p99 = ms([a["v2_rtt_p99_ms"] for a in al])
        lo = ms([a["v2_loss_pct"] for a in al]); sl = ms([a["slo_ok_worst"] for a in al])
        cp = ms([a["cpu_mean"] for a in al])
        out.append(f"ALONE {s}: {len(al)} slots, slo_ok_worst {sl[0]:.3f} +- {sl[1]:.3f}, RTT SLO passed in "
                   f"{int(sum(ok))}/{len(ok)} (false-alarm rate of the RTT SLO with no flooders "
                   f"{(1 - st.mean(ok)) if ok else float('nan'):.3f}), rtt_p50 {p50[0]:.3f} +- {p50[1]:.3f} ms, rtt_p99 "
                   f"{p99[0]:.3f} +- {p99[1]:.3f} ms, loss {lo[0]:.2f} +- {lo[1]:.2f} %, host CPU {cp[0]:.1f} +- "
                   f"{cp[1]:.1f} %")
    return "\n".join(out)

def setup_configs(configs, calib):
    # validate every config and fill VFROT / LQR / LQO; labels (mode, param) must be unique
    if len(set(configs)) != len(configs):
        raise SystemExit(f"duplicate config label(s) in {configs}")
    for m, p in configs:
        if m not in MODES:
            raise SystemExit(f"mode {m!r} is not supported by exp45 (only {', '.join(MODES)})")
        if m == "lqr":
            target, rwt = parse_lqr(p)
            LQR[p] = (target, rwt, lqr_gain(rwt))
        elif m == "lqr_orig":
            LQO[p] = setup_lqo(p, calib)
        elif m == "hyb":
            parse_hyb(p)
            LQO[p] = setup_lqo(p, calib)   # same (target, K, fraction, kind) as lqr_orig with this param
        else:
            VFROT[(m, p)] = parse_vfrot(m, p)

def all_configs():
    # distinct (mode, param) over all scenarios, in first-use order
    return list(dict.fromkeys(c for s in SCENARIOS for c in SCEN_CONFIGS[s]))

def scen_order(rep):
    k = (rep - 1) % len(SCEN_ORDER)
    return list(SCEN_ORDER[k:]) + list(SCEN_ORDER[:k])

def check_static():
    # consistency of the scenario tables (before anything runs)
    if LQO_EVERY * DT != LQO_DT:
        raise SystemExit(f"LQO_EVERY x DT = {LQO_EVERY * DT:g} != LQO_DT {LQO_DT:g}")
    if sorted(SCEN_ORDER) != sorted(SCENARIOS) or sorted(SCEN_CONFIGS) != sorted(SCENARIOS):
        raise SystemExit(f"SCEN_ORDER {SCEN_ORDER} / SCEN_CONFIGS {sorted(SCEN_CONFIGS)} != SCENARIOS {sorted(SCENARIOS)}")
    for s, d in SCENARIOS.items():
        if not d["win"] or any(g not in FLOODERS for g in d["win"]) or sorted(d["v_rate"]) != sorted(VICTIMS):
            raise SystemExit(f"scenario {s}: bad flooder windows {d['win']} or victim rates {d['v_rate']}")
        for g, (a, n) in d["win"].items():
            if a < 0 or n <= 0 or a + n > T:
                raise SystemExit(f"scenario {s}: flooder {g} window {a}-{a + n} s outside 0-{T} s")
        if overlap_of(d["win"])[0] >= overlap_of(d["win"])[1]:
            raise SystemExit(f"scenario {s}: the flooders are never all active together")
        if PAIRED_REF not in SCEN_CONFIGS[s]:
            raise SystemExit(f"scenario {s}: paired reference {PAIRED_REF} not among its configs")
    if not (0 < HYB_BETA < 1 and 0.85 <= HYB_THRESH < 1 and 0 < HYB_FLOOR_FRAC < HYB_CEIL_FRAC and HYB_T > 0
            and HYB_CUT_HOLD > 0 and HYB_ADD_FRAC > 0 and HYB_GUARD_FRAC >= 0):
        raise SystemExit("hyb constants out of range")

def overlap_of(w):
    return (max(w[g][0] for g in w), min(w[g][0] + w[g][1] for g in w))

def main():
    random.seed(7)
    configs = all_configs()
    check_static()
    ALONE = ("alone", "na")
    res = {}
    # never append to or overwrite earlier output (exp45.log only as this process's own stdout from run45.sh)
    try:
        own_log = os.path.exists(LOG) and os.path.samestat(os.fstat(sys.stdout.fileno()), os.stat(LOG))
    except (OSError, ValueError, AttributeError):
        own_log = False
    existing = [f for f in (CSV, TS_CSV, REF_FILE) if os.path.exists(f)]
    if os.path.exists(LOG) and not own_log:
        existing.append(LOG)
    if existing:
        raise SystemExit("REFUSING TO START: output file(s) already exist: " + ", ".join(existing)
                         + " -- move or delete them first (exp45 never appends)")
    nproc, ncpu = host_cpus()
    if nproc != NPROC_REQUIRED:
        raise SystemExit(f"REFUSING TO START: nproc = {nproc} (os.cpu_count() = {ncpu}), exp45 requires exactly "
                         f"{NPROC_REQUIRED}: it continues exp39-exp44 on the 6 vCPU host VM and is only comparable there")
    # FAIL FAST (before sudo, attach or any traffic): calibration present and not FAIL, every lqr_orig gain with
    # K[0] < 0 (setup_lqo -> check_gain_sign); the reference checks follow in rep 1's S3 reference, before any slot
    calib = load_calib()
    setup_configs(configs, calib)
    VH_MIN["pct"] = S3_VH_MIN_FRAC * VH_MIN["frac"] * float(calib["cpu_ref"])
    try:
        sudo(["true"])
    except subprocess.CalledProcessError as e:
        raise SystemExit(f"sudo needs a password: run 'sudo -v' first (run45.sh does this) [{e}]")
    for o in (OBJ, OBJ_DF):
        if not os.path.exists(o):
            raise SystemExit(f"{o} not found")
    check_servers()
    check_guests()
    resolve_vhost()
    for s in SCENARIOS:
        set_scenario(s)
        w = win()
        print(f"SCENARIO {s}: " + ", ".join(
                  f"flooder {g} ({GUESTS[g][0]}) t={w[g][0]}-{sum(w[g])} s ({PAR[g]} streams)" for g in active())
              + f"; all active {overlap()[0]:g}-{overlap()[1]:g} s (Jain / dip_ovl window); victims "
              + ", ".join(f"{v} {v_rate(v):g} Gbit/s" for v in VICTIMS)
              + f"; lqr_orig / hyb on {', '.join(lqo_tenants())}"
              + ("" if not SCENARIOS[s]["lqo_victims"] else " (ALL SIX: victims too, as the authors' design)")
              + "; configs " + ", ".join(f"{m} {p}" for m, p in SCEN_CONFIGS[s]), flush=True)
        for g, cmd in flooder_cmds().items():
            print(f"FLOODER CMD {s} {g}: ssh alpine@{GUESTS[g][1]} '{cmd}'", flush=True)
        for v, cmd in victim_cmds(T).items():
            print(f"VICTIM CMD {s} {v}: ssh alpine@{GUESTS[v][1]} '{cmd}'", flush=True)
    print(f"SCENARIO ORDER: rep r runs {'/'.join(SCEN_ORDER)} rotated left by (r - 1) mod {len(SCEN_ORDER)}; per "
          f"scenario: reference ({REF_RUNS} x {REF_T} s), one alone slot, then its configs shuffled (seed 7)",
          flush=True)
    print(f"PAIRED REFERENCE per scenario: {PAIRED_REF[0]} {PAIRED_REF[1]} (vfrot = exp39-exp44 code path; lqr_orig "
          f"= exp43/exp44 code path; hyb = lqr_orig + exp45's outer loop on its targets)", flush=True)
    for (m, p), x in VFROT.items():
        print(f"VFROT {p}: beta {x[0]:g}, add {x[1]:g} Gbit/s per s, rotation {x[2]:g} s, thresh {x[3]:g}, budget "
              f"ceiling (BMAX) {x[4]:g} Mbit/s, floor {NF * CAP_MIN:g}", flush=True)
    print(f"CALIBRATION {os.path.basename(CALIB)}: verdict {calib['verdict']}, cpu_ref {float(calib['cpu_ref']):.2f} % "
          f"of one core (median unregulated per-flooder vhost CPU, all four active), tau {float(calib['tau']):.3f} s, "
          f"b {float(calib['b']):.3f}, written {calib.get('written', '?')} (no new calibration)", flush=True)
    for c in calib.get("checks", []):
        print(f"CALIBRATION CHECK {c[0]}: {c[1]} -- {c[2]}", flush=True)
    for p in LQO:
        print(lqo_report(p, calib), flush=True)
    for m, p in configs:
        if m == "hyb":
            print(hyb_report(p, calib), flush=True)
            print(f"HYB NOTE: the ceiling {HYB_CEIL_FRAC:g} x cpu_ref ({HYB_CEIL_FRAC * float(calib['cpu_ref']):.1f} % of "
                  f"one core) is above the ~217 % needed to stop lqr_orig's 0.95 / 0 flip-flop on a victim (exp44: victims' u "
                  f"alternated on 100 % of S3 controller steps); a tenant whose target was raised and that has had no drop of "
                  f"ours for {HYB_T:g} s is flagged EXEMPT -- hyb then has learned to exempt it from lqr_orig, it is not "
                  f"tracking a CPU target; a tenant at the ceiling that is not exempt is still in the flip-flop",
                  flush=True)
            print("HYB NOTE: rule (b) is expected to be NEARLY A NO-OP: lqr_orig flip-flops, CPU_eff goes negative on the "
                  "low half of the cycle, so u = 0 on every second step whatever the target; lowering a flooder's target "
                  "cannot raise its duty cycle (exp44 S3: T40 vs T60 flooder totals 59.7 vs 60.1 Gbit/s). Cuts are "
                  "logged (hyb_cuts, hyb_min_be_tgt), no effect on u is expected", flush=True)
    print(f"LQR_ORIG (faithful port, exp43 code): per controlled tenant, dt {LQO_DT:g} s = every {LQO_EVERY}nd "
          f"{DT:g} s tick, CPU = its vhost thread % of one core over that 0.5 s window (the SAME schedstat path for "
          f"flooders and victims), median of last {LQO_MED_N}, CPU_eff = {1 - LQO_TREND_W:g} smooth + "
          f"{LQO_TREND_W:g} (smooth + trend), u = -K [e, edot], soft limit, clip [0, {LQO_UMAX:g}], nearest of "
          f"{len(LQO_LEVELS_BP)} levels; actuator xdp_dropfrac.o swapped in on the controlled tenants for the slot "
          f"and xdp_limit back on all six after it; PPS feed-forward and management-port exemption NOT enabled",
          flush=True)
    print(f"DEVIATION (lqr_orig and hyb, documented): drop_bp -> 0 on every controlled tenant BEFORE waiting for the "
          f"iperf3 clients, {LQO_WAIT_S:g} s timeout + WARNING; S3: the victims' drop is released at t >= "
          f"{OFFSET + T:g} s (end of the victim window, column victim_release_s; victim_win_after_release counts "
          f"victims whose last SLO window ends after it)", flush=True)
    print(f"S3 CHECK: V2's vhost CPU in each S3 reference must be >= {VH_MIN['pct']:.2f} % of one core "
          f"({S3_VH_MIN_FRAC:g} x the T{int(round(VH_MIN['frac'] * 100))} target "
          f"{VH_MIN['frac'] * float(calib['cpu_ref']):.2f} %) and every victim >= {REF_RATE_MIN_FRAC:g} x its offered "
          f"rate (rep 1: refuse; later: WARNING)", flush=True)
    for p, (target, rwt, K) in LQR.items():
        for line in lqr_report(p, rwt, K):
            print(line, flush=True)
    print(f"TOKEN BUCKET: vfrot and lqr use xdp_limit.o on every flooder interface (burst {XDP_BURST_NS / 1e6:g} ms); "
          f"victim interfaces xdp_limit, never limited (counter only), except under S3's lqr_orig", flush=True)
    print(f"VICTIMS: " + ", ".join(f"{v} = {GUESTS[v][0]} {GUESTS[v][1]} port {GUESTS[v][2]}" for v in VICTIMS)
          + f", one stream each at the scenario's rate, constant for [0,{T}) s (launched together {OFFSET:g} s after "
          f"the flooders); slo_ok per victim = fraction of its {int(T) - 1} 1 s windows >= {SLO_FRAC * 100:g}% of its "
          f"own per-repeat, per-scenario reference; slo_ok_worst = the smaller of the two", flush=True)
    print(f"LATENCY: {GUESTS[PING_V][0]} pings {HOST} every {PING_I:g} s during each run and reference run; "
          f"slo_ok_v2_rtt = 1 iff p99 <= max({RTT_FRAC:g} x reference p99, {RTT_FLOOR_MS:g} ms) AND loss <= "
          f"{RTT_LOSS_MAX:g}%; slo_ok_worst_with_rtt = slo_ok_worst x slo_ok_v2_rtt; no controller sees the RTT",
          flush=True)
    print(f"PING CMD V2: ssh alpine@{GUESTS[PING_V][1]} '{ping_cmd(T)}' (reference: '{ping_cmd(REF_T)}')", flush=True)
    print(f"DIPS: as exp43; dipXX_ovl over the windows inside the scenario's all-flooders-active window", flush=True)
    print(f"METRICS: as exp43 (per-flooder columns nan for a flooder not in the scenario; flooder_total / min_own / "
          f"Jain over the active flooders, Jain nan with one), plus victimN_ebpf_gbps, vhostV1/V2_cpu_mean, "
          f"track_errV1/V2 and uV1/V2_mean (S3 lqr_orig), n_active_flooders, vN_rate_gbps, victim_release_s, "
          f"victim_wait_timeouts, victim_win_after_release, ref_vhostV1/V2_cpu", flush=True)
    n_slots, per_rep, total = expected_duration(REPS)
    _, per_rep_m, total_m = measured_duration(REPS)
    print(f"EXPECTED DURATION: {n_slots} slots/repeat + {len(SCENARIOS)} reference blocks, ~{per_rep_m / 60:.1f} "
          f"min/repeat at exp43's measured rate (nominal ~{per_rep / 60:.1f}), budget {BUDGET_MIN_PER_REP:g} min/repeat: "
          f"{'fits' if per_rep_m <= BUDGET_MIN_PER_REP * 60 else 'DOES NOT FIT'}; {REPS} repeats + {REPS - 1} x "
          f"({SESSION_REST_S} + {COOLDOWN}) s rest ~ {total_m / 3600:.2f} h (nominal {total / 3600:.2f} h)", flush=True)
    procs = []
    ref_hist, warnings = [], []
    csv = open(CSV, "a")
    tsf = open(TS_CSV, "a")
    try:
        for g in TENANTS:
            IF[g] = attach(vnet(GUESTS[g][0]), 0)
        devs = [IF[g]["dev"] for g in IF]
        if len(set(devs)) != len(devs):
            raise SystemExit(f"two guests share a host interface: {devs}")
        print("INTERFACES: " + ", ".join(f"{g} = {GUESTS[g][0]} {IF[g]['dev']}" for g in IF), flush=True)
        csv.write(HEADER + "\n"); csv.flush()
        tsf.write(TS_HEADER + "\n"); tsf.flush()
        print(f"{REPS} repeats x {n_slots} slots, writing {CSV} and {TS_CSV}; {SESSION_REST_S} s rest after every "
              f"repeat except the last", flush=True)
        rested = False
        for rep in range(1, REPS + 1):
            if rep > 1 and not rested:
                print(f"cool-down {COOLDOWN} s before repeat {rep}", flush=True)
                time.sleep(COOLDOWN)
            rested = False
            slot = 0
            for s in scen_order(rep):
                set_scenario(s)
                refs, base_cpu, rref, vh_ref, rw = reference(rep, procs)
                ref_hist.append((rep, s, refs, base_cpu, rref, vh_ref))
                for txt in rw:
                    warnings.append((rep, -1, s, "reference", txt))
                    print(f"WARNING: rep {rep} {txt}", flush=True)
                print(f"REFERENCE rep {rep} {s}: " + ", ".join(
                          f"{v} ({GUESTS[v][0]}, offered {v_rate(v):g}) {refs[v]:.3f} Gbit/s (slo_ok threshold "
                          f"{SLO_FRAC * refs[v]:.3f}), vhost {vh_ref[v]:.1f}%" for v in VICTIMS)
                      + f", base_cpu {base_cpu:.1f}%; RTT {PING_V} p50 {rref['p50']:.3f} ms, p99 {rref['p99']:.3f} ms "
                      f"(RTT SLO threshold {rtt_thresh(rref['p99']):.3f} ms), loss {rref['loss']:.2f}% of "
                      f"{rref['n']} replies", flush=True)
                print("rep scen slot mode           param "
                      + "".join(f"{v}_mean {v}_p5 {v}_ok " for v in VICTIMS) + "ok_worst "
                      + "".join(f"{g}_ebpf " for g in FLOODERS)
                      + " jain cpu_mean cpu_max tick_m  fl_tot min_own V1_ebpf V2_ebpf vhV1 vhV2 rtt_p99 loss% okw_rtt "
                      "d95o d90o d80o", flush=True)
                order = SCEN_CONFIGS[s][:]; random.shuffle(order)
                for (m, p) in [ALONE] + order:
                    for g in TENANTS:
                        vhost_ns(VH[g])   # refuse here if a guest restarted since the start
                    if m in LQO_MODES:
                        lt = lqo_tenants()
                        with dropfrac_tenants(lt):
                            print(f"XDP: xdp_dropfrac on " + ", ".join(
                                f"{g} {IF[g]['dev']} (prog {IF[g]['prog']}, cfg {IF[g]['cfg']}, cnt {IF[g]['cnt']})"
                                for g in lt), flush=True)
                            r, ts = run(m, p, refs, base_cpu, procs, rref)
                        print("XDP: xdp_limit back on " + ", ".join(f"{g} {IF[g]['dev']} (prog {IF[g]['prog']})"
                                                                   for g in TENANTS), flush=True)
                    else:
                        r, ts = run(m, p, refs, base_cpu, procs, rref)
                    res.setdefault((s, m, p), []).append(r)
                    vr, okw, fl, tail, ex, nw, x3, x4 = (r[:R_OKW], r[R_OKW], r[R_FL:R_TAIL], r[R_TAIL:R_X],
                                                         r[R_X:R_N], r[R_N:R_M], r[R_M:R_X44], r[R_X44:R_X45])
                    csv.write(csv_line(rep, s, slot, m, p, r, refs, rref, vh_ref)); csv.flush(); os.fsync(csv.fileno())
                    tsf.write("".join(ts_line(rep, s, slot, m, p, row) for row in ts)); tsf.flush()
                    os.fsync(tsf.fileno())
                    X = lambda n: r[RESULT_NAMES.index(n)]
                    print(f"{rep:3d} {s:4s} {slot:4d} {m:8s} {p:>14s} "
                          + "".join(f"{a:7.2f} {b:5.2f} {c:5.2f} " for a, b, c in zip(vr[0::3], vr[1::3], vr[2::3]))
                          + f"{okw:8.2f} " + "".join(f"{b:6.2f} " for b in fl[1::2])
                          + f"{tail[0]:5.3f} {tail[1]:8.1f} {tail[2]:7.1f} {tail[5]:6.3f} {ex[0]:7.2f} {ex[1]:7.2f} "
                          + f"{X('victim1_ebpf_gbps'):7.2f} {X('victim2_ebpf_gbps'):7.2f} {X('vhostV1_cpu_mean'):5.1f} "
                          + f"{X('vhostV2_cpu_mean'):5.1f} {X('v2_rtt_p99_ms'):7.3f} {X('v2_loss_pct'):5.2f} "
                          + f"{X('slo_ok_worst_with_rtt'):7.2f} " + " ".join(f"{x:4.2f}" for x in x3[NF:NF + 3]),
                          flush=True)
                    print(f"VHOST rep {rep} {s} slot {slot} {m} {p}: mean vhost CPU (% of one core) flooders in own "
                          f"windows " + " ".join(f"{g} {x:.1f}" for g, x in zip(FLOODERS, nw[:NF]))
                          + f", victims V1 {X('vhostV1_cpu_mean'):.1f} V2 {X('vhostV2_cpu_mean'):.1f}", flush=True)
                    if m in LQO_MODES:
                        print(f"{m.upper()} rep {rep} {s} slot {slot} {p}: target {nw[3 * NF + 1]:.2f}, |e| "
                              + " ".join(f"{g} {x:.2f}" for g, x in zip(FLOODERS, nw[NF:2 * NF]))
                              + f" (mean {nw[2 * NF]:.2f}), mean u "
                              + " ".join(f"{g} {x:.3f}" for g, x in zip(FLOODERS, nw[2 * NF + 1:3 * NF + 1]))
                              + (f"; victims |e| V1 {X('track_errV1'):.2f} V2 {X('track_errV2'):.2f}, mean u V1 "
                                 f"{X('uV1_mean'):.3f} V2 {X('uV2_mean'):.3f}, released at {X('victim_release_s'):.2f} s"
                                 if SCENARIOS[s]["lqo_victims"] else "")
                              + f", u released at {nw[-2]:.2f} s", flush=True)
                    if m == "hyb":
                        print(f"HYB rep {rep} {s} slot {slot} {p}: mean target " + " ".join(
                                  f"{g} {X(f'tgt{g}_mean'):.1f}" for g in TENANTS)
                              + f", raises {int(X('hyb_raises'))} (first at {X('hyb_first_raise_s'):.2f} s), cuts "
                              f"{int(X('hyb_cuts'))} (rule (b), expected near no-op), lowest best-effort target "
                              f"{X('hyb_min_be_tgt'):.2f}", flush=True)
                        for g in TENANTS:
                            if not math.isnan(X(f"exempt{g}_s")):
                                print(f"HYB EXEMPT rep {rep} {s} slot {slot}: tenant {g} exempt from {X(f'exempt{g}_s'):.2f} s "
                                      f"(target raised, no drop of ours for {HYB_T:g} s: victim exempt from lqr_orig)",
                                      flush=True)
                            if not math.isnan(X(f"ceil{g}_s")):
                                print(f"HYB CEILING rep {rep} {s} slot {slot}: tenant {g} target reached the ceiling at "
                                      f"{X(f'ceil{g}_s'):.2f} s" + ("" if not math.isnan(X(f"exempt{g}_s")) else
                                      " and it was NEVER exempt (still in lqr_orig's flip-flop at the ceiling)"), flush=True)
                    if m in LQO_MODES:
                        print(f"FLIPFLOP rep {rep} {s} slot {slot} {m} {p}: fraction of controller steps with u > 0 while "
                              f"CPU < target " + " ".join(f"{g} {X(f'ubelow{g}_frac'):.3f}" for g in TENANTS), flush=True)
                    def warn(txt):
                        warnings.append((rep, slot, s, f"{m} {p}", txt))
                        print(f"WARNING: rep {rep} {s} slot {slot} {m} {p}: {txt}", flush=True)
                    if m in LQO_MODES and nw[-1] > 0:
                        warn(f"{int(nw[-1])} flooder iperf3 client(s) still running {LQO_WAIT_S:g} s after the "
                             f"release (terminated)")
                    if X("victim_wait_timeouts") > 0:
                        warn(f"{int(X('victim_wait_timeouts'))} victim iperf3 client(s) still running {LQO_WAIT_S:g} s "
                             f"after the loop (terminated)")
                    if X("victim_win_after_release") > 0:
                        warn(f"{int(X('victim_win_after_release'))} victim(s) with SLO windows ending after the victim "
                             f"release at {X('victim_release_s'):.2f} s")
                    miss = [g for i, g in enumerate(FLOODERS) if g in active() and math.isnan(fl[2 * i])]
                    if m != "alone" and miss:
                        warn("missing flooder iperf3 report(s): " + ", ".join(miss) + " (eBPF columns unaffected)")
                    if tail[5] > TICK_WARN_S:
                        warn(f"mean control tick {tail[5]:.3f} s > {TICK_WARN_S:g} s (nominal {DT:g} s; max "
                             f"{tail[6]:.3f} s, steal mean {tail[3]:.2f}%)")
                    if tail[3] > STEAL_WARN_MEAN or tail[4] > STEAL_WARN_MAX:
                        warn(f"steal mean {tail[3]:.2f}% / max {tail[4]:.2f}% above {STEAL_WARN_MEAN:g}% / "
                             f"{STEAL_WARN_MAX:g}%")
                    if tail[1] > CPU_WARN:
                        warn(f"mean host CPU {tail[1]:.1f}% > {CPU_WARN:g}% (max {tail[2]:.1f}%)")
                    slot += 1
                    time.sleep(PAUSE)
            if rep % SESSION_REPEATS == 0 and rep < REPS:
                print(f"session rest {SESSION_REST_S} s", flush=True)
                time.sleep(SESSION_REST_S)
                rested = True
    finally:
        for p in procs:
            if p.poll() is None: p.terminate()
        # after ANY failure, Ctrl+C, SIGTERM or SIGHUP: xdp_limit back on all six, checked against the kernel's state
        restore_tenants()
        for d in IF.values():
            if d.get("kind", "limit") != "limit":
                continue
            last_cap.pop(d["cfg"], None)
            try:
                set_cap(d, 0)
            except Exception as ex:
                print(f"WARNING: could not reset cap on {d['dev']} (cfg map id {d['cfg']}): {ex}", flush=True)
        csv.close(); tsf.close()

    print("\nREFERENCES per repeat and scenario (Gbit/s): " + "; ".join(
        f"rep {rep} {s} " + "/".join(f"{v} {r[v]:.3f} (vhost {vh[v]:.1f}%)" for v in VICTIMS)
        + f", RTT p50/p99 {q['p50']:.3f}/{q['p99']:.3f} ms (base_cpu {b:.1f}%)"
        for rep, s, r, b, q, vh in ref_hist))
    print(f"WARNINGS (mean tick > {TICK_WARN_S:g} s, steal mean > {STEAL_WARN_MEAN:g}% or max > {STEAL_WARN_MAX:g}%, "
          f"host CPU > {CPU_WARN:g}%, missing flooder iperf3 report, wait timeouts, victim windows after the release, "
          f"reference checks): {len(warnings)}"
          + "".join(f"\n  rep {a} {c} slot {b} {d}: {e}" for a, b, c, d, e in warnings))
    print(f"\nALL COLUMNS (mean +- sd over {REPS} repeats, per scenario)")
    for c in sorted(res, key=lambda c: (list(SCENARIOS).index(c[0]), c[1] != "alone", c[1], c[2])):
        print(f"{c[0]:4s} {c[1]:8s} {c[2]:>14s} {len(res[c]):4d} " + " ".join(
            f"{n}={a:.3f}+-{b:.3f}" for n, (a, b) in zip(RESULT_NAMES, (ms([r[j] for r in res[c]])
                                                                     for j in range(len(RESULT_NAMES))))))
    print("\n" + summarize(CSV), flush=True)

if __name__ == "__main__":
    if sys.argv[1:2] == ["--summary"]:
        print(summarize(sys.argv[2] if len(sys.argv) > 2 else CSV))
    else:
        exit_on_term_signals()
        main()
