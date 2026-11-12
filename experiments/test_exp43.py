#!/usr/bin/env python3
# Pure-python self-test for exp43.py and run43.sh: no VM, no sudo, no network, no traffic, no sleeping (simulated
# clock), nothing attached; run43.sh is executed in a temp dir against fake nproc/ss/pgrep/sudo on PATH.
# Part 0: configs (8 -> 17 slots), headers, durations, VF_BMAX_RANGE 40000, vfrot/lqr params == exp40/exp39.
# Part 1: K SIGN: Euler ARE with the paper's tau/b reproduces K = [-0.2562, -0.3031]; the ARE on the ZOH plant of
#         calib42 (b = |G|/tau > 0, B from [[0], [-b]]) has K[0] < 0 and K[1] < 0; exp42's Euler ARE on calib42 has
#         K[0] > 0 (the bug), the sign flips between dt/tau 1.5 and 1.9; ZOH cross-checked with scipy.
# Part 2: CONTROLLER SIGN on the real lqr_orig controller (lqo_new/lqo_step): above target + margin u > 0 and a
#         LARGER positive error gives a LARGER drop fraction; below target u == 0; closed loop on a first-order plant
#         with a drop cliff (u >= 0.05 collapses CPU) never locks at high u while CPU < target (exp42's K does).
# Part 3: the REAL exp43.run() on a simulated testbed: vfrot 16000/24000, lqr 85_4 and lqr_orig T60_paperK equal
#         exp42.run() tick by tick (only VF_BMAX_RANGE changed in the vfrot path); vfrot B32000 reaches 32000;
#         lqr_orig T60_zoh: u = 0 whenever e < 0; RTT and dip columns; real reference() incl. the ping refusal.
# Part 4: RTT p99/loss/SLO logic, dip metrics on synthetic series, SUMMARY43 with known W/T/L, p and frontier order.
# Part 5: refusals: exp43.main() (outputs exist, nproc, calib missing / FAIL, K[0] >= 0, plain T60) and FAIL FAST
#         (K check before sudo; rep-1 reference ping refusal before any slot and before the 600 s rest); a dry
#         exp43.main() (slot order, swaps only around lqr_orig, CSV/ts lines, SUMMARY43); run43.sh refusals.
# Part 6: exp39-exp42 and calib42 files unchanged (sha256 against a snapshot taken when exp43 was built);
#         no exp43 output in the testbed.
import sys
sys.dont_write_bytecode = True
import contextlib, csv as csvmod, hashlib, inspect, io, json, math, os, random, shutil, struct, subprocess, tempfile, time
sys.argv = sys.argv[:1]
import numpy as np
import exp39, exp40, exp42, exp43
import scipy.linalg, scipy.stats   # test-only cross-checks

E, E42 = exp43, exp42
HERE = os.path.dirname(os.path.abspath(E.__file__))
F = E.FLOODERS
nan = float("nan")
OUTS = ("results_dyn43.csv", "ts_dyn43.csv", "ref_victim43.txt", "exp43.log")
FLOOD_GBIT = 6.0
CPU_PER_GBIT = 10.0      # simulated vhost CPU, % of one core per passed Gbit/s
REFS = {"V1": 9.0, "V2": 9.0}
RREF = {"p50": 0.10, "p99": 0.25, "loss": 0.0, "n": 200}
CALIB_REAL = json.load(open(os.path.join(HERE, "calib42_result.json")))   # read only
CALIB_SIM = {"verdict": "WARN", "checks": [["x", "WARN", "sim"]], "cpu_ref": 50.0, "tau": 0.08, "b": 1000.0,
             "written": "test"}   # dt/tau 6.25 like calib42: Euler K > 0, ZOH K < 0
# sha256 snapshot taken when exp43 was built (2026-11-07), before any test run
SNAPSHOT = """
92ac155cd1433b416258e476e203476a7db9011eb70c6ce37ca116a354fd483b exp39.py
c52248bde8795ba19f033c32fe2eb065916713af97c69d558078d820241b0db4 run39.sh
a9ff7f290d5a9854262d995c2c2fdbbeadb9841a96a9097a80f8ade6f3771c7a test_exp39.py
b69243589cda428d082d84e462d17f4639df8c658514348167d1ea2966b749ef results_dyn39.csv
d01f5d249b5c71f17b1340bfd70a15a971dbac2cc378837a820c733176c47fee ts_dyn39.csv
66d287eef2344d3d7c6f32620ac2b57d5acc9fb9c87f1691bd7d450ce15d0ace ref_victim39.txt
a318a8bb060c0b018d666f308dd13b811314de624820620fa89687a8f2028e0f exp39.log
32401a0ca18165ac9b9d3a1caa6025543d59029cbdcde948c37ea60e8b3a2a2d exp40.py
c43aa5182d0570ffb4651c12062c4435c65a34ceef3c3360ab15e3bdff9926a4 run40.sh
f00184cf9e943a4fcdd46be13e6720a806ee7ab23365569f10b41ac68b959089 test_exp40.py
8753327fe1783cfd0391ed26ef6d4aacd58d1257e0f9fac6d4c2e04a671a67cc results_dyn40.csv
cfea8b0caa761703fe990829ee68d9e6bc49c3ba9446e4585ce8b0bf4f5c4875 ts_dyn40.csv
819fd46ee65980130e393b807a1f9f80b35cf932fa466dc55a19ca31529da6e5 ref_victim40.txt
4834900f6aad7b771b23193f79a6d7b80e8c8fbc59ab537cffe25116bc7e81a9 exp40.log
3b48b6681facd6f364a571b7abeac90c5f8af115cc7c438d22c3dc5615a31772 exp41.py
08419a823e3888f4af430ae2f50aa31030b59385f18ef4430d4cec9898043647 run41.sh
a5b991564b5a7b65dc512514deea6d1257e703d77876bf69deba34066e75741f test_exp41.py
fcb67d5708330a5ecb7e19a22197ea94c11cef7e2ab480ab1a46321874b208ca results_dyn41.csv
14379109105f0cde3ba603a3a2620da3deb12b8c610881ef938ca0b37917a158 ts_dyn41.csv
c49b553e14bb874ec1efc536658a7d6dc17ffb2b13249e10db3689ea7549e779 ref_victim41.txt
c661449f14202a7ebe9681e018e4db5cb5059fe4e712b11fb500ba76f75601cb exp41.log
00ce4364f9fcfd8722ca2c7fe5819839a845eeb716c8719eb3c983cbc425e700 exp42.py
4eebe54fd5b9a73a15ebf167597e83fd93e2fa587ba9c219b889f50190c15aa6 run42.sh
112ec56a688f6ed2ff42b62d32f0f06795fe0da4f419b1e3a44b10ced6aae03b test_exp42.py
6d22245cd4c52e1613ccf86f78e32645f00242348a0ca5a007bc5cda7e2fae49 results_dyn42.csv
c03bf4cc2b9866d1a50d296dc3c9ce203b3d008a628198962ba9acc40ccd483f ts_dyn42.csv
0997b109e0e69c8d77bcc87c0259d20772a38bd8e73c09864d80ca584ac256ea ref_victim42.txt
612b68779398fe7a1fe0f2792954099d428903078cdce8fb47a29d37de0f1b4c exp42.log
28d534f7d6ce2340eb655791f826492d0fc27c45efa913b70853fc7fc6b49a16 calib42.py
5e6ac68a69308904edf97dbb37622e6260d182dcf0e85dfbc81a5fcd92c7ee07 calibrate42.sh
19c5ac2cb096a1c9ee9da3e3ade02b77a94390920b082843ed9f9d75d85563e9 calib42_result.json
71f9ff938c90cb03490ce2b6505c7b014154a376f1e22a7560595427b2e6b826 calib42_ts.csv
a1ec83bf44aef85201c70c6b22cbbf88914fb9d138fc3896a2537f46eb305b7a calib42_run.log
ad97d0ce7b8ec939233a8fe0cd410ebcfec678afa74f7d6986690c7e99ec6bae xdp_dropfrac.c
71f133ea1565d447ed0509f9f25092a703050052a1308794a1f5f890521c02c0 xdp_dropfrac.o
1d83fcc4d48af22600f8344aa681cd0c77fd72c8c1b2723d97d5a2cef370055d xdp_limit.c
9c8f91066da34423702b1ebe840234c9a0e36e38b5b7ac9c49c6506df03b1baf xdp_limit.o
"""
SNAP = {l.split()[1]: l.split()[0] for l in SNAPSHOT.strip().splitlines()}


def sha(names):
    return {n: (hashlib.sha256(open(os.path.join(HERE, n), "rb").read()).hexdigest()
                if os.path.exists(os.path.join(HERE, n)) else None) for n in names}


def same(a, b):
    return a == b or (isinstance(a, float) and isinstance(b, float) and a != a and b != b)


def expect_exit(fn, *words):
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            fn()
    except SystemExit as e:
        msg = str(e)
        assert all(w in msg for w in words), (msg, words)
        return msg
    raise AssertionError(f"no SystemExit, expected {words}")


def rcol(n):
    return E.RESULT_NAMES.index(n)


# ---------------- simulated testbed ----------------

def parse_cmd(cmd):
    w = cmd.split()
    if "ping" in w:
        return {"ping": True, "n": int(w[w.index("-c") + 1]), "i": float(w[w.index("-i") + 1]), "delay": 0.0}
    delay = float(w[1].rstrip(";")) if w[0] == "sleep" else 0.0
    par = int(w[w.index("-P") + 1]) if "-P" in w else 1
    rate = float(w[w.index("-b") + 1].rstrip("G")) * par if "-b" in w else None
    return {"ping": False, "delay": delay, "secs": float(w[w.index("-t") + 1]), "rate": rate}


class FakeProc:
    def __init__(self, sim, name, end):
        self.sim, self.name, self.end, self.terminated = sim, name, end, False
    def poll(self): return 0 if self.sim.clock[0] >= self.end else None
    def wait(self, timeout=None):
        if timeout is not None and self.end > self.sim.clock[0] + timeout:
            self.sim.clock[0] += timeout
            raise subprocess.TimeoutExpired(self.name, timeout)
        self.sim.clock[0] = max(self.sim.clock[0], self.end); return 0
    def terminate(self): self.terminated = True


def ping_text(n, lost, rtt):
    lines = ["PING 10.10.0.1 (10.10.0.1): 56 data bytes"]
    lines += [f"64 bytes from 10.10.0.1: seq={s} ttl=64 time={rtt(s):.3f} ms" for s in range(n) if s not in lost]
    lines += ["", "--- 10.10.0.1 ping statistics ---", f"{n} packets transmitted, {n - len(lost)} packets received, "
              f"{100 * len(lost) // n}% packet loss"]
    return "\n".join(lines) + "\n"


class Sim:
    # victim v delivers its -b rate; a flooder offers FLOOD_GBIT inside its iperf3 window and passes min(offer, cap)
    # under xdp_limit (cap 0 = all) or offer x (1 - drop_bp / 1e4) under xdp_dropfrac; vhost CPU = CPU_PER_GBIT x
    # passed Gbit/s; ping: replies with rtt(seq) ms except the seqs in `lost`
    def __init__(self, seed=1, read_cost=0.002, lost=(), rtt=None, lost_frac=0.0):
        self.rng, self.clock, self.read_cost = random.Random(seed), [1000.0], read_cost
        self.launches, self.capb, self.cum, self.kinds, self.ids = {}, {}, {}, {}, 100
        self.cpu_acc, self.lost, self.lost_frac = [0, 0, 0], set(lost), lost_frac
        self.rtt = rtt or (lambda s: 0.1 + 0.01 * (s % 10))
        self.pings, self.ssh_log = [], []

    def sudo(self, args):
        if args == ["true"]:
            return ""
        assert args[:3] == ["bpftool", "map", "update"], args
        self.capb[args[4]] = struct.unpack("<Q", bytes.fromhex("".join(args[-8:])))[0]
        return ""

    def counters(self, d):
        self.clock[0] += self.read_cost
        now, g = self.clock[0], str(d["cnt"]).split(":")[0][3:]
        c = self.cum.setdefault(d["cnt"], [0.0, 0.0, now, 0.0, 0.0])
        L = self.launches.get(g, [])
        if g in E.VICTIMS:
            c[0] = sum(p["rate"] * 1e9 / 8 * max(0.0, min(now - t - p["delay"], p["secs"])) for t, p in L)
        else:
            dt, c[2] = now - c[2], now
            off = sum(FLOOD_GBIT * 1e9 / 8 * dt for t, p in L if t + p["delay"] <= now < t + p["delay"] + p["secs"])
            v = self.capb.get(d["cfg"], 0)
            passed = off * (1.0 - v / 10000.0) if d.get("kind") == "dropfrac" else (off if v == 0 else min(off, v * dt))
            c[0] += passed; c[1] += off - passed
            c[3] += passed / 1500.0; c[4] += (off - passed) / 1500.0
        return {0: int(c[3]), 1: int(c[4]), 2: int(c[0]), 3: int(c[1])}

    def vhost_ns(self, path):
        g = path[2:]
        passed = sum(x[0] for k, x in self.cum.items() if str(k).split(":")[0] == f"cnt{g}")
        return int(passed * 8 / 1e9 * CPU_PER_GBIT / 100.0 * 1e9)

    def cpu(self):
        self.cpu_acc[0] += 100; self.cpu_acc[1] += 40
        return tuple(self.cpu_acc)

    def ssh(self, guest, cmd, out):
        now = self.clock[0]
        self.ssh_log.append((guest, cmd, now))
        p = parse_cmd(cmd)
        if p["ping"]:
            n = p["n"]
            prng = random.Random(len(self.pings))   # own generator: the clock jitter (self.rng) stays untouched
            lost = set(self.lost) | {s for s in range(n) if prng.random() < self.lost_frac}
            self.pings.append((now, n, lost))
            out.write(ping_text(n, lost, self.rtt)); out.flush()
            pr = FakeProc(self, guest, now + n * p["i"] + 0.1)
            return pr
        self.launches.setdefault(guest, []).append((now, p))
        gb = p["rate"] if guest in E.VICTIMS else FLOOD_GBIT
        out.write(json.dumps({"end": {"sum_received": {"bytes": int(gb * 1e9 / 8 * p["secs"]),
                                                       "bits_per_second": gb * 1e9}}}))
        out.flush()
        return FakeProc(self, guest, now + p["delay"] + p["secs"] + 0.3)

    def sleep(self, s):
        self.clock[0] += max(0.0, s) + self.rng.uniform(0.0, 0.04)

    def load(self, dev, kind):
        g = dev[4:]; self.ids += 1
        self.kinds[dev] = kind
        return {"dev": dev, "prog": self.ids, "cfg": f"cfg{g}:{self.ids}", "cnt": f"cnt{g}:{self.ids}", "kind": kind}

    def vnet(self, dom):
        return "vnet" + [g for g, v in E.GUESTS.items() if v[0] == dom][0]

    def make_attach(self, mod):
        def attach(dev, cap):
            g = dev[4:]
            if self.kinds.get(dev) == "limit" and mod.IF.get(g, {}).get("kind") == "limit":
                return dict(mod.IF[g])
            return self.load(dev, "limit")
        return attach

    def prog_maps(self, dev, kind):
        if self.kinds.get(dev) != kind:
            raise SystemExit(f"{dev}: XDP prog is not {kind}")
        g = dev[4:]
        return {"dev": dev, "prog": 0, "cfg": f"cfg{g}:x", "cnt": f"cnt{g}:x", "kind": kind}


@contextlib.contextmanager
def patched(mod, sim):
    extra = {"sudo": sim.sudo, "counters": sim.counters, "cpu": sim.cpu, "ssh": sim.ssh, "vhost_ns": sim.vhost_ns,
             "load": sim.load, "prog_maps": sim.prog_maps, "vnet": sim.vnet, "attach": sim.make_attach(mod)}
    saved = {k: getattr(mod, k) for k in extra}
    real_time = (time.monotonic, time.sleep)
    for k, v in extra.items():
        setattr(mod, k, v)
    time.monotonic, time.sleep = (lambda: sim.clock[0]), sim.sleep
    try:
        yield
    finally:
        time.monotonic, time.sleep = real_time
        for k, v in saved.items():
            setattr(mod, k, v)


def fake_if(mod, sim):
    mod.IF.clear(); mod.last_cap.clear(); mod.last_drop.clear()
    for g in mod.VICTIMS + mod.FLOODERS:
        mod.IF[g] = {"dev": f"vnet{g}", "prog": 0, "cfg": f"cfg{g}", "cnt": f"cnt{g}", "kind": "limit"}
        sim.kinds[f"vnet{g}"] = "limit"
    for g in mod.FLOODERS:
        mod.VH[g] = f"vh{g}"


def sim_run(mod, mode, param, sim, refs=None):
    refs = dict(refs or REFS)
    with patched(mod, sim):
        fake_if(mod, sim)
        with contextlib.redirect_stdout(io.StringIO()):
            args = (mode, param, refs, 50.0, []) + ((dict(RREF),) if mod is E else ())
            if mode == "lqr_orig":
                with mod.dropfrac_flooders():
                    r, ts = mod.run(*args)
            else:
                r, ts = mod.run(*args)
    return r, ts


def setup(mod, configs, calib):
    mod.LQR.clear(); mod.LQO.clear(); mod.VFROT.clear()
    mod.setup_configs(configs, calib)


# ---------------- Part 0 ----------------

def part0():
    assert len(E.CONFIGS) == 8 and len(set(E.CONFIGS)) == 8
    assert set(E.CONFIGS) == {("lqr_orig", "T40_paperK"), ("lqr_orig", "T60_paperK"), ("lqr_orig", "T80_paperK"),
                              ("lqr_orig", "T60_zoh"), ("vfrot", "0.8_1_2_0.94"), ("vfrot", "0.8_1_2_0.94_B24000"),
                              ("vfrot", "0.8_1_2_0.94_B32000"), ("lqr", "85_4")}
    assert E.PAIRED_REFS == [("vfrot", "0.8_1_2_0.94"), ("lqr_orig", "T60_paperK")]
    setup(E, E.CONFIGS, CALIB_REAL)
    assert E.LQR["85_4"][:2] == exp39.parse_lqr("85_4") and (E.LQR["85_4"][2] == exp39.lqr_gain(4.0)).all()
    assert E.VFROT[("vfrot", "0.8_1_2_0.94")] == exp40.parse_vfrot("vfrot", "0.8_1_2_0.94")
    assert E.VFROT[("vfrot", "0.8_1_2_0.94_B24000")] == E42.parse_vfrot("vfrot", "0.8_1_2_0.94_B24000")
    assert E.VFROT[("vfrot", "0.8_1_2_0.94_B32000")][4] == 32000.0
    expect_exit(lambda: E42.parse_vfrot("vfrot", "0.8_1_2_0.94_B32000"), "outside")   # why the range was widened
    assert E.VF_BMAX_RANGE == (4000.0, 40000.0) and E42.VF_BMAX_RANGE == (4000.0, 30000.0)
    assert E.parse_vfrot("vfrot", "0.8_1_2_0.94_B40000")[4] == 40000.0
    expect_exit(lambda: E.parse_vfrot("vfrot", "0.8_1_2_0.94_B40001"), "outside")
    for name in ("parse_vfrot", "vf_params", "active_flooders", "high_cap", "next_after", "hold_caps",
                 "lowest_delivered_pick", "pidrotf_caps", "pf_update", "budget_shares", "highcap_shares",
                 "lqo_filter", "lqo_soft", "lqo_quant", "lqo_step", "parse_lqr", "lqr_plant", "lqr_gain", "riccati"):
        assert inspect.getsource(getattr(E, name)) == inspect.getsource(getattr(E42, name)), name
    for k in ("VFROT_VS_TAU", "VFROT_THRESH", "VFROT_HOLD", "VFROT_CUT_HOLD", "VF_B0", "VF_BMAX", "CAP_MIN",
              "CAP_MAX", "DFS_ACTIVE_MBIT", "HIGH_CAP_FRAC", "SMOOTH", "LQO_DT", "LQO_MED_N", "LQO_TREND_W", "LQO_R",
              "LQO_Q", "LQO_ALPHA_NUM", "K_PAPER", "LQO_MARGIN", "LQO_UMAX", "LQO_LEVELS_BP", "LQO_WAIT_S", "T", "DT",
              "OFFSET", "V_RATE", "SLO_FRAC", "STEADY", "PAR", "GUESTS", "SESSION_REST_S", "PAUSE", "TICK_WARN_S",
              "STEAL_WARN_MEAN", "STEAL_WARN_MAX", "NPROC_REQUIRED"):
        assert getattr(E, k) == getattr(E42, k), k
    assert E.REF_RUNS == 2 and E.REF_T == 20 and E.PING_V == "V2" and E.PING_I == 0.2
    assert E.HEADER.startswith(E42.HEADER + ",") and E.HEADER.endswith("ref_v2_rtt_p50_ms,ref_v2_rtt_p99_ms")
    assert E.TS_HEADER.startswith(E42.TS_HEADER + ",") and len(E.TS_HEADER.split(",")) == 4 + E.TS_RTT + 2
    n, per, tot = E.expected_duration(len(E.CONFIGS), 6)
    _, perm, totm = E.measured_duration(len(E.CONFIGS), 6)
    assert n == 17 and perm <= 15.1 * 60 and per <= 16 * 60
    print(f"CHECK 0 OK: 8 configs -> {n} slots/repeat, ~{perm / 60:.1f} min/repeat at exp42's measured slot rate "
          f"(nominal {per / 60:.1f}), R=6 ~{totm / 3600:.2f} h (nominal {tot / 3600:.2f} h); VF_BMAX_RANGE upper end "
          f"40000 (exp42 refuses B32000), B40001 refused; vfrot/lqr/lqr_orig controller functions source-identical to "
          f"exp42's, constants identical; headers extend exp42's; reference 2 x 20 s with ping")


# ---------------- Part 1: K sign ----------------

def zoh_scipy(tau, b, dt):
    Ac = np.array([[0.0, 1.0], [-1.0 / tau, -1.0 / tau]]); Bc = np.array([[0.0], [-b]])
    M = np.zeros((3, 3)); M[:2, :2] = Ac; M[:2, 2:] = Bc
    X = scipy.linalg.expm(M * dt)
    return X[:2, :2], X[:2, 2:]


def part1():
    tau, b, cref = float(CALIB_REAL["tau"]), float(CALIB_REAL["b"]), float(CALIB_REAL["cpu_ref"])
    assert b > 0 and tau > 0 and abs(b - abs(CALIB_REAL["G"]) / tau) < 1e-6 * b   # b = |G| / tau > 0
    # paper values still reproduce the paper K (Euler, as the authors)
    for target in (10.0, 24.66, 50.0):
        k = E.lqo_gain(E.LQO_TAU_PAPER, E.LQO_B_PAPER, target)
        assert abs(k[0] - E.K_PAPER[0]) < 5e-4 and abs(k[1] - E.K_PAPER[1]) < 5e-4, k
    # ZOH plant: B from [[0], [-b]], entries <= 0, exp43's expm == scipy's
    A, B = E.lqo_plant_zoh(tau, b)
    As, Bs = zoh_scipy(tau, b, E.LQO_DT)
    assert np.allclose(A, As, rtol=1e-9, atol=1e-12) and np.allclose(B, Bs, rtol=1e-9, atol=1e-12)
    assert (B <= 0).all() and B[1, 0] < 0
    Ap, Bp = E.lqo_plant_zoh(2.0, 10.0)
    assert np.allclose(Ap, zoh_scipy(2.0, 10.0, 0.5)[0]) and np.allclose(Bp, zoh_scipy(2.0, 10.0, 0.5)[1])
    # THE RE-IDENTIFIED GAIN: K[0] < 0 and K[1] < 0 like the paper, at every target; equals scipy's DARE
    for frac in (0.4, 0.6, 0.8):
        target = frac * cref
        K = E.lqo_gain_zoh(tau, b, target)
        assert K[0] < 0, f"re-identified K[0] = {K[0]} is not negative"
        assert K[1] < 0, f"re-identified K[1] = {K[1]} is not negative"
        al = E.LQO_ALPHA_NUM / max(target, 1.0)
        Q = np.diag([E.LQO_Q[0] * al, E.LQO_Q[1] * al]); R = np.array([[E.LQO_R]])
        P = scipy.linalg.solve_discrete_are(As, Bs, Q, R)
        Ks = np.linalg.solve(R + Bs.T @ P @ Bs, Bs.T @ P @ As)
        assert abs(K[0] - Ks[0, 0]) < 1e-6 and abs(K[1] - Ks[0, 1]) < 1e-6, (K, Ks)
        assert E.lqo_eig(tau, b, K, zoh=True) < 1.0
    K60 = E.lqo_gain_zoh(tau, b, 0.6 * cref)
    assert E.calib_gain(CALIB_REAL) == K60
    assert abs(K60[0] - (-0.0133)) < 5e-4 and abs(K60[1] - (-0.0012)) < 5e-4, K60
    setup(E, E.CONFIGS, CALIB_REAL)
    assert E.LQO["T60_zoh"][1] == K60 and E.LQO["T60_zoh"][1][0] < 0
    for p in ("T40_paperK", "T60_paperK", "T80_paperK"):
        assert E.LQO[p][1] == E.K_PAPER and E.LQO[p][3] == "paperK"
    # the exp42 bug: the Euler ARE on calib42 has the WRONG sign, the flip is caused by dt/tau, not by B
    ke = E.lqo_gain(tau, b, 0.6 * cref)
    assert ke[0] > 0 and ke[1] > 0, ke
    assert E.lqo_plant(tau, b)[1][1, 0] < 0   # exp42's B was [[0], [-b dt]] all along
    G = b * tau
    def euler_k(r):
        return E.lqo_gain(0.5 / r, G / (0.5 / r), 24.66)
    assert euler_k(1.5)[0] < 0 and euler_k(1.9)[0] > 0
    # the ZOH gain has the right sign on the same dt/tau sweep
    assert all(E.lqo_gain_zoh(0.5 / r, G / (0.5 / r), 24.66)[0] < 0 for r in (0.25, 1.0, 1.9, 3.0, 6.19, 20.0))
    # check_gain_sign refuses K[0] >= 0 and nan
    for bad in ((0.0092, 0.0087), (0.0, -0.1), (nan, -0.1)):
        expect_exit(lambda: E.check_gain_sign(bad, "t"), "REFUSING TO START", "K[0] >= 0")
    assert E.check_gain_sign(K60, "t") == K60
    print(f"CHECK 1 OK (K SIGN): paper tau/b (Euler, as the authors) -> K {E.K_PAPER}; calib42 plant b = |G|/tau = "
          f"{b:.1f} > 0, ZOH B = [{B[0, 0]:.2f}, {B[1, 0]:.2f}]^T; re-identified ZOH ARE K at T40/T60/T80 = "
          f"[{K60[0]:.4f}, {K60[1]:.4f}] (K[0] < 0 and K[1] < 0, == scipy DARE, stable); exp42's Euler ARE on the "
          f"same plant = [{ke[0]:+.4f}, {ke[1]:+.4f}] (the bug; sign flips between dt/tau 1.5 and 1.9, B was "
          f"correct); K[0] >= 0 refused")


# ---------------- Part 2: controller sign ----------------

def steady_u(K, target, cpu, n=8):
    s = E.lqo_new(); u = None
    for _ in range(n):
        u, eff, e = E.lqo_step(s, cpu, target, K)
    return u / 10000.0, e


def cliff_cpu(u, cref=41.1):
    # first-order plant target: static map of calib42 with the cliff: u < 0.05 -> full CPU, u >= 0.05 collapses
    pts = [(0.0, cref), (0.05, 15.8), (0.10, 15.5), (0.20, 1.7), (0.50, 0.13), (0.95, 0.05)]
    if u < 0.05:
        return cref
    for (ua, ca), (ub, cb) in zip(pts, pts[1:]):
        if ua <= u <= ub:
            return ca + (cb - ca) * (u - ua) / (ub - ua)
    return pts[-1][1]


def closed_loop(K, target, steps=160, tau_p=0.2, seed=3):
    rng = random.Random(seed); s = E.lqo_new(); cpu = 41.1; rows = []
    a = math.exp(-E.LQO_DT / tau_p); u = 0.0
    for _ in range(steps):
        meas = cpu + rng.gauss(0.0, 0.4)
        bp, eff, e = E.lqo_step(s, meas, target, K)
        u = bp / 10000.0
        rows.append((cpu, eff, e, u))
        cpu = cliff_cpu(u) + (cpu - cliff_cpu(u)) * a
    return rows


def part2():
    tau, b, cref = float(CALIB_REAL["tau"]), float(CALIB_REAL["b"]), float(CALIB_REAL["cpu_ref"])
    target = 0.6 * cref
    kz = E.lqo_gain_zoh(tau, b, target)
    ke = E.lqo_gain(tau, b, target)
    for name, K in (("paper", E.K_PAPER), ("zoh", kz)):
        # above target + margin: u > 0, non-decreasing in e, and a LARGER error gives a LARGER drop fraction
        es = [0.6, 1.0, 2.0, 4.0, 8.0, 12.0, 16.0]
        us = [steady_u(K, target, target + x)[0] for x in es]
        assert all(u > 0 for u in us), (name, us)
        assert all(b_ >= a_ for a_, b_ in zip(us, us[1:])), (name, us)
        assert us[-1] > us[0], (name, us)
        # step-by-step with a rising CPU (edot > 0): u never decreases while e > margin rises
        s = E.lqo_new(); prev = -1.0
        for x in np.linspace(target + 1.0, target + 16.0, 12):
            bp, eff, e = E.lqo_step(s, x, target, K)
            if e > E.LQO_MARGIN:
                assert bp / 1e4 >= prev, (name, x, bp)
                prev = bp / 1e4
        # below target: u == 0 exactly (no latching), whatever the history
        for x in (-0.1, -1.0, -5.0, -20.0, -target + 0.5):
            assert steady_u(K, target, target + x)[0] == 0.0, (name, x)
        # above, then collapse below and stay there: u == 0 on every step with e < 0 and e not rising (edot <= 0);
        # the authors' D term may give ONE kick (u > 0 with e < 0) on the step where e climbs back after the
        # filter's jump (edot > 0); it never persists while CPU stays below target
        s = E.lqo_new(); e_prev = None; kicks = 0; tail = []
        for x in [target + 15] * 5 + [target - 10] * 10:
            bp, eff, e = E.lqo_step(s, x, target, K)
            edot = 0.0 if e_prev is None else (e - e_prev) / E.LQO_DT
            if e < 0 and edot <= 0:
                assert bp == 0, (name, e, edot, bp)
            if e < 0 and bp > 0:
                assert edot > 0, (name, e, edot, bp); kicks += 1
            e_prev = e; tail.append(bp)
        assert kicks <= 1 and tail[-6:] == [0] * 6, (name, kicks, tail)
        # closed loop with the drop cliff: u == 0 whenever CPU_eff < target and not rising; never stuck at u > 0
        # while CPU < target (at most 3 steps: median-of-5 filter lag + one D kick)
        rows = closed_loop(K, target)
        for (c0, f0, e0, u0), (c1, f1, e1, u1) in zip(rows, rows[1:]):
            if e1 < 0 and e1 <= e0:
                assert u1 == 0.0, (name, e0, e1, u1)
        stuck, worst = 0, 0
        for cpu, eff, e, u in rows:
            stuck = stuck + 1 if (cpu < target and u > 0) else 0
            worst = max(worst, stuck)
        assert worst <= 3, (name, worst)
        stuck, worst_e = 0, 0
        for cpu, eff, e, u in rows:
            stuck = stuck + 1 if (e < 0 and u > 0) else 0
            worst_e = max(worst_e, stuck)
        assert worst_e <= 1, (name, worst_e)
        assert sum(u == 0 for *_, u in rows[40:]) >= 0.2 * len(rows[40:]), name
    # the exp42 gain FAILS these checks (the test bites)
    assert steady_u(ke, target, target - 10.0)[0] > 0                       # drop while CPU is BELOW target
    assert steady_u(ke, target, target - 20.0)[0] > steady_u(ke, target, target - 5.0)[0]   # inverted: lower CPU, more drop
    rows = closed_loop(ke, target)
    assert all(u > 0 and cpu < target for cpu, eff, e, u in rows[40:]), "exp42's Euler K should lock"
    print(f"CHECK 2 OK (CONTROLLER SIGN, real lqo_step): paper K and ZOH K [{kz[0]:.4f}, {kz[1]:.4f}]: CPU above "
          f"target + margin -> u > 0, non-decreasing in e and larger e -> larger u (e 0.6 -> 16: u "
          f"{steady_u(kz, target, target + 0.6)[0]:.2f} -> {steady_u(kz, target, target + 16)[0]:.2f} for ZOH); CPU "
          f"below target (steady or falling) -> u == 0, at most one D-term kick when e climbs back, never two in a "
          f"row; closed loop with the u >= 0.05 cliff: at most 3 steps (filter lag) with CPU < target and u > 0; exp42's Euler K [{ke[0]:+.4f}, {ke[1]:+.4f}] gives u > 0 "
          f"below target, MORE drop the further CPU falls, and locks (CPU < target with u > 0 for the whole run), as observed in exp42")


# ---------------- Part 3: real run() on the simulated testbed ----------------

def part3():
    cfg42 = [("vfrot", "0.8_1_2_0.94"), ("vfrot", "0.8_1_2_0.94_B24000"), ("lqr", "85_4"), ("lqr_orig", "T60_paperK")]
    setup(E42, cfg42, CALIB_SIM)
    setup(E, E.CONFIGS, CALIB_SIM)
    for m, p in cfg42:
        s42, s43 = Sim(seed=11), Sim(seed=11)
        r42, ts42 = sim_run(E42, m, p, s42)
        r43, ts43 = sim_run(E, m, p, s43)
        assert len(r42) == E42.R_N + len(E42.NEW_NAMES) and len(r43) == len(E.RESULT_NAMES)
        assert len(ts42) == len(ts43) > 100
        for a, b_ in zip(ts42, ts43):
            assert len(b_) == E.TS_RTT + 2 and all(same(x, y) for x, y in zip(a, b_[:E.TS_RTT])), (m, p)
        assert all(same(x, y) for x, y in zip(r42, r43[:E.R_M])), (m, p)
    # vfrot B32000 reaches its ceiling (victims never suffer in the sim)
    r, ts = sim_run(E, "vfrot", "0.8_1_2_0.94_B32000", Sim(seed=5))
    bud = [row[E.TS_BUDGET] for row in ts]
    assert max(bud) == 32000.0 and r[rcol("budget_ceiling_share")] > 0
    r16, ts16 = sim_run(E, "vfrot", "0.8_1_2_0.94", Sim(seed=5))
    assert max(row[E.TS_BUDGET] for row in ts16) == 16000.0
    # lqr_orig T60_zoh: sign on every controller tick of the real run
    sim = Sim(seed=7)
    r, ts = sim_run(E, "lqr_orig", "T60_zoh", sim)
    ctl = [row for row in ts if row[E.TS_CTL] == 1]
    n_neg = n_pos = 0
    for prev, row in zip(ctl, ctl[1:]):
        for i in range(E.NF):
            e, u, e0 = row[E.TS_E + i], row[E.TS_U + i], prev[E.TS_E + i]
            if math.isnan(e) or math.isnan(e0):
                continue
            if e < 0 and e <= e0:
                assert u == 0.0, (row[0], e, u); n_neg += 1
            if e > E.LQO_MARGIN:
                assert u > 0.0, (row[0], e, u); n_pos += 1
    assert n_neg > 0 and n_pos > 0
    assert all(r[rcol(f"u{g}_mean")] > 0 for g in F)
    # RTT columns from the fake ping: 200 probes, 3 lost, rtt 0.10 .. 0.19 ms
    lost = {5, 50, 150}
    sim = Sim(seed=9, lost=lost)
    r, ts = sim_run(E, "alone", "na", sim)
    got = {s: sim.rtt(s) for s in range(200) if s not in lost}
    p50, p99, loss, _ = E.rtt_stats(got, 200)
    assert abs(r[rcol("v2_rtt_p50_ms")] - p50) < 1e-9 and abs(r[rcol("v2_rtt_p99_ms")] - p99) < 1e-9
    assert abs(r[rcol("v2_loss_pct")] - 1.5) < 1e-9 and r[rcol("slo_ok_v2_rtt")] == 0.0   # 1.5 % > 1 %
    assert r[rcol("slo_ok_worst_with_rtt")] == 0.0 and r[rcol("slo_ok_worst")] == 1.0
    assert sum(row[E.TS_RTT + 1] for row in ts) == 3
    r, ts = sim_run(E, "alone", "na", Sim(seed=9, lost={7, 77}))
    assert r[rcol("slo_ok_v2_rtt")] == 1.0 and r[rcol("slo_ok_worst_with_rtt")] == 1.0   # 1.0 % passes
    r, ts = sim_run(E, "alone", "na", Sim(seed=9, rtt=lambda s: 0.1 if s % 50 else 5.0))
    assert r[rcol("v2_rtt_p99_ms")] > 2.0 and r[rcol("slo_ok_v2_rtt")] == 0.0   # p99 above max(2 x 0.25, 2 ms)
    # dips in run(): V1's reference 9.5 -> every window at 9.0 / 9.5 = 0.947: below 0.95, not below 0.90
    r, ts = sim_run(E, "alone", "na", Sim(seed=9), refs={"V1": 9.5, "V2": 9.0})
    d = {n: r[rcol(n)] for n in E.DIP_NAMES}
    assert d["dip95_ovl"] == 1.0 and d["dip95_all"] == 1.0, d
    assert d["dip90_ovl"] == d["dip90_all"] == d["dip80_ovl"] == d["dip80_all"] == 0.0, d
    r, ts = sim_run(E, "alone", "na", Sim(seed=9))
    assert all(r[rcol(n)] == 0.0 for n in E.DIP_NAMES)
    # offered rates: a flooder offers FLOOD_GBIT over its window (vfrot caps it, offered stays ~6)
    r, ts = sim_run(E, "vfrot", "0.8_1_2_0.94", Sim(seed=4))
    assert all(abs(r[rcol(f"offered{g}_gbps")] - FLOOD_GBIT) < 0.3 for g in F), [r[rcol(f"offered{g}_gbps")] for g in F]
    # CSV / ts line field counts
    line = E.csv_line(1, 0, "vfrot", "0.8_1_2_0.94", r, REFS, RREF)
    assert len(line.strip().split(",")) == len(E.HEADER.split(","))
    assert all(len(E.ts_line(1, 0, "vfrot", "x", row).strip().split(",")) == len(E.TS_HEADER.split(",")) for row in ts)
    # the REAL reference(): ping reference and its refusal (loss > 5 %)
    tmp = tempfile.mkdtemp()
    saved = E.REF_FILE
    try:
        E.REF_FILE = os.path.join(tmp, "ref.txt")
        sim = Sim(seed=2)
        with patched(E, sim):
            fake_if(E, sim)
            with contextlib.redirect_stdout(io.StringIO()):
                refs, base, rref = E.reference(1, [])
        assert abs(refs["V1"] - 9.0) < 0.05 and rref["n"] == 200 and rref["loss"] == 0.0
        assert abs(rref["p99"] - E.q_incl([sim.rtt(s) for s in range(100)] * 2, 99)) < 1e-9
        assert sum(1 for g, c, t in sim.ssh_log if c.startswith("ping -c 100 -i 0.2")) == 2
        os.remove(E.REF_FILE)
        sim = Sim(seed=2, lost=set(range(0, 100, 10)) | {1, 2})   # 12 of 100 per run lost = 12 %
        with patched(E, sim):
            fake_if(E, sim)
            expect_exit(lambda: E.reference(1, []), "reference ping failed", "loss 12.0%")
    finally:
        E.REF_FILE = saved
        shutil.rmtree(tmp)
    print("CHECK 3 OK (real run() on the simulated testbed): vfrot 16000 / 24000, lqr 85_4 and lqr_orig T60_paperK "
          "== exp42.run() tick by tick and in every exp42 result column; vfrot B32000 reaches 32000 (16000 stays at "
          "16000); lqr_orig T60_zoh: u == 0 on every controller tick with e < 0 and not rising, u > 0 with e > margin; RTT p50/p99/"
          "loss from the ping output, loss 1.5 % fails / 1.0 % passes, p99 > 2 ms fails; dips 1/0/0 at a 0.947 ratio; "
          "offered rates; CSV/ts field counts; real reference() pings 2 x 100 and refuses at 12 % loss")


# ---------------- Part 4: RTT, dips, summary ----------------

SUMCOLS = ["rep", "slot", "mode", "param"] + [m for m, _, _ in E.SUMMARY_METRICS] + ["slo_ok_v2_rtt", "v2_rtt_p50_ms",
                                                                                     "v2_loss_pct"]


def part4():
    # RTT
    xs = [0.1 * i for i in range(1, 101)]
    assert abs(E.q_incl(xs, 50) - statistics_q(xs, 50)) < 1e-12 and abs(E.q_incl(xs, 99) - statistics_q(xs, 99)) < 1e-12
    got = {s: 0.2 for s in range(98)}; got[98] = 3.0; got[99] = 3.0
    p50, p99, loss, p99s = E.rtt_stats(got, 102)
    assert p50 == 0.2 and abs(p99 - 3.0) < 1e-12 and abs(loss - 100 * 2 / 102) < 1e-12 and math.isinf(p99s)
    assert E.rtt_thresh(0.24) == 2.0 and E.rtt_thresh(1.5) == 3.0
    assert E.rtt_ok(1.99, 0.24, 0.0) == 1.0 and E.rtt_ok(2.01, 0.24, 0.0) == 0.0
    assert E.rtt_ok(2.9, 1.5, 1.0) == 1.0 and E.rtt_ok(2.9, 1.5, 1.01) == 0.0 and E.rtt_ok(3.1, 1.5, 0.0) == 0.0
    assert E.rtt_ok(nan, 0.24, 100.0) == 0.0 and math.isnan(E.rtt_ok(1.0, nan))
    g, sent = E.parse_ping(ping_text(10, {3}, lambda s: 0.5) + "64 bytes from 10.10.0.1: seq=2 ttl=64 time=9.000 ms "
                           "(DUP!)\n", 99)
    assert sent == 10 and sorted(g) == [0, 1, 2, 4, 5, 6, 7, 8, 9] and g[2] == 0.5
    g, sent = E.parse_ping("64 bytes from 10.10.0.1: seq=0 ttl=64 time=0.300 ms\n", 200)
    assert sent == 200 and g == {0: 0.3}
    assert E.ping_cmd(40) == "ping -c 200 -i 0.2 -W 1 -w 42 10.10.0.1"
    # dips: 39 windows starting at 0.5 .. 38.5 s; all-active 18-28 s -> windows starting 18.5 .. 26.5 (9 windows)
    starts = [0.5 + j for j in range(39)]
    ratios = [1.0] * 39
    for j, v in ((18, 0.94), (19, 0.89), (20, 0.70), (21, 0.95), (2, 0.5), (30, 0.85)):
        ratios[j] = v      # windows starting 18.5, 19.5, 20.5, 21.5 (inside), 2.5 and 30.5 (outside)
    ovl, al = E.dip_metrics(ratios, starts, (18.0, 28.0))
    assert ovl == [3 / 9, 2 / 9, 1 / 9], ovl     # 0.95 itself is not below 0.95
    assert al == [5 / 39, 4 / 39, 2 / 39], al   # 0.94 0.89 0.70 0.5 0.85 / 0.89 0.70 0.5 0.85 / 0.70 0.5
    ovl, al = E.dip_metrics([], [], (18.0, 28.0))
    assert all(math.isnan(x) for x in ovl + al)
    assert E.dip_metrics([0.5], [17.9], (18.0, 28.0))[0] == [nan] * 3 or all(
        math.isnan(x) for x in E.dip_metrics([0.5], [17.9], (18.0, 28.0))[0])
    assert E.dip_metrics([0.5], [27.0], (18.0, 28.0))[0] == [1.0, 1.0, 1.0]
    # SUMMARY43 with known W/T/L
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "res.csv")
        ref, ref2, X = ("vfrot", "0.8_1_2_0.94"), ("lqr_orig", "T60_paperK"), ("vfrot", "0.8_1_2_0.94_B24000")
        vals = {ref: {"slo_ok_worst": [0.9] * 4, "v2_rtt_p99_ms": [1.0] * 4, "flooder_total_gbps": [16, 17, 16, 17]},
                ref2: {"slo_ok_worst": [0.92] * 4, "v2_rtt_p99_ms": [1.2] * 4, "flooder_total_gbps": [32, 31, 33, 32]},
                X: {"slo_ok_worst": [0.95, 0.9, 0.85, 0.97], "v2_rtt_p99_ms": [0.5, 2.0, 2.0, 1.0],
                    "flooder_total_gbps": [20, 21, 22, 21]}}
        with open(path, "w") as f:
            w = csvmod.writer(f); w.writerow(SUMCOLS)
            for rep in range(1, 5):
                for c, v in vals.items():
                    row = {m: 0.5 for m in SUMCOLS[4:]}
                    row.update({k: x[rep - 1] for k, x in v.items()})
                    row["slo_ok_worst_with_rtt"] = row["slo_ok_worst"]
                    w.writerow([rep, 0, c[0], c[1]] + [row[m] for m in SUMCOLS[4:]])
                w.writerow([rep, 1, "alone", "na"] + [1.0] * len(SUMCOLS[4:-3]) + [1.0 if rep < 4 else 0.0, 0.1, 0.0])
        order, stats, diffs, alone = E.summary_data(path)
        d = diffs[ref][X]
        assert d["slo_ok_worst"][3] == (2, 1, 1), d["slo_ok_worst"]
        assert d["v2_rtt_p99_ms"][3] == (1, 1, 2), d["v2_rtt_p99_ms"]   # lower is better
        assert d["flooder_total_gbps"][3] == (4, 0, 0)
        ds = [0.05, 0.0, -0.05, 0.07]
        assert abs(d["slo_ok_worst"][4] - scipy.stats.ttest_rel(vals[X]["slo_ok_worst"], vals[ref]["slo_ok_worst"])
                   .pvalue) < 1e-9
        assert abs(d["slo_ok_worst"][0] - statistics_mean(ds)) < 1e-12
        assert diffs[ref2][ref]["flooder_total_gbps"][3] == (0, 0, 4)
        assert diffs[ref2][X]["slo_ok_worst"][3] == (2, 0, 2)
        assert E.frontier(order, stats) == [ref2, X, ref]
        txt = E.summarize(path)
        assert "SUMMARY43" in txt and "PAIRED vs vfrot 0.8_1_2_0.94" in txt and "PAIRED vs lqr_orig T60_paperK" in txt
        assert any("ok_worst" in l and "0.8_1_2_0.94_B24000" in l and "W/T/L 2/1/1" in l for l in txt.splitlines())
        assert any("rtt_p99" in l and "0.8_1_2_0.94_B24000" in l and "W/T/L 1/1/2" in l for l in txt.splitlines())
        fr = txt[txt.index("FRONTIER"):].splitlines()[2:5]
        assert "T60_paperK" in fr[0] and "B24000" in fr[1] and "0.8_1_2_0.94 " in fr[2] + " ", fr
        assert "ALONE controls: 4 slots, RTT SLO passed in 3/4" in txt
    finally:
        shutil.rmtree(tmp)
    print("CHECK 4 OK: q_incl == statistics.quantiles; RTT p50/p99/loss, threshold max(2 x ref p99, 2 ms), loss <= 1 % "
          "(1.01 % fails), DUP/summary handling in parse_ping; dips on a synthetic series: 18-28 s 3/9, 2/9, 1/9 and "
          "whole run 5/39, 4/39, 2/39 (0.95 itself is not a dip); SUMMARY43 with known W/T/L (ok_worst 2/1/1, rtt_p99 "
          "lower-is-better 1/1/2), p == scipy ttest_rel, both paired references, frontier order, alone RTT line")


def statistics_q(xs, pct):
    import statistics
    return statistics.quantiles(xs, n=100, method="inclusive")[pct - 1]


def statistics_mean(xs):
    import statistics
    return statistics.mean(xs)


# ---------------- Part 5: refusals, fail fast, dry main(), run43.sh ----------------

def fake_result(m, p, rep):
    r = [0.5] * len(E.RESULT_NAMES)
    r[rcol("flooder_total_gbps")] = 10.0 + 3 * E.CONFIGS.index((m, p)) + 0.1 * rep if m != "alone" else 0.0
    r[rcol("flooder_wait_timeouts")] = 0.0
    r[rcol("tick_mean_s")] = 0.25
    r[rcol("steal_mean")] = r[rcol("steal_max")] = 0.0
    return tuple(r)


def fake_ts_row():
    return tuple([1.0] * E.TS_CTL + [1, 0.2, 0])


class DryMain:
    # every side effect of exp43.main() stubbed; records the call order
    def __init__(self, tmp, calib=CALIB_SIM, ref_fail_rep=None, nproc=6):
        self.tmp, self.calib, self.ref_fail_rep, self.nproc = tmp, calib, ref_fail_rep, nproc
        self.calls, self.sleeps, self.runs, self.swaps = [], [], [], 0

    def __enter__(self):
        cpath = os.path.join(self.tmp, "calib.json")
        if self.calib is not None:
            json.dump(self.calib, open(cpath, "w"))
        elif os.path.exists(cpath):
            os.remove(cpath)
        stubs = {
            "CSV": os.path.join(self.tmp, "results_dyn43.csv"), "TS_CSV": os.path.join(self.tmp, "ts_dyn43.csv"),
            "REF_FILE": os.path.join(self.tmp, "ref_victim43.txt"), "LOG": os.path.join(self.tmp, "exp43.log"),
            "CALIB": cpath, "REPS": 2,
            "host_cpus": lambda: (self.nproc, self.nproc),
            "sudo": lambda a: self.calls.append(("sudo", a)) or "",
            "check_servers": lambda: self.calls.append(("servers",)),
            "check_guests": lambda: self.calls.append(("guests",)),
            "resolve_vhost": lambda: self.calls.append(("vhost",)),
            "vnet": lambda dom: "vnet" + dom, "attach": lambda dev, cap: {"dev": dev, "prog": 1, "cfg": dev, "cnt": dev,
                                                                       "kind": "limit"},
            "restore_flooders": lambda: [], "vhost_ns": lambda p: 0, "set_cap": lambda d, c: None,
            "dropfrac_flooders": self.dropfrac, "reference": self.reference, "run": self.run,
        }
        self.saved = {k: getattr(E, k) for k in stubs}
        for k, v in stubs.items():
            setattr(E, k, v)
        self.real_sleep = time.sleep
        time.sleep = lambda s: self.sleeps.append(s)
        E.VH.update({g: "x" for g in F})
        return self

    def __exit__(self, *a):
        time.sleep = self.real_sleep
        for k, v in self.saved.items():
            setattr(E, k, v)

    @contextlib.contextmanager
    def dropfrac(self):
        self.swaps += 1; self.calls.append(("swap_in",))
        yield
        self.calls.append(("swap_out",))

    def reference(self, rep, procs):
        self.calls.append(("reference", rep))
        if rep == self.ref_fail_rep:
            raise SystemExit(f"reference ping failed (rep {rep}, tenant5): 150 replies of 200 probes, loss 25.0%")
        return dict(REFS), 55.0, dict(RREF)

    def run(self, m, p, refs, base, procs, rref):
        self.calls.append(("run", m, p)); self.runs.append((m, p))
        rep = sum(1 for c in self.calls if c[0] == "reference")
        return fake_result(m, p, rep), [fake_ts_row(), fake_ts_row()]


def run_main(dm):
    E.LQR.clear(); E.LQO.clear(); E.VFROT.clear()
    out = io.StringIO()
    with dm, contextlib.redirect_stdout(out):
        E.main()
    return out.getvalue()


def main_exit(dm, *words):
    E.LQR.clear(); E.LQO.clear(); E.VFROT.clear()
    with dm:
        return expect_exit(E.main, *words)


def part5():
    tmp = tempfile.mkdtemp()
    try:
        # outputs exist
        for name in ("results_dyn43.csv", "ts_dyn43.csv", "ref_victim43.txt", "exp43.log"):
            d = os.path.join(tmp, name + ".d"); os.mkdir(d)
            open(os.path.join(d, name), "w").close()
            dm = DryMain(d)
            main_exit(dm, "REFUSING TO START", name)
            assert not dm.calls
        d = os.path.join(tmp, "n"); os.mkdir(d)
        main_exit(DryMain(d, nproc=4), "REFUSING TO START", "nproc = 4")
        main_exit(DryMain(d, calib=None), "REFUSING TO START", "not found")
        main_exit(DryMain(d, calib=dict(CALIB_SIM, verdict="FAIL", checks=[["x", "FAIL", "bad"]])),
                  "REFUSING TO START", "verdict FAIL")
        # K[0] >= 0: refused BEFORE sudo, servers, attach or any traffic
        saved = E.lqo_gain_zoh
        try:
            E.lqo_gain_zoh = lambda *a, **k: (0.0092, 0.0087)
            dm = DryMain(d)
            main_exit(dm, "REFUSING TO START", "K[0] >= 0", "T60_zoh")
            assert dm.calls == [] and dm.sleeps == []
        finally:
            E.lqo_gain_zoh = saved
        # exp42's plain T60 (Euler ARE) is not accepted
        expect_exit(lambda: E.setup_configs([("lqr_orig", "T60")], CALIB_SIM), "malformed")
        expect_exit(lambda: E.setup_configs([("lqr_orig", "T60_euler")], CALIB_SIM), "malformed")
        # FAIL FAST: the rep-1 reference ping refusal comes before any slot and before any 600 s rest
        d2 = os.path.join(tmp, "ff"); os.mkdir(d2)
        dm = DryMain(d2, ref_fail_rep=1)
        main_exit(dm, "reference ping failed")
        assert dm.runs == [] and E.SESSION_REST_S not in dm.sleeps and sum(dm.sleeps) < 60, dm.sleeps
        assert [c[0] for c in dm.calls][:4] == ["sudo", "servers", "guests", "vhost"]
        assert ("reference", 1) in dm.calls and not any(c[0] == "run" for c in dm.calls)
        # dry main(): 2 repeats
        d3 = os.path.join(tmp, "dry"); os.mkdir(d3)
        dm = DryMain(d3)
        txt = run_main(dm)
        assert len(dm.runs) == 2 * 17
        for rep in range(2):
            seq = dm.runs[rep * 17:(rep + 1) * 17]
            assert seq[0::2] == [("alone", "na")] * 9 and sorted(seq[1::2]) == sorted(E.CONFIGS)
        random.seed(7); o1 = E.CONFIGS[:]; random.shuffle(o1); o2 = E.CONFIGS[:]; random.shuffle(o2)
        assert dm.runs[1:17:2] == o1 and dm.runs[18:34:2] == o2
        assert dm.swaps == 2 * 4
        i = 0
        for c in dm.calls:   # each lqr_orig run is inside swap_in / swap_out, nothing else is
            if c[0] == "run":
                prev = [x[0] for x in dm.calls[:dm.calls.index(c, i)] if x[0] in ("swap_in", "swap_out")]
                inside = bool(prev) and prev[-1] == "swap_in"
                assert inside == (c[1] == "lqr_orig"), c
                i = dm.calls.index(c, i) + 1
        assert dm.sleeps.count(E.SESSION_REST_S) == 1
        lines = open(os.path.join(d3, "results_dyn43.csv")).read().splitlines()
        assert lines[0] == E.HEADER and len(lines) == 1 + 34
        assert all(len(l.split(",")) == len(E.HEADER.split(",")) for l in lines)
        tsl = open(os.path.join(d3, "ts_dyn43.csv")).read().splitlines()
        assert tsl[0] == E.TS_HEADER and len(tsl) == 1 + 68
        for w in ("LQR_ORIG T60_zoh: target", "K = [-0.", "ARE on the ZOH plant", "PAPER K, unchanged",
                  "VFROT 0.8_1_2_0.94_B32000:", "ceiling (BMAX) 32000", "LATENCY:", "PING CMD V2", "DIPS:",
                  "EXPECTED DURATION: 17 slots/repeat", "SUMMARY43", "FRONTIER", "PAIRED vs vfrot 0.8_1_2_0.94",
                  "PAIRED vs lqr_orig T60_paperK", "ALONE controls: 18 slots", "WARNINGS ("):
            assert w in txt, w
        # run43.sh against fake commands
        sh_checks = run43_checks(tmp)
    finally:
        shutil.rmtree(tmp)
    print("CHECK 5 OK: exp43.main() refuses on existing outputs (each of the 4), nproc 4, missing / FAIL calibration, "
          "K[0] >= 0 (before sudo, servers or attach) and exp42's plain T60; FAIL FAST: a rep-1 reference ping "
          "refusal stops main() before any slot and before the 600 s rest; dry main(): 2 x 17 slots, seed-7 order, "
          "xdp_dropfrac only around the 4 lqr_orig slots, one 600 s rest, CSV/ts lines with the header's field "
          f"count, SUMMARY43/FRONTIER/PAIRED/ALONE in the log; run43.sh: {sh_checks}")


FAKE_BIN = {
    "nproc": "#!/bin/sh\necho ${FAKE_NPROC:-6}\n",
    "ss": "#!/bin/sh\nfor p in 5201 5202 5203 5204 5205 5206; do echo \"LISTEN 0 5 0.0.0.0:$p 0.0.0.0:*\"; done\n",
    "pgrep": "#!/bin/sh\nexit 1\n",
    "sudo": "#!/bin/sh\necho \"$@\" >> \"$FAKE_LOG\"\nexit 1\n",
    "nohup": "#!/bin/sh\necho nohup \"$@\" >> \"$FAKE_LOG\"\nexit 0\n",
}


def run43(tmp, name, calib=CALIB_REAL, extra_py="", outputs=(), env=None):
    d = os.path.join(tmp, "sh_" + name); os.mkdir(d)
    b = os.path.join(d, "bin"); os.mkdir(b)
    for k, v in FAKE_BIN.items():
        open(os.path.join(b, k), "w").write(v); os.chmod(os.path.join(b, k), 0o755)
    shutil.copy(os.path.join(HERE, "run43.sh"), d)
    src = open(os.path.join(HERE, "exp43.py")).read() + extra_py
    open(os.path.join(d, "exp43.py"), "w").write(src)
    if calib is not None:
        json.dump(calib, open(os.path.join(d, "calib42_result.json"), "w"))
    for o in outputs:
        open(os.path.join(d, o), "w").close()
    log = os.path.join(d, "fake.log")
    e = dict(os.environ, PATH=b + ":" + os.environ["PATH"], FAKE_LOG=log, PYTHONDONTWRITEBYTECODE="1", **(env or {}))
    p = subprocess.run(["sh", os.path.join(d, "run43.sh"), "6"], capture_output=True, text=True, env=e, cwd=d,
                       timeout=120)
    calls = open(log).read() if os.path.exists(log) else ""
    return p.returncode, p.stdout + p.stderr, calls, d


def run43_checks(tmp):
    rc, out, calls, d = run43(tmp, "ok")
    assert rc == 1 and "-v" in calls and "nohup" not in calls, (rc, out, calls)   # fake sudo -v fails: stops there
    assert "re-identified K (ZOH, T60) = [-0.0133, -0.0012] (K[0] < 0: OK)" in out, out
    assert "17 slots (8 configs + 9 alone)" in out, out
    assert not os.path.exists(os.path.join(d, "exp43.log"))
    rc, out, calls, d = run43(tmp, "kpos", extra_py="\ndef lqo_gain_zoh(*a, **k):\n    return (0.0092, 0.0087)\n")
    assert rc == 1 and "K[0] >= 0" in out and calls == "", (out, calls)
    rc, out, calls, d = run43(tmp, "nocal", calib=None)
    assert rc == 1 and "calib42_result.json not found" in out and calls == ""
    rc, out, calls, d = run43(tmp, "fail", calib=dict(CALIB_REAL, verdict="FAIL"))
    assert rc == 1 and "verdict FAIL" in out and calls == ""
    rc, out, calls, d = run43(tmp, "nproc", env={"FAKE_NPROC": "4"})
    assert rc == 1 and "nproc = 4" in out and calls == ""
    for o in OUTS:
        rc, out, calls, d = run43(tmp, "out_" + o.replace(".", "_"), outputs=(o,))
        assert rc == 1 and f"{o} already exists" in out and calls == ""
    return ("refuses (exit 1, sudo never called) on each existing output, nproc 4, missing calib42_result.json, "
            "verdict FAIL and K[0] >= 0; with everything OK it prints K = [-0.0133, -0.0012], 17 slots, then asks "
            "sudo -v itself (fake sudo fails -> nothing started)")


# ---------------- Part 6 ----------------

def part6(before):
    after = sha(list(SNAP))
    bad = [n for n in SNAP if after[n] != SNAP[n] or before[n] != SNAP[n]]
    assert not bad, f"protected files changed or missing: {bad}"
    present = [n for n in OUTS if os.path.exists(os.path.join(HERE, n))]
    assert not present, present
    print(f"CHECK 6 OK: sha256 of {len(SNAP)} exp39-exp42 / calib42 / XDP files == the snapshot taken when exp43 was "
          f"built (before and after this test); no exp43 output in the testbed")


def main():
    before = sha(list(SNAP))
    saved = (E.CSV, E.TS_CSV, E.REF_FILE, E.LOG, E.CALIB, E.REPS)
    try:
        part0(); part1(); part2(); part3(); part4(); part5()
    finally:
        E.CSV, E.TS_CSV, E.REF_FILE, E.LOG, E.CALIB, E.REPS = saved
    part6(before)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
