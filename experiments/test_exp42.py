#!/usr/bin/env python3
# Pure-python self-test for exp42.py, calib42.py, run42.sh and calibrate42.sh: no VM, no sudo, no network, no
# traffic, no sleeping (simulated clock), nothing attached. Optionally recompiles xdp_dropfrac.c into a temp dir.
# Part 0: configs, headers, durations.
# Part 1: lqr_orig pure functions: filter chain, ARE gain (paper K), soft limit, quantization, drop rule (Python
#         model and the C source), paired t-test.
# Part 2: the REAL exp42.run() on a simulated testbed: vfrot == REAL exp40.run(), lqr 85_4 == REAL exp39.run() tick by
#         tick; lqr_orig (controller on every 2nd tick, set_drop, release before the flooder wait, timeout, tracking
#         error, eBPF accounting); XDP swap in/out.
# Part 2b: REAL sudo/attach/load/swap/restore_flooders, calib42.main and exp42.main against a command-level fake
#         host (fake sudo, nothing executed): the 'ip' exit 2 without -force, a failing second swap, Ctrl+C.
# Part 3: the REAL exp42.main() with every side effect stubbed (slot order, swaps only around lqr_orig, CSV/ts,
#         deviation line, warnings, SUMMARY42 with known answers), refusals of main(), run42.sh, calibrate42.sh;
#         calib42 fit/grades and the REAL calib42.main() on a simulated plant (PASS and FAIL cases);
#         exp39/exp40/exp41 files unchanged, no exp42/calib42 output in the testbed.
import sys
sys.dont_write_bytecode = True
import contextlib, hashlib, io, json, math, os, random, shutil, signal, statistics, struct, subprocess, tempfile, time
sys.argv = sys.argv[:1]
import numpy as np
import exp39, exp40, exp42, calib42
import scipy.linalg, scipy.stats   # test-only cross-checks

E, C = exp42, calib42
HERE = os.path.dirname(os.path.abspath(E.__file__))
REPS = 6
ALONE = ("alone", "na")
REFS = {"V1": 9.0, "V2": 9.0}
OUTS = ("results_dyn42.csv", "ts_dyn42.csv", "ref_victim42.txt", "exp42.log")
COUTS = ("calib42_run.log", "calib42_ts.csv", "calib42_result.json")
OLD_FILES = tuple(f"{a}{n}{b}" for n in (39, 40, 41) for a, b in
                  (("exp", ".py"), ("run", ".sh"), ("test_exp", ".py"), ("results_dyn", ".csv"), ("ts_dyn", ".csv"),
                   ("ref_victim", ".txt"), ("exp", ".log"))) + ("xdp_limit.c", "xdp_limit.o", "pilot41.py")
FLOOD_GBIT = 6.0
F = E.FLOODERS
CPU_PER_GBIT = 10.0      # simulated vhost CPU, % of one core per passed Gbit/s
CONFIGS = [("vfrot", "0.8_1_2_0.94"), ("lqr", "85_4"), ("lqr_orig", "T40"), ("lqr_orig", "T60"),
           ("lqr_orig", "T80"), ("lqr_orig", "T60_paperK")]
CALIB_OK = {"verdict": "PASS", "checks": [["x", "PASS", "ok"]], "cpu_ref": 50.0, "tau": 0.8, "b": 40.0,
            "written": "test"}
nan = float("nan")
SHA_BEFORE = None


def sha(names):
    return {n: (hashlib.sha256(open(os.path.join(HERE, n), "rb").read()).hexdigest()
                if os.path.exists(os.path.join(HERE, n)) else None) for n in names}


def same(a, b):
    return a == b or (isinstance(a, float) and isinstance(b, float) and a != a and b != b)


def rcol(n):
    return E.RESULT_NAMES.index(n)


def expect_exit(fn, *words):
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            fn()
    except SystemExit as e:
        msg = str(e)
        assert all(w in msg for w in words), (msg, words)
        return msg
    raise AssertionError(f"no SystemExit, expected {words}")


# ---------------- fake testbed ----------------

def parse_cmd(cmd):
    w = cmd.split()
    delay = float(w[1].rstrip(";")) if w[0] == "sleep" else 0.0
    par = int(w[w.index("-P") + 1]) if "-P" in w else 1
    rate = float(w[w.index("-b") + 1].rstrip("G")) * par if "-b" in w else None
    return {"delay": delay, "secs": float(w[w.index("-t") + 1]), "rate": rate}


class FakeProc:
    def __init__(self, sim, name, end):
        self.sim, self.name, self.end, self.terminated = sim, name, end, False
    def poll(self): return 0 if self.sim.clock[0] >= self.end else None
    def wait(self, timeout=None):
        self.sim.events.append(("wait", self.name, self.sim.clock[0]))
        if timeout is not None and self.end > self.sim.clock[0] + timeout:
            self.sim.clock[0] += timeout
            raise subprocess.TimeoutExpired(self.name, timeout)
        self.sim.clock[0] = max(self.sim.clock[0], self.end); return 0
    def terminate(self): self.terminated = True


class Sim:
    # victim v delivers its -b rate; a flooder offers FLOOD_GBIT inside its iperf3 window and passes
    # min(offer, cap) under xdp_limit (cap 0 = all) or offer x (1 - drop_bp / 1e4) under xdp_dropfrac;
    # vhost CPU of a flooder = CPU_PER_GBIT x its passed Gbit/s (as of the last counter read);
    # hang: flooders whose ssh never ends; kinds: cfg id -> "limit"/"dropfrac" (set by fake load())
    def __init__(self, seed=1, read_cost=0.002, hang=()):
        self.rng, self.clock, self.read_cost, self.hang = random.Random(seed), [1000.0], read_cost, set(hang)
        self.launches, self.events, self.capb, self.cum, self.kinds, self.procs = {}, [], {}, {}, {}, []
        self.cpu_acc, self.ids = [0, 0, 0], 100

    def sudo(self, args):
        if args == ["true"]:
            return ""
        assert args[:3] == ["bpftool", "map", "update"], args
        self.capb[args[4]] = struct.unpack("<Q", bytes.fromhex("".join(args[-8:])))[0]
        self.events.append(("set", args[4], self.capb[args[4]], self.clock[0]))
        return ""

    def g_of(self, d):
        return str(d["cnt"]).split(":")[0][3:]

    def counters(self, d):
        self.clock[0] += self.read_cost
        now, g = self.clock[0], self.g_of(d)
        c = self.cum.setdefault(d["cnt"], [0.0, 0.0, now, 0.0, 0.0])
        L = self.launches.get(g, [])
        if g in E.VICTIMS:
            c[0] = sum(p["rate"] * 1e9 / 8 * max(0.0, min(now - t - p["delay"], p["secs"])) for t, p in L)
        else:
            dt, c[2] = now - c[2], now
            off = sum(FLOOD_GBIT * 1e9 / 8 * dt for t, p in L if t + p["delay"] <= now < t + p["delay"] + p["secs"])
            v = self.capb.get(d["cfg"], 0)
            if d.get("kind") == "dropfrac":
                passed = off * (1.0 - v / 10000.0)
            else:
                passed = off if v == 0 else min(off, v * dt)
            c[0] += passed; c[1] += off - passed
            c[3] += passed / 1500.0; c[4] += (off - passed) / 1500.0
        return {0: int(c[3]), 1: int(c[4]), 2: int(c[0]), 3: int(c[1])}

    def vhost_ns(self, path):
        # passed bytes of every program instance of this flooder so far (as of the last counter read)
        g = path[2:]
        passed = sum(x[0] for k, x in self.cum.items() if str(k).split(":")[0] == f"cnt{g}")
        return int(passed * 8 / 1e9 * CPU_PER_GBIT / 100.0 * 1e9)

    def cpu(self):
        self.cpu_acc[0] += 100; self.cpu_acc[1] += 40
        return tuple(self.cpu_acc)

    def ssh(self, guest, cmd, out):
        now = self.clock[0]
        self.events.append(("ssh", guest, cmd, now))
        p = parse_cmd(cmd)
        self.launches.setdefault(guest, []).append((now, p))
        gb = p["rate"] if guest in E.VICTIMS else FLOOD_GBIT
        out.write(json.dumps({"end": {"sum_received": {"bytes": int(gb * 1e9 / 8 * p["secs"]),
                                                       "bits_per_second": gb * 1e9}}}))
        out.flush()
        end = now + p["delay"] + p["secs"] + 0.3 + (1e6 if guest in self.hang else 0)
        pr = FakeProc(self, guest, end); self.procs.append(pr)
        return pr

    def sleep(self, s):
        self.clock[0] += max(0.0, s) + self.rng.uniform(0.0, 0.04)

    def load(self, dev, kind):
        # fake exp42.load: a new program with new map ids on dev
        g = dev[4:]; self.ids += 1
        self.kinds[dev] = kind
        return {"dev": dev, "prog": self.ids, "cfg": f"cfg{g}:{self.ids}", "cnt": f"cnt{g}:{self.ids}", "kind": kind}

    def vnet(self, dom):
        return "vnet" + [g for g, v in E.GUESTS.items() if v[0] == dom][0]

    def attach(self, dev, cap):
        # fake exp42.attach (restore_flooders): an xdp_limit stays, anything else becomes a new xdp_limit
        g = dev[4:]
        if self.kinds.get(dev) == "limit" and E.IF.get(g, {}).get("kind") == "limit":
            return dict(E.IF[g])
        return self.load(dev, "limit")

    def prog_maps(self, dev, kind):
        if self.kinds.get(dev) != kind:
            raise SystemExit(f"{dev}: XDP prog is not {kind}")
        g = dev[4:]
        return {"dev": dev, "prog": 0, "cfg": f"cfg{g}:x", "cnt": f"cnt{g}:x", "kind": kind}


@contextlib.contextmanager
def patched(mod, sim, extra=None):
    names = ("sudo", "counters", "cpu", "ssh") + tuple(extra or {})
    saved = {k: getattr(mod, k) for k in names}
    real_time = (time.monotonic, time.sleep)
    mod.sudo, mod.counters, mod.cpu, mod.ssh = sim.sudo, sim.counters, sim.cpu, sim.ssh
    for k, v in (extra or {}).items():
        setattr(mod, k, v)
    time.monotonic, time.sleep = (lambda: sim.clock[0]), sim.sleep
    try:
        yield
    finally:
        time.monotonic, time.sleep = real_time
        for k, v in saved.items():
            setattr(mod, k, v)


def fake_if(mod, sim=None, kind="limit"):
    mod.IF.clear(); mod.last_cap.clear()
    if mod is E:
        E.last_drop.clear()
    for g in mod.VICTIMS + mod.FLOODERS:
        mod.IF[g] = {"dev": f"vnet{g}", "prog": 0, "cfg": f"cfg{g}", "cnt": f"cnt{g}"}
        if mod is E:
            mod.IF[g]["kind"] = "limit"
            if sim is not None:
                sim.kinds[f"vnet{g}"] = "limit"
    if mod is E:
        for g in F:
            E.VH[g] = f"vh{g}"


def e_extra(sim):
    return {"vhost_ns": sim.vhost_ns, "load": sim.load, "prog_maps": sim.prog_maps, "vnet": sim.vnet,
            "attach": sim.attach}


def sim_run(mod, mode, param, sim, dropfrac=False):
    extra = e_extra(sim) if mod is E else None
    with patched(mod, sim, extra):
        fake_if(mod, sim)
        with contextlib.redirect_stdout(io.StringIO()):
            if dropfrac:
                with E.dropfrac_flooders():
                    r, ts = mod.run(mode, param, dict(REFS), 50.0, [])
            else:
                r, ts = mod.run(mode, param, dict(REFS), 50.0, [])
    return r, ts, sim.events


def setup_all(calib=None):
    E.LQR.clear(); E.LQO.clear(); E.VFROT.clear()
    E.setup_configs(CONFIGS, calib or CALIB_OK)


# ---------------- Part 0 ----------------

def part0():
    assert E.CONFIGS == CONFIGS and E.PAIRED_REFS == [CONFIGS[0], CONFIGS[1]]
    setup_all()
    assert E.LQR["85_4"][:2] == exp39.parse_lqr("85_4") and (E.LQR["85_4"][2] == exp39.lqr_gain(4.0)).all()
    assert E.VFROT[("vfrot", "0.8_1_2_0.94")] == exp40.parse_vfrot("vfrot", "0.8_1_2_0.94")
    t40, k40, f40, p40 = E.LQO["T40"]; t60p, k60p, _, p60p = E.LQO["T60_paperK"]
    assert abs(t40 - 20.0) < 1e-12 and f40 == 0.4 and not p40 and p60p and k60p == E.K_PAPER and abs(t60p - 30) < 1e-12
    assert E.LQO["T80"][0] == 40.0
    for m, p, w in (("lqr_orig", "X40", "malformed"), ("lqr_orig", "T40_K", "malformed"), ("lqr_orig", "T5",
                    "out of range"), ("vffair", "0.8_1_2_0.94_F0.5", "not supported"), ("lqr", "96_4", "out of range")):
        expect_exit(lambda: E.setup_configs([(m, p)], CALIB_OK), w)
    expect_exit(lambda: E.setup_configs([CONFIGS[0], CONFIGS[0]], CALIB_OK), "duplicate")
    assert E.TS_HEADER.split(",")[:4 + E.TS_VH] == exp40.TS_HEADER.split(",")
    assert E.HEADER.startswith(exp40.HEADER + ",")
    assert len(E.TS_HEADER.split(",")) == 4 + E.TS_CTL + 1
    n, per, tot = E.expected_duration(len(CONFIGS), REPS)
    _, perm, totm = E.measured_duration(len(CONFIGS), REPS)
    assert n == 13 and per <= 15 * 60 and perm <= 15 * 60
    print(f"CHECK 0 OK: 6 configs, lqr 85_4 gain == exp39's, vfrot params == exp40's, lqr_orig targets 40/60/80 % of "
          f"cpu_ref (paperK keeps K {E.K_PAPER}), malformed params refused; headers extend exp40's; 13 slots/repeat, "
          f"~{per / 60:.1f} min/repeat (exp39 measured rate ~{perm / 60:.1f}), R=6 ~{tot / 3600:.2f} h "
          f"(~{totm / 3600:.2f} h); calib42 ~{C.expected_s() / 60:.1f} min")


# ---------------- Part 1 ----------------

def part1():
    # filter chain
    s = E.lqo_new()
    seq = [10.0, 20.0, 60.0, 30.0, 40.0, 50.0, 0.0]
    meds = [10.0, 15.0, 20.0, 25.0, 30.0, 40.0, 40.0]   # median of the last <= 5 samples
    prev = None
    for x, m in zip(seq, meds):
        eff = E.lqo_filter(s, x)
        trend = 0.0 if prev is None else m - prev
        assert abs(s["smooth"] - m) < 1e-12 and abs(eff - (0.7 * m + 0.3 * (m + trend))) < 1e-12, (x, eff)
        prev = m
    assert s["win"] == [60.0, 30.0, 40.0, 50.0, 0.0]
    # lqo_step: e, edot, u = -K x on a known state
    s = E.lqo_new(); K = E.K_PAPER
    b1, eff1, e1 = E.lqo_step(s, 30.0, 25.0, K)     # e = 5, edot 0, u = 0.2562 * 5 -> clip 0.95
    assert eff1 == 30.0 and e1 == 5.0 and b1 == 9500
    s = E.lqo_new()
    b1, _, e1 = E.lqo_step(s, 25.2, 25.0, K)        # e = 0.2: u = 0.05124, inside the margin
    w = 0.2 / 0.5; hard = min((0.2 / 50) ** 0.5 * 0.95, 0.95)
    exp_u = max(0.2562 * 0.2, (1 - w) * 0.2562 * 0.2 + w * hard)
    assert b1 == E.lqo_quant(exp_u), (b1, exp_u)
    b2, eff2, e2 = E.lqo_step(s, 25.2, 25.0, K)     # same sample: smooth 25.2, trend 0, edot 0
    assert abs(e2 - 0.2) < 1e-12 and b2 == b1
    s2 = E.lqo_new(); E.lqo_step(s2, 20.0, 25.0, K); bb, eff, ee = E.lqo_step(s2, 30.0, 25.0, K)
    # median(20, 30) = 25, trend 5, eff 26.5, e 1.5, edot (1.5 - (-5)) / 0.5 = 13
    assert abs(eff - 26.5) < 1e-12 and abs(ee - 1.5) < 1e-12
    u = 0.2562 * 1.5 + 0.3031 * 13.0
    assert bb == E.lqo_quant(E.lqo_soft(u, 26.5, 25.0)) == 9500
    print("CHECK 1 OK: filter chain: median of the last 5 (2-4 at start-up), trend = smooth(k) - smooth(k-1) (0 at "
          "the first step), CPU_eff = 0.7 smooth + 0.3 (smooth + trend); edot = de / 0.5 (0 at the first step); "
          "u = -K x with K = paper K on hand-computed states")

    # ARE gain
    for tgt in (1.0, 3.0, 5.0, 20.0, 60.0):
        k = E.lqo_gain(2.0, 10.0, tgt)
        assert (round(k[0], 4), round(k[1], 4)) == (-0.2562, -0.3031), (tgt, k)
    for tau, b, tgt in ((2.0, 10.0, 5.0), (0.8, 40.0, 20.0), (0.3, 120.0, 40.0), (5.0, 3.0, 2.0)):
        A, B = E.lqo_plant(tau, b)
        al = 10.0 / max(tgt, 1.0)
        Q, R = np.diag([100 * al, 10 * al]), np.array([[0.01]])
        P = scipy.linalg.solve_discrete_are(A, B, Q, R)
        Ks = np.linalg.solve(R + B.T @ P @ B, B.T @ P @ A)[0]
        k = E.lqo_gain(tau, b, tgt)
        assert abs(k[0] - Ks[0]) < 1e-7 * max(1, abs(Ks[0])) and abs(k[1] - Ks[1]) < 1e-7 * max(1, abs(Ks[1]))
    assert abs(E.lqo_eig(2.0, 10.0, E.K_PAPER) - 0.2344) < 1e-3
    assert E.lqo_eig(2.0, 10.0, E.K_PAPER, 1.5) < 1 < E.lqo_eig(2.0, 10.0, E.K_PAPER, 1.6)
    A, B = E.lqo_plant(2.0, 10.0)
    assert np.allclose(A, [[1, 0.5], [-0.25, 0.75]]) and np.allclose(B, [[0], [-5.0]])
    print("CHECK 2 OK: ARE: tau 2, b 10, dt 0.5, R 0.01 gives K = [-0.2562, -0.3031] for targets 1-60 (alpha "
          "cancels: cheap-control limit); Riccati iteration == scipy solve_discrete_are on 4 plants; paper loop "
          "spectral radius 0.234, stable up to plant gain x1.5, unstable at x1.6")

    # soft limit and quantization
    T = 20.0
    assert E.lqo_soft(0.1, 19.0, T) == 0.1 and E.lqo_soft(-0.3, 20.0, T) == -0.3          # e <= 0: unchanged
    e = 0.5; hard_in = min((e / 40) ** 0.5 * 0.95, 0.95)
    assert abs(E.lqo_soft(0.0, 20.5, T) - hard_in) < 1e-12                                  # w = 1 at the margin
    e = 0.25; hard_in = min((e / 40) ** 0.5 * 0.95, 0.95)
    assert abs(E.lqo_soft(0.0, 20.25, T) - 0.5 * hard_in) < 1e-12                           # w = 0.5
    assert E.lqo_soft(0.9, 20.25, T) == 0.9                                                 # max(u, blend)
    e = 0.5000001; hard_out = min((e / 40) ** 0.7 * 0.95, 0.95)
    assert abs(E.lqo_soft(0.0, T + e, T) - hard_out) < 1e-12                                # above: exponent 0.7
    assert E.lqo_soft(0.0, 200.0, T) == 0.95 and E.lqo_soft(0.0, 60.0, T) == 0.95           # e = 2 target -> 0.95
    assert abs(E.lqo_soft(-1.0, 30.0, T) - (10 / 40) ** 0.7 * 0.95) < 1e-12
    for u, bp in ((-0.3, 0), (0.0, 0), (0.024, 0), (0.025, 0), (0.026, 500), (0.074, 500), (0.075, 500),
                  (0.0751, 1000), (0.5, 5000), (0.93, 9500), (0.95, 9500), (0.97, 9500), (1.5, 9500)):
        assert E.lqo_quant(u) == bp, (u, E.lqo_quant(u), bp)
    lv = np.array(E.LQO_LEVELS_BP) / 1e4
    for u in np.random.default_rng(3).uniform(-0.2, 1.2, 5000):
        assert E.lqo_quant(u) == E.LQO_LEVELS_BP[int(np.argmin(np.abs(lv - min(0.95, max(0.0, u)))))]
    assert len(E.LQO_LEVELS_BP) == 20 and E.LQO_LEVELS_BP[-1] == 9500
    print("CHECK 3 OK: soft limit: e <= 0 unchanged; inside the 0.5 margin (1-w) u + w hard (exp 0.5, w = e/0.5), "
          "above it hard (exp 0.7), result max(u, .), 0.95 cap; quantization: clip [0, 0.95], nearest of 20 levels "
          "(0.024 -> 0, tie 0.025 -> 0 as argmin, 0.026 -> 0.05, 0.97 -> 0.95) == numpy argmin on 5000 random u")

    # drop rule
    src = open(os.path.join(HERE, "xdp_dropfrac.c")).read()
    assert "if (d != 0 && (total * d) % 10000 < d) { add(1, 1); add(3, len); return XDP_DROP; }" in src
    assert "__u64 total = __sync_fetch_and_add(s, 1);" in src and "add(0, 1); add(2, len);" in src
    lim = open(os.path.join(HERE, "xdp_limit.c")).read()
    for line in ("struct { __uint(type, BPF_MAP_TYPE_ARRAY); __uint(max_entries, 4);",
                 "static __always_inline void add(__u32 idx, __u64 v) {",
                 "  __u64 *p = bpf_map_lookup_elem(&cnt, &idx);", "  if (p) __sync_fetch_and_add(p, v);",
                 "  __u64 len = (__u64)(ctx->data_end - ctx->data);"):
        assert line in lim and line in src, line
    N = 200000
    for bp in E.LQO_LEVELS_BP:
        d = [E.drop_model(t, bp) for t in range(N)]
        frac = sum(d) / N
        assert abs(frac - bp / 1e4) <= 0.001, (bp, frac)
        run = mx = 0
        for x in d:
            run = run + 1 if x else 0; mx = max(mx, run)
        if bp == 6000:
            assert mx <= 2, mx
        if bp == 0:
            assert not any(d)
    cc = shutil.which("clang")
    comp = "clang not found, compile skipped"
    if cc:
        tmp = tempfile.mkdtemp(prefix="test_exp42_bpf_")
        p = subprocess.run([cc, "-O2", "-g", "-target", "bpf", "-mcpu=v3", "-I/usr/include/aarch64-linux-gnu", "-c",
                            os.path.join(HERE, "xdp_dropfrac.c"), "-o", os.path.join(tmp, "x.o")],
                           capture_output=True, text=True)
        assert p.returncode == 0, p.stderr
        dis = subprocess.run(["llvm-objdump", "-d", os.path.join(tmp, "x.o")], capture_output=True, text=True).stdout
        assert "atomic_fetch_add" in dis and "%= 0x2710" in dis
        comp = "xdp_dropfrac.c recompiles (clang, -mcpu=v3) with atomic_fetch_add and % 10000"
        shutil.rmtree(tmp)
    print(f"CHECK 4 OK: drop rule (C source line == drop_model): all 20 levels within 0.1 % over {N} packets "
          f"(exact), max consecutive drops at 60 % = 2, bp 0 never drops; same cnt map/add()/len as xdp_limit.c; "
          f"{comp}")

    rng = random.Random(5)
    for _ in range(300):
        n = rng.randint(2, 12)
        a = [rng.gauss(0, 1) for _ in range(n)]; b = [x + rng.gauss(0.3, 1) for x in a]
        ds = [y - x for x, y in zip(a, b)]
        ref = scipy.stats.ttest_rel(b, a).pvalue
        assert abs(E.paired_p(ds) - ref) < 1e-9 * max(1, ref), (E.paired_p(ds), ref)
    assert math.isnan(E.paired_p([1.0])) and E.paired_p([0.5] * 4) == 0.0 and E.paired_p([0.0] * 4) == 1.0
    print("CHECK 5 OK: paired_p == scipy.stats.ttest_rel on 300 random samples; n < 2 nan; sd 0 -> 0 or 1")


# ---------------- Part 2 ----------------

def part2():
    setup_all()
    exp39.LQR["85_4"] = (*exp39.parse_lqr("85_4"), exp39.lqr_gain(4.0))
    for old, cfg in ((exp40, ("vfrot", "0.8_1_2_0.94")), (exp39, ("lqr", "85_4"))):
        if old is exp39:
            exp39.VFROT.clear()
        r_o, ts_o, ev_o = sim_run(old, *cfg, Sim(seed=11))
        r_n, ts_n, ev_n = sim_run(E, *cfg, Sim(seed=11))
        n = len(r_o)
        assert all(same(a, b) for a, b in zip(r_n[:n], r_o)), (cfg, r_n[:n], r_o)
        assert len(ts_n) == len(ts_o) > 150
        for a, b in zip(ts_n, ts_o):
            assert all(same(x, y) for x, y in zip(a[:E.TS_VH], b)), (cfg, a, b)
        assert [e for e in ev_n if e[0] == "set"] == [e for e in ev_o if e[0] == "set"]
        assert all(math.isnan(r_n[rcol(f"track_err{g}")]) for g in F) and math.isnan(r_n[rcol("u_release_s")])
        assert all(row[E.TS_CTL] == 0 for row in ts_n)
    print("CHECK 6 OK: controllers unchanged: exp42.run() == REAL exp40.run() for vfrot 0.8_1_2_0.94 and == REAL "
          "exp39.run() for lqr 85_4, tick by tick (exp40/exp39 ts columns, results, every cap write); no lqr_orig "
          "columns for them")

    # lqr_orig
    rec = []
    real_step = E.lqo_step
    def rec_step(state, cpu_pct, target, K):
        out = real_step(state, cpu_pct, target, K); rec.append((cpu_pct, target, K, out)); return out
    E.lqo_step = rec_step
    try:
        sim = Sim(seed=4)
        r, ts, ev = sim_run(E, "lqr_orig", "T60", sim, dropfrac=True)
    finally:
        E.lqo_step = real_step
    target, K = E.LQO["T60"][:2]
    ctl_rows = [i for i, row in enumerate(ts) if row[E.TS_CTL] == 1]
    assert ctl_rows == list(range(1, len(ts), 2)), ctl_rows[:6]       # ticks 2, 4, 6, ... (index 1, 3, ...)
    assert len(rec) == 4 * len(ctl_rows)
    states = {g: E.lqo_new() for g in F}
    for j, i in enumerate(ctl_rows):
        for gi, g in enumerate(F):
            cpu_in, tgt, KK, out = rec[4 * j + gi]
            assert tgt == target and KK == K and real_step(states[g], cpu_in, target, K) == out
            assert ts[i][E.TS_U + gi] == out[0] / 1e4 and ts[i][E.TS_EFF + gi] == out[1] and ts[i][E.TS_E + gi] == out[2]
            if i + 1 < len(ts):
                assert ts[i + 1][E.TS_U + gi] == out[0] / 1e4 and ts[i + 1][E.TS_CTL] == 0   # held on the off tick
    # inputs = vhost CPU over the 0.5 s window: D has not started at the first steps, A has
    assert rec[3][0] == 0.0 and rec[4 + 0][0] > 0
    mid = [rec[4 * j][0] for j in range(len(ctl_rows)) if 3 < ts[ctl_rows[j]][0] < 5.5]   # A alone, steady
    assert mid and all(0 <= x <= CPU_PER_GBIT * FLOOD_GBIT * 1.05 for x in mid)
    # every drop write = a controller output on the dropfrac cfg ids, then the release, then the flooder waits
    sets = [e for e in ev if e[0] == "set" and ":" in e[1]]          # writes to the dropfrac cfg maps only
    assert {e[1].split(":")[0] for e in sets} <= {f"cfg{g}" for g in F}
    t0 = [e for e in ev if e[0] == "ssh" and e[1] == "A"][0][3]
    t_last_tick = t0 + ts[-1][0]
    t_first_wait = min(e[2] for e in ev if e[0] == "wait" and e[1] in F)
    for g in F:   # the last drop written before the first flooder wait is 0 (released), the one before it was not
        mine = [e for e in sets if e[1].split(":")[0] == f"cfg{g}" and e[3] <= t_first_wait]
        last_nonzero = [e for e in mine if e[2] != 0]
        assert mine[-1][2] == 0, (g, mine[-3:])
        assert not last_nonzero or last_nonzero[-1][3] <= t_last_tick + 1e-9
        assert all(e[3] <= t_last_tick + 1e-9 or e[2] == 0 for e in mine)
    assert t_last_tick - t0 <= r[rcol("u_release_s")] <= t_first_wait - t0 and r[rcol("flooder_wait_timeouts")] == 0
    assert max(e[2] for e in sets) <= 9500 and any(e[2] > 0 for e in sets)
    assert all(e[2] in E.LQO_LEVELS_BP for e in sets)
    # tracking error, u mean, vhost mean over own windows
    for gi, g in enumerate(F):
        a, d = E.STEADY[E.SCENARIO][g]
        rows = [row for row in ts if a <= row[0] < a + d and row[E.TS_CTL] == 1]
        assert abs(r[rcol(f"track_err{g}")] - statistics.mean(abs(row[E.TS_E + gi]) for row in rows)) < 1e-12
        assert abs(r[rcol(f"u{g}_mean")] - statistics.mean(row[E.TS_U + gi] for row in rows)) < 1e-12
        allr = [row for row in ts if a <= row[0] < a + d]
        assert abs(r[rcol(f"vhost{g}_cpu_mean")] - statistics.mean(row[E.TS_VH + gi] for row in allr)) < 1e-12
    assert abs(r[rcol("track_err_mean")] - statistics.mean(r[rcol(f"track_err{g}")] for g in F)) < 1e-12
    assert r[rcol("target_cpu")] == target
    # eBPF accounting: flooder means = passed bytes of the dropfrac cnt maps / own duration (as every mode)
    for g in F:
        cum = sim.cum[[k for k in sim.cum if str(k).startswith(f"cnt{g}:")][0]]
        assert abs(r[rcol(f"flooder{g}_ebpf_gbps")] - int(cum[0]) * 8 / 1e9 / E.flooder_dur(g)) < 1e-3
    assert all(math.isnan(row[E.TS_CAP + i]) for row in ts for i in range(E.NF)) and all(
        math.isnan(row[E.TS_BUDGET]) for row in ts)
    assert all(sim.kinds[f"vnet{g}"] == "limit" for g in F) and all(E.IF[g]["kind"] == "limit" for g in F)
    # a flooder that never ends: release, 60 s timeout, terminate, count
    sim = Sim(seed=4, hang={"D"})
    r2, ts2, ev2 = sim_run(E, "lqr_orig", "T60", sim, dropfrac=True)
    assert r2[rcol("flooder_wait_timeouts")] == 1 and [p.terminated for p in sim.procs if p.name == "D"] == [True]
    assert all(not p.terminated for p in sim.procs if p.name != "D")
    # refusals inside run()
    with patched(E, Sim(), e_extra(Sim())):
        fake_if(E)
        expect_exit(lambda: E.run("lqr_orig", "T60", dict(REFS), 50.0, []), "without xdp_dropfrac")
        for g in F: E.IF[g]["kind"] = "dropfrac"
        expect_exit(lambda: E.run("vfrot", "0.8_1_2_0.94", dict(REFS), 50.0, []), "without xdp_limit")
        expect_exit(lambda: E.set_cap(E.IF["A"], 100) or E.set_drop({"dev": "x", "cfg": 1, "kind": "limit"}, 5),
                    "set_drop on a limit")
    print(f"CHECK 7 OK: lqr_orig in the real run(): controller on every 2nd tick only ({len(ctl_rows)} steps), one "
          f"instance per flooder, each step == lqo_step replayed on its 0.5 s vhost window CPU (inactive D: 0), "
          f"u/CPU_eff/e held on the off ticks; drop written via set_drop to the dropfrac maps only; released to 0 after "
          f"the last tick and before the first flooder wait (u_release_s {r[rcol('u_release_s')]:.2f} s); a hanging "
          f"flooder -> 60 s timeout, terminated, flooder_wait_timeouts 1; track_err / u mean / vhost mean over own "
          f"windows; eBPF means from the dropfrac cnt maps as for every mode; no caps/budget logged; xdp_limit back")

    # swap in / out
    sim = Sim()
    with patched(E, sim, e_extra(sim)):
        fake_if(E, sim)
        E.last_cap.update({"cfgA": 5}); old = dict(E.IF["A"])
        with contextlib.redirect_stdout(io.StringIO()):
            with E.dropfrac_flooders():
                assert all(E.IF[g]["kind"] == "dropfrac" and sim.kinds[f"vnet{g}"] == "dropfrac" for g in F)
                assert E.IF["A"]["cfg"] != old["cfg"] and "cfgA" not in E.last_cap
                assert all(E.IF[v]["kind"] == "limit" and sim.kinds[f"vnet{v}"] == "limit" for v in E.VICTIMS)
                assert all(E.last_drop[E.IF[g]["cfg"]] == 0 for g in F)
            assert all(E.IF[g]["kind"] == "limit" and sim.kinds[f"vnet{g}"] == "limit" for g in F)
            try:
                with E.dropfrac_flooders():
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
            assert all(E.IF[g]["kind"] == "limit" and sim.kinds[f"vnet{g}"] == "limit" for g in F)
    # attach(): a left-over xdp_dropfrac is replaced, anything else refused
    calls = []
    saved = {k: getattr(E, k) for k in ("xdp_prog", "sudo", "load", "prog_maps", "set_cap")}
    try:
        E.set_cap = lambda d, c: None
        E.load = lambda dev, kind: calls.append(("load", dev, kind)) or {"dev": dev, "prog": 9, "cfg": 1, "cnt": 2,
                                                                         "kind": kind}
        E.prog_maps = lambda dev, kind: {"dev": dev, "prog": 7, "cfg": 1, "cnt": 2, "kind": kind}
        for name, want in (("xdp_dropfrac", "replaced"), ("xdp_limit", "reusing"), ("other", "is not xdp_limit")):
            E.xdp_prog = lambda dev: (7, "generic")
            E.sudo = lambda args, n=name: json.dumps({"name": n})
            calls.clear()
            if want == "is not xdp_limit":
                expect_exit(lambda: E.attach("vnetX", 0), want)
            else:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    d = E.attach("vnetX", 0)
                assert want in out.getvalue() and d["kind"] == "limit"
                assert calls == ([("load", "vnetX", "limit")] if name == "xdp_dropfrac" else [])
    finally:
        for k, v in saved.items():
            setattr(E, k, v)
    print("CHECK 8 OK: dropfrac_flooders(): xdp_dropfrac on the 4 flooders only (new map ids, caches dropped, drop 0), "
          "xdp_limit back afterwards, also after an exception; attach() replaces a left-over xdp_dropfrac, reuses "
          "xdp_limit, refuses anything else")


# ---------------- Part 2b: fake sudo, failed swaps, restore ----------------

class FakeHost:
    # command-level fake of the host behind the REAL exp42.sudo() (only subprocess.run is replaced, nothing is
    # executed): 'virsh domiflist', 'bpftool -j net/prog/map show', 'bpftool map update' and
    # 'ip [-force] link set dev X xdpgeneric obj O sec xdp' with the kernel's rule: a program attached and no
    # -force -> exit 2, "Error: XDP program already attached." (XDP_FLAGS_UPDATE_IF_NOEXIST). fail_ip(n, args) True
    # -> the n-th ip call (1-based) fails with exit 2 and FAKE_ERR on stderr.
    FAKE_ERR = "libbpf: fake: prog 'xdp_dropfrac': BPF program load failed\nError: fake failure of this ip call (test)."

    def __init__(self, fail_ip=None):
        self.ids, self.att, self.progs, self.maps, self.vals = 40, {}, {}, {}, {}
        self.fail_ip, self.ip_calls, self.cmds, self.devof = fail_ip, [], [], {}
        for i, g in enumerate(E.VICTIMS + E.FLOODERS):
            self.devof[g] = f"vnet{i}"
            self.load(f"vnet{i}", "xdp_limit")
        self.orig = dict(self.att)

    def nid(self):
        self.ids += 1; return self.ids

    def load(self, dev, name):
        mids = []
        for m in (("cfg", "st", "cnt") if name == "xdp_limit" else ("cfg", "seq", "cnt") if name == "xdp_dropfrac"
                  else ("cfg",)):
            i = self.nid(); self.maps[i] = m; self.vals[i] = 0; mids.append(i)
        pid = self.nid(); self.progs[pid] = (name, mids); self.att[dev] = pid
        return pid

    def name(self, g):
        return self.progs[self.att[self.devof[g]]][0]

    def do(self, a):
        if a == ["true"]:
            return 0, "", ""
        if a[:2] == ["virsh", "domiflist"]:
            g = [k for k, v in E.GUESTS.items() if v[0] == a[2]][0]
            return 0, (" Interface   Type     Source   Model    MAC\n" + "-" * 58 + "\n"
                       f" {self.devof[g]}       bridge   virbr0   virtio   52:54:00:00:00:01\n\n"), ""
        if a[:5] == ["bpftool", "-j", "net", "show", "dev"]:
            pid = self.att.get(a[5])
            xdp = [{"devname": a[5], "ifindex": 9, "mode": "generic", "id": pid}] if pid else []
            return 0, json.dumps([{"xdp": xdp, "tc": [], "flow_dissector": []}]), ""
        if a[:5] == ["bpftool", "-j", "prog", "show", "id"]:
            name, mids = self.progs[int(a[5])]
            return 0, json.dumps({"id": int(a[5]), "type": "xdp", "name": name, "map_ids": mids}), ""
        if a[:5] == ["bpftool", "-j", "map", "show", "id"]:
            return 0, json.dumps({"id": int(a[5]), "type": "array", "name": self.maps[int(a[5])]}), ""
        if a[:4] == ["bpftool", "map", "update", "id"]:
            self.vals[int(a[4])] = struct.unpack("<Q", bytes.fromhex("".join(a[-8:])))[0]
            return 0, "", ""
        if a[0] == "ip":
            force, rest = "-force" in a, [x for x in a[1:] if x != "-force"]
            assert rest[:3] == ["link", "set", "dev"] and rest[4:6] == ["xdpgeneric", "obj"] and rest[7:] == ["sec", "xdp"], a
            self.ip_calls.append(a)
            if self.fail_ip and self.fail_ip(len(self.ip_calls), a):
                return 2, "", self.FAKE_ERR + "\n"
            if rest[3] in self.att and not force:
                return 2, "", "Error: XDP program already attached.\n"
            self.load(rest[3], {E.OBJ: "xdp_limit", E.OBJ_DF: "xdp_dropfrac"}[rest[6]])
            return 0, "", ""
        raise AssertionError(f"unexpected sudo command {a}")

    def run(self, cmd, capture_output=False, text=False, check=False, **kw):
        assert cmd[:2] == ["sudo", "-n"] and capture_output and text and not kw, cmd   # never executed
        self.cmds.append(cmd[2:])
        rc, out, err = self.do(cmd[2:])
        if check and rc:
            raise subprocess.CalledProcessError(rc, cmd, out, err)
        return subprocess.CompletedProcess(cmd, rc, out, err)


class _SubprocessShim:
    # exp42's view of the subprocess module: run() goes to the FakeHost, everything else is the real module
    def __init__(self, host): self.run = host.run
    def __getattr__(self, k): return getattr(subprocess, k)


@contextlib.contextmanager
def on_host(host, extra=None):
    # REAL sudo/vnet/xdp_prog/prog_maps/load/attach/swap/set_cap/set_drop/restore_flooders against the FakeHost
    saved = {k: getattr(E, k) for k in ("subprocess",) + tuple(extra or {})}
    E.subprocess = _SubprocessShim(host)
    for k, v in (extra or {}).items():
        setattr(E, k, v)
    E.IF.clear(); E.last_cap.clear(); E.last_drop.clear()
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(E, k, v)


def attach_all(host):
    for g in E.VICTIMS + F:
        E.IF[g] = E.attach(E.vnet(E.GUESTS[g][0]), 0)
    assert all(E.IF[g]["prog"] == host.orig[host.devof[g]] for g in E.IF)   # reused, nothing loaded


def all_limit_again(host, fresh=F):
    # every flooder vnet has xdp_limit (generic) again; `fresh` got a new instance, the others still the original;
    # IF agrees with the kernel; victims never touched; caps of the fresh cfg maps written 0
    for g in F:
        dev = host.devof[g]
        assert host.name(g) == "xdp_limit", (g, host.name(g))
        assert (host.att[dev] != host.orig[dev]) == (g in fresh), (g, host.att[dev], host.orig[dev])
        assert E.IF[g]["prog"] == host.att[dev] and E.IF[g]["kind"] == "limit", (g, E.IF[g], host.att[dev])
        assert ["bpftool", "map", "update", "id", str(E.IF[g]["cfg"]), "key", "0", "0", "0", "0", "value", "hex"] \
            + ["00"] * 8 in host.cmds, g
    assert all(host.att[host.devof[v]] == host.orig[host.devof[v]] for v in E.VICTIMS)
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler   # restore_flooders put SIGINT back


def part2_restore():
    second = lambda n, a: n == 2   # the second ip call = the second swap (flooder B -> xdp_dropfrac)
    # (a) the original bug: a plain 'ip link set ... xdpgeneric obj' over an attached program -> exit 2, and since
    #     the fix the stderr is in the exception; load() now uses -force and the swap goes through
    host = FakeHost()
    with on_host(host), contextlib.redirect_stdout(io.StringIO()):
        attach_all(host)
        try:
            E.sudo(["ip", "link", "set", "dev", "vnet2", "xdpgeneric", "obj", E.OBJ_DF, "sec", "xdp"])
            raise AssertionError("no error")
        except subprocess.CalledProcessError as e:
            assert isinstance(e, E.SudoError) and e.returncode == 2
            assert "exit status 2" in str(e) and "stderr: Error: XDP program already attached." in str(e), str(e)
        host.ip_calls.clear()
        with E.dropfrac_flooders():
            assert all(host.name(g) == "xdp_dropfrac" and E.IF[g]["kind"] == "dropfrac" for g in F)
        assert len(host.ip_calls) == 8 and all(c[:2] == ["ip", "-force"] for c in host.ip_calls)
        all_limit_again(host)
    # (b) fake sudo fails on the second swap: the error (with ip's stderr) propagates, A (already swapped) gets
    #     xdp_limit back, B/C/D were never changed
    host = FakeHost(fail_ip=second)
    out = io.StringIO()
    with on_host(host), contextlib.redirect_stdout(out):
        attach_all(host)
        try:
            with E.dropfrac_flooders():
                raise AssertionError("slot body must not run")
        except subprocess.CalledProcessError as e:
            msg = str(e)
        assert "exit status 2" in msg and "fake failure of this ip call" in msg and "BPF program load failed" in msg
        all_limit_again(host, fresh={"A"})
    assert "XDP RESTORE: xdp_limit (cap 0) on all flooders" in out.getvalue()
    # (c) Ctrl+C inside the slot: all 4 back, KeyboardInterrupt propagates
    host = FakeHost()
    with on_host(host), contextlib.redirect_stdout(io.StringIO()):
        attach_all(host)
        try:
            with E.dropfrac_flooders():
                raise KeyboardInterrupt
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("KeyboardInterrupt swallowed")
        all_limit_again(host)
    # (d) IF behind the kernel (C: ip succeeded, IF still says limit), a foreign program on D and a restore whose ip
    #     fails on B: restore_flooders() never raises, fixes A and C, leaves D alone, reports B and D with the stderr;
    #     dropfrac_flooders() then raises SystemExit naming them
    host = FakeHost()
    with on_host(host), contextlib.redirect_stdout(io.StringIO()):
        attach_all(host)
        for g in ("A", "B", "C"):
            host.load(host.devof[g], "xdp_dropfrac")
        host.load(host.devof["D"], "other")
        for g in ("A", "B"):
            E.IF[g] = E.prog_maps(host.devof[g], "dropfrac")
        host.fail_ip = lambda n, a: host.devof["B"] in a
        failed = E.restore_flooders()
        assert [g for g, _ in failed] == ["B", "D"], failed
        assert "fake failure of this ip call" in failed[0][1] and "is not xdp_limit" in failed[1][1], failed
        assert host.name("A") == host.name("C") == "xdp_limit" and host.name("B") == "xdp_dropfrac"
        assert host.name("D") == "other"
        host.ip_calls.clear()
        host.fail_ip = lambda n, a: n == 1    # the swap of A fails: dropfrac_flooders' restore still hits D
        try:
            with E.dropfrac_flooders():
                pass
        except SystemExit as e:
            assert "could not be restored on D" in str(e) and isinstance(e.__context__, subprocess.CalledProcessError)
        else:
            raise AssertionError("no SystemExit")
    # (e) REAL calib42.main(), fake sudo failing on the second swap: FAIL result naming ip's stderr, all 4 restored
    def calib_on(host, phase=None):
        tmp = tempfile.mkdtemp(prefix="test_calib42_host_")
        C.TS, C.RESULT = os.path.join(tmp, COUTS[1]), os.path.join(tmp, COUTS[2])
        extra = dict(host_cpus=lambda: (6, 6), check_servers=lambda: None, check_guests=lambda: None,
                     resolve_vhost=lambda: E.VH.update({g: f"vh{g}" for g in F}))
        saved_phase = C.phase
        if phase:
            C.phase = phase
        out = io.StringIO()
        try:
            with on_host(host, extra), contextlib.redirect_stdout(out):
                return C.main(), out.getvalue(), tmp
        finally:
            C.phase = saved_phase
    host = FakeHost(fail_ip=second)
    res, out, tmp = calib_on(host)
    assert res["verdict"] == "FAIL" and res["checks"][0][0] == "xdp_swap_in_out", res
    assert "swap in:" in res["checks"][0][2] and "fake failure of this ip call" in res["checks"][0][2], res
    assert "ERROR: swap in:" in out and "XDP RESTORE" in out and json.load(open(C.RESULT))["verdict"] == "FAIL"
    all_limit_again(host, fresh={"A"})
    # (f) Ctrl+C during calib42's S phase: all 4 back, KeyboardInterrupt propagates, no result written
    def ctrl_c(*a, **k):
        raise KeyboardInterrupt
    host = FakeHost()
    try:
        calib_on(host, phase=ctrl_c)
        raise AssertionError("KeyboardInterrupt swallowed")
    except KeyboardInterrupt:
        pass
    assert not os.path.exists(C.RESULT)
    all_limit_again(host)
    # (g) REAL exp42.main(): the second swap of the first lqr_orig slot fails / Ctrl+C in an lqr_orig slot
    def main_on(host, run):
        tmp = tempfile.mkdtemp(prefix="test_exp42_host_")
        redirect_outputs(tmp)
        json.dump(CALIB_OK, open(E.CALIB, "w"))
        extra = dict(host_cpus=lambda: (6, 6), check_servers=lambda: None, check_guests=lambda: None,
                     resolve_vhost=lambda: E.VH.update({g: f"vh{g}" for g in F}), vhost_ns=lambda p: 0,
                     reference=lambda rep, procs: (dict(REFS), 50.0), run=run)
        real_sleep, time.sleep = time.sleep, lambda s: None
        try:
            with on_host(host, extra), contextlib.redirect_stdout(io.StringIO()):
                E.main()
        finally:
            time.sleep = real_sleep
    def ok_run(mode, param, refs, base_cpu, procs):
        return fake_result(), [fake_ts_row()]
    host = FakeHost(fail_ip=second)
    try:
        main_on(host, ok_run)
        raise AssertionError("no error")
    except subprocess.CalledProcessError as e:
        assert "fake failure of this ip call" in str(e)
    all_limit_again(host, fresh={"A"})
    def ctrl_c_in_lqo(mode, param, refs, base_cpu, procs):
        assert all(host.name(g) == ("xdp_dropfrac" if mode == "lqr_orig" else "xdp_limit") for g in F)
        if mode == "lqr_orig":
            raise KeyboardInterrupt
        return ok_run(mode, param, refs, base_cpu, procs)
    host = FakeHost()
    try:
        main_on(host, ctrl_c_in_lqo)
        raise AssertionError("KeyboardInterrupt swallowed")
    except KeyboardInterrupt:
        pass
    all_limit_again(host)
    print("CHECK 8b OK: fake sudo (command-level host, nothing executed): plain 'ip link set ... xdpgeneric obj' over "
          "an attached program -> exit 2 with 'XDP program already attached.' in the exception; load() uses "
          "'ip -force' (8 ip calls per slot); second swap failing -> exception carries ip's stderr, A re-attached to "
          "a new xdp_limit (cap 0), B/C/D untouched, IF == kernel state; Ctrl+C in a slot -> all 4 back; restore "
          "never raises, follows the kernel not IF, leaves a foreign program alone and reports it; REAL calib42.main "
          "(second swap fails -> FAIL result with the stderr; Ctrl+C in S -> restored, no result) and REAL "
          "exp42.main (second swap fails / Ctrl+C in an lqr_orig slot) leave xdp_limit on all 4 flooders")


# ---------------- Part 3 ----------------

def redirect_outputs(tmp):
    E.CSV, E.TS_CSV = os.path.join(tmp, OUTS[0]), os.path.join(tmp, OUTS[1])
    E.REF_FILE, E.LOG = os.path.join(tmp, OUTS[2]), os.path.join(tmp, OUTS[3])
    E.CALIB = os.path.join(tmp, "calib42_result.json")


def fake_result(**over):
    v = dict.fromkeys(E.RESULT_NAMES, 1.0)
    v.update({"jain_fairness": 0.9, "cpu_mean": 80.0, "cpu_max": 90.0, "steal_mean": 0.0, "steal_max": 0.0,
              "tick_mean_s": 0.25, "tick_max_s": 0.3, "flooder_total_gbps": 4.0, "track_err_mean": nan,
              "u_release_s": nan, "flooder_wait_timeouts": 0.0})
    v.update(over)
    return tuple(v[n] for n in E.RESULT_NAMES)


def fake_ts_row():
    return ((20.0, 0.25, 0.01, 60.0, 0.0) + (9.0,) * E.NV + (9.0,) * E.NV + (nan,) * E.NF + (1.0,) * E.NF
            + (nan, nan) + (30.0,) * E.NF + (31.0,) * E.NF + (1.0,) * E.NF + (0.5,) * E.NF + (1,))


def run_main_dry(calib=CALIB_OK, results=None, hang=()):
    tmp = tempfile.mkdtemp(prefix="test_exp42_")
    redirect_outputs(tmp)
    if calib is not None:
        with open(E.CALIB, "w") as f:
            json.dump(calib, f)
    calls, rep_now, sleeps, sudo_calls, loads, attaches = [], [0], [], [], [], []
    names = ("sudo", "check_servers", "check_guests", "vnet", "attach", "set_cap", "reference", "run", "host_cpus",
             "resolve_vhost", "vhost_ns", "load", "set_drop")
    saved = {k: getattr(E, k) for k in names}
    E.sudo = lambda args: sudo_calls.append(args) or ""
    E.host_cpus = lambda: (6, 6)
    E.check_servers = lambda: None
    E.check_guests = lambda: None
    E.resolve_vhost = lambda: E.VH.update({g: f"vh{g}" for g in F})
    E.vhost_ns = lambda p: 0
    E.vnet = lambda dom: "vnet-" + dom
    E.attach = lambda dev, cap: attaches.append(dev) or {"dev": dev, "prog": 0, "cfg": dev, "cnt": dev, "kind": "limit"}
    E.set_cap = lambda d, mbps: None
    E.set_drop = lambda d, bp: None
    E.load = lambda dev, kind: loads.append((rep_now[0], dev, kind)) or {"dev": dev, "prog": 1, "cfg": dev,
                                                                         "cnt": dev, "kind": kind}

    def fake_reference(rep, procs):
        rep_now[0] = rep
        return dict(REFS), 50.0

    def fake_run(mode, param, refs, base_cpu, procs):
        c = (mode, param)
        calls.append((rep_now[0], c, {g: E.IF[g]["kind"] for g in E.IF}))
        r = results[(rep_now[0], c)] if results and (rep_now[0], c) in results else fake_result(
            **({"track_err_mean": 2.0, "u_release_s": 40.6, "flooder_wait_timeouts": float(c in hang)}
               if mode == "lqr_orig" else {}))
        return r, [fake_ts_row()]

    E.reference, E.run = fake_reference, fake_run
    real_sleep, time.sleep = time.sleep, lambda s: sleeps.append(s)
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            E.main()
    finally:
        time.sleep = real_sleep
        for k, v in saved.items():
            setattr(E, k, v)
    return calls, out.getvalue(), tmp, sleeps, sudo_calls, loads + [("attach", dev) for dev in attaches]


def part3():
    calls, log, tmp, sleeps, _, loads = run_main_dry(hang={("lqr_orig", "T80")})
    rng = random.Random(7)
    for rep in range(1, REPS + 1):
        order = CONFIGS[:]; rng.shuffle(order)
        seq = [ALONE] + [x for c in order for x in (c, ALONE)]
        got = [c for r, c, _ in calls if r == rep]
        assert got == seq, (rep, got, seq)
    for r, c, kinds in calls:
        want = "dropfrac" if c[0] == "lqr_orig" else "limit"
        assert all(kinds[g] == want for g in F) and all(kinds[v] == "limit" for v in E.VICTIMS), (c, kinds)
    n_lqo = sum(c[0] == "lqr_orig" for _, c, _ in calls)
    swaps_in = [x for x in loads if x[0] != "attach"]; att = [x for x in loads if x[0] == "attach"]
    # swap in = load(); back = restore_flooders() -> attach() on the 4 flooders per slot, + 6 at start + 4 at the end
    assert len(swaps_in) == 4 * n_lqo and all(dev.startswith("vnet-") and k == "dropfrac" for _, dev, k in swaps_in)
    assert len(att) == 6 + 4 * n_lqo + 4, (len(att), n_lqo)
    assert len(open(E.CSV).read().splitlines()) == 1 + REPS * 13
    assert len(open(E.TS_CSV).read().splitlines()) == 1 + REPS * 13
    hdr = open(E.CSV).readline().strip().split(",")
    assert hdr == E.HEADER.split(",") and all(len(l.split(",")) == len(hdr) for l in open(E.CSV).read().splitlines())
    assert all(len(l.split(",")) == len(E.TS_HEADER.split(",")) for l in open(E.TS_CSV).read().splitlines())
    assert "DEVIATION (lqr_orig only, documented)" in log and "PPS feed-forward" in log and "SUMMARY42" in log
    assert log.count("u released at 40.60 s") == 4 * REPS
    assert sum("still running 60 s after the release" in l for l in log.splitlines() if l.startswith("WARNING")) == REPS
    assert sleeps.count(E.SESSION_REST_S) == REPS - 1 and "LQR_ORIG T60_paperK: target 30.00" in log
    assert "K = [-0.2562, -0.3031] (PAPER K" in log
    print(f"\nCHECK 9 OK: main(): 13 slots per repeat, alone at 0,2,..,12, order == random.seed(7) shuffle; every "
          f"lqr_orig slot runs with xdp_dropfrac on the 4 flooders and every other slot with xdp_limit ({len(loads)} "
          f"loads + attaches = 8 per lqr_orig slot, + 6 + 4 restore at start/end), victims never swapped; CSV/ts column counts match the headers; deviation + "
          f"PPS lines logged; release time and a 60 s wait timeout produce lines/WARNINGs; {REPS - 1} rests")

    # summary with known answers
    known = {}
    base = {"vfrot": 0.9, "lqr": 0.95}
    for rep in range(1, REPS + 1):
        known[(rep, ("vfrot", "0.8_1_2_0.94"))] = fake_result(slo_ok_worst=0.9)
        known[(rep, ("lqr", "85_4"))] = fake_result(slo_ok_worst=0.95)
        okw = {1: 0.9, 2: 0.9, 3: 0.95, 4: 0.97, 5: 0.8, 6: 0.92}[rep]
        known[(rep, ("lqr_orig", "T40"))] = fake_result(slo_ok_worst=okw, track_err_mean=1.0 + rep)
        known[(rep, ("lqr_orig", "T60"))] = fake_result(slo_ok_worst=0.5, track_err_mean=3.0)
        known[(rep, ("lqr_orig", "T80"))] = fake_result(slo_ok_worst=0.95, track_err_mean=2.0)
        known[(rep, ("lqr_orig", "T60_paperK"))] = fake_result(slo_ok_worst=0.9 + 0.01 * rep, track_err_mean=9.0)
    calls, log, tmp, *_ = run_main_dry(results=known)
    order, stats, diffs = E.summary_data(E.CSV)
    d = diffs[("vfrot", "0.8_1_2_0.94")][("lqr_orig", "T40")]["slo_ok_worst"]
    assert d[3] == (3, 2, 1), d   # 0.95/0.97/0.92 better, 0.9 x2 equal, 0.8 worse
    xs = [0.9, 0.9, 0.95, 0.97, 0.8, 0.92]
    assert abs(d[4] - scipy.stats.ttest_rel(xs, [0.9] * 6).pvalue) < 1e-9
    d = diffs[("lqr", "85_4")][("lqr_orig", "T40")]["slo_ok_worst"]
    assert d[3] == (1, 1, 4), d   # 0.97 better, 0.95 equal, 4 worse
    assert abs(d[4] - scipy.stats.ttest_rel(xs, [0.95] * 6).pvalue) < 1e-9
    d = diffs[("lqr", "85_4")][("lqr_orig", "T60")]["slo_ok_worst"]
    assert d[3] == (0, 0, 6) and d[4] == 0.0 and d[5]
    d = diffs[("vfrot", "0.8_1_2_0.94")][("lqr_orig", "T80")]["slo_ok_worst"]
    assert d[3] == (6, 0, 0)
    tr = diffs[("vfrot", "0.8_1_2_0.94")][("lqr_orig", "T40")]["track_err_mean"]
    assert tr[2] == 0 and math.isnan(tr[0]) and math.isnan(tr[4])     # reference has no tracking error
    assert abs(stats[("lqr_orig", "T40")]["track_err_mean"][0] - 4.5) < 1e-12
    s = E.summarize(E.CSV)
    assert "PAIRED vs vfrot 0.8_1_2_0.94" in s and "PAIRED vs lqr 85_4" in s and "3/2/1" in s and "1/1/4" in s
    assert "0/0/6 p=0.000(sd0)" in s
    print("CHECK 10 OK: SUMMARY42 with known values: T40 vs vfrot ok_worst W/T/L 3/2/1 and vs lqr 85_4 1/1/4, p == "
          "scipy ttest_rel; T60 vs lqr 0/0/6 p=0 (sd0); T80 vs vfrot 6/0/0; trk_err lower-is-better, nan (n 0) "
          "against references without it; both paired tables printed")

    # refusals of main()
    def main_with(nproc=6, calib=CALIB_OK, exist=None, calib_raw=None):
        tmp = tempfile.mkdtemp(prefix="test_exp42_ref_")
        redirect_outputs(tmp)
        if calib_raw is not None:
            open(E.CALIB, "w").write(calib_raw)
        elif calib is not None:
            json.dump(calib, open(E.CALIB, "w"))
        if exist:
            open(os.path.join(tmp, exist), "w").close()
        sc = []
        E.host_cpus, E.sudo = (lambda: (nproc, nproc)), (lambda a: sc.append(a) or "")
        return lambda: E.main(), sc, tmp
    sv = (E.host_cpus, E.sudo)
    try:
        for kw, words in (({"nproc": 4}, ("nproc = 4",)), ({"exist": OUTS[0]}, ("already exist",)),
                          ({"exist": OUTS[2]}, ("already exist",)), ({"calib": None}, ("not found", "calibrate42")),
                          ({"calib": dict(CALIB_OK, verdict="FAIL", checks=[["targets_span", "FAIL", "flat"]])},
                           ("verdict FAIL", "targets_span: flat")),
                          ({"calib_raw": "{"}, ("malformed",)), ({"calib": dict(CALIB_OK, tau=-1)}, ("> 0",)),
                          ({"calib": dict(CALIB_OK, verdict="maybe")}, ("verdict",))):
            fn, sc, tmp = main_with(**kw)
            before = sorted(os.listdir(tmp))
            expect_exit(fn, *words)
            assert sc == [], (kw, sc)
            assert sorted(os.listdir(tmp)) == before, (kw, before, os.listdir(tmp))
    finally:
        E.host_cpus, E.sudo = sv
    print("CHECK 11 OK: main() refuses before sudo and before writing anything with nproc 4, an existing output, no "
          "calib42_result.json, calibration verdict FAIL (lists the failed check), malformed JSON, tau <= 0, "
          "unknown verdict")

    # shell scripts in a temp copy (fake tools: sudo / nohup leave a mark and fail)
    def shell(script, files, nproc=6, ports=(5201, 5202, 5203, 5204, 5205, 5206), arg=None):
        d = tempfile.mkdtemp(prefix="test_exp42_sh_")
        for n in (script, "exp42.py", "calib42.py", "xdp_limit.o", "xdp_dropfrac.o"):
            shutil.copy(os.path.join(HERE, n), d)
        for n, txt in files.items():
            open(os.path.join(d, n), "w").write(txt)
        b = os.path.join(d, "bin"); os.mkdir(b)
        for tool, body in (("ss", "printf '%s\\n' " + " ".join(f"'LISTEN 0 5 *:{p} *:*'" for p in ports)),
                           ("nproc", f"echo {nproc}"), ("sudo", f"echo sudo >> {d}/MARK; exit 1"),
                           ("nohup", f"echo nohup >> {d}/MARK; exit 1"), ("pgrep", "exit 1"),
                           ("tee", f"echo tee >> {d}/MARK"), ("python3", f'exec {sys.executable} -B "$@"')):
            open(os.path.join(b, tool), "w").write(f"#!/bin/sh\n{body}\n"); os.chmod(os.path.join(b, tool), 0o755)
        env = dict(os.environ, PATH=b + ":" + os.environ["PATH"])
        p = subprocess.run(["sh", os.path.join(d, script)] + ([arg] if arg else []), capture_output=True, text=True,
                           env=env, cwd=d)
        mark = open(os.path.join(d, "MARK")).read().split() if os.path.exists(os.path.join(d, "MARK")) else []
        made = set(os.listdir(d)) - {script, "exp42.py", "calib42.py", "xdp_limit.o", "xdp_dropfrac.o", "bin", "MARK",
                                     "__pycache__"} - set(files)
        return p.returncode, p.stdout + p.stderr, mark, made
    good = json.dumps(CALIB_OK)
    rc, out, mark, made = shell("run42.sh", {"calib42_result.json": good}, arg="6")
    assert rc == 1 and mark == ["sudo"] and "verdict PASS" in out and "13 slots" in out and not made, (rc, out, mark)
    for files, kw, word in (({"calib42_result.json": good, "exp42.log": ""}, {}, "exp42.log already exists"),
                            ({"calib42_result.json": good, "ts_dyn42.csv": ""}, {}, "ts_dyn42.csv already exists"),
                            ({"calib42_result.json": good}, {"nproc": 4}, "nproc = 4"),
                            ({"calib42_result.json": good}, {"ports": (5201, 5202)}, "port 5203"),
                            ({}, {}, "calibrate42.sh first"),
                            ({"calib42_result.json": json.dumps(dict(CALIB_OK, verdict="FAIL"))}, {}, "verdict FAIL")):
        rc, out, mark, made = shell("run42.sh", files, **kw)
        assert rc != 0 and word in out and mark == [] and not made, (files, kw, rc, out, mark, made)
    rc, out, mark, made = shell("calibrate42.sh", {})
    assert rc == 1 and mark == ["sudo"] and "planned: calib42" in out and not made, (rc, out, mark, made)
    for files, kw, word in (({"calib42_result.json": good}, {}, "calib42_result.json already exists"),
                            ({"calib42_run.log": ""}, {}, "calib42_run.log already exists"),
                            ({"calib42_ts.csv": ""}, {}, "calib42_ts.csv already exists"),
                            ({}, {"nproc": 8}, "nproc = 8"), ({}, {"ports": (5201,)}, "port 5202")):
        rc, out, mark, made = shell("calibrate42.sh", files, **kw)
        assert rc != 0 and word in out and mark == [] and not made, (files, kw, rc, out, mark)
    print("CHECK 12 OK: run42.sh: healthy -> plan + calibration line, reaches sudo -v (nohup never without it); refuses "
          "before sudo on existing exp42.log / ts_dyn42.csv, nproc 4, missing iperf3 port, no calibration, calibration "
          "FAIL; calibrate42.sh: healthy -> plan, reaches sudo -v; refuses on each existing calib42_* file, nproc 8, "
          "missing port; neither creates any file when refusing")

    part3_calib()


# ---------------- calibration ----------------

class Plant(Sim):
    # calibration sim: vhost CPU of each flooder = first-order (tau_true) response to c0 (1 - gr u) while its
    # iperf3 runs (0 otherwise), plus white noise; counters with 1500-byte packets and the exact drop fraction;
    # broken_swap: load() never replaces the program; flat: CPU does not respond to u
    def __init__(self, tau=0.8, c0=40.0, gr=1.0, noise=0.2, broken_swap=False, hang=(), seed=2, small=None):
        super().__init__(seed=seed, hang=hang)
        self.tau, self.c0, self.gr, self.noise, self.broken, self.small = tau, c0, gr, noise, broken_swap, small
        self.y, self.ns, self.tl, self.nrng = {}, {}, {}, random.Random(seed + 1)

    def u_of(self, g):
        d = E.IF.get(g, {})
        return self.capb.get(d.get("cfg"), 0) / 1e4 if d.get("kind") == "dropfrac" else 0.0

    def active(self, g, now):
        return any(t + p["delay"] <= now < t + p["delay"] + p["secs"] for t, p in self.launches.get(g, []))

    def ystar(self, g, now):
        u = self.u_of(g)
        f = (1.0 - self.gr * u) if self.small is None else self.small(u)
        return self.c0 * f if self.active(g, now) else 0.0

    def adv(self, g):
        now = self.clock[0]; t = self.tl.get(g, now); dt = now - t; self.tl[g] = now
        y = self.y.get(g, 0.0); ys = self.ystar(g, now)
        a = math.exp(-dt / self.tau) if dt > 0 else 1.0
        integ = ys * dt + (y - ys) * self.tau * (1 - a) + self.nrng.gauss(0, self.noise) * math.sqrt(max(dt, 0) * 0.1)
        self.y[g] = ys + (y - ys) * a
        self.ns[g] = self.ns.get(g, 0) + max(0.0, integ) / 100.0 * 1e9

    def vhost_ns(self, path):
        self.adv(path[2:]); return int(self.ns[path[2:]])

    def sudo(self, args):
        if args[:3] == ["bpftool", "map", "update"]:
            for g in F: self.adv(g)
        return super().sudo(args)

    def load(self, dev, kind):
        if self.broken and kind == "dropfrac":
            self.ids += 1; g = dev[4:]
            return {"dev": dev, "prog": self.ids, "cfg": f"cfg{g}:{self.ids}", "cnt": f"cnt{g}:{self.ids}",
                    "kind": kind}
        return super().load(dev, kind)


def run_calib(plant):
    tmp = tempfile.mkdtemp(prefix="test_calib42_")
    C.TS, C.RESULT = os.path.join(tmp, COUTS[1]), os.path.join(tmp, COUTS[2])
    def attach(dev, cap):
        g = [k for k, v in E.GUESTS.items() if v[0] == dev[5:]][0]
        plant.kinds[f"vnet{g}"] = "limit"
        return {"dev": f"vnet{g}", "prog": 0, "cfg": f"cfg{g}:0", "cnt": f"cnt{g}:0", "kind": "limit"}
    extra = dict(e_extra(plant), host_cpus=lambda: (6, 6), check_servers=lambda: None, check_guests=lambda: None,
                 resolve_vhost=lambda: E.VH.update({g: f"vh{g}" for g in F}), vnet=lambda dom: "vnet-" + dom,
                 attach=attach)
    out = io.StringIO()
    E.IF.clear(); E.last_cap.clear(); E.last_drop.clear()
    with patched(E, plant, extra), contextlib.redirect_stdout(out):
        t0 = plant.clock[0]
        res = C.main()
        dur = plant.clock[0] - t0
    return res, out.getvalue(), tmp, dur


def part3_calib():
    # pure fit and grades
    t = [0.05 + 0.1 * i for i in range(200)]
    for tau_true, th in ((0.8, 0.0), (0.3, 0.2), (2.0, 0.4)):
        y = [30.0 if x < 5 + th else 30.0 - 15.0 * (1 - math.exp(-(x - 5 - th) / tau_true)) for x in t]
        y = [v + random.Random(int(x * 100)).gauss(0, 0.1) for v, x in zip(y, t)]
        f = C.fit_step(t, y, 5.0, 8.0)
        assert abs(f["tau"] - tau_true) / tau_true < 0.1 and abs(f["delta"] + 15) < 0.3 and f["r2"] > 0.99, f
        assert abs(f["theta"] - th) <= 0.1 + 1e-9
    assert C.grade_steady(0.5, 40.0) == "PASS" and C.grade_steady(2.0, 40.0) == "WARN" and C.grade_steady(4.0, 40.0) \
        == "FAIL" and C.grade_steady(0.1, 1.0) == "FAIL"
    assert C.grade_monotone([[40, 38, 36, 32, 20]] * 4) == "PASS"
    assert C.grade_monotone([[40, 38, 39.6, 32, 20]] + [[40, 38, 36, 32, 20]] * 3) == "WARN"
    assert C.grade_monotone([[40, 40, 40, 40, 39]] + [[40, 38, 36, 32, 20]] * 3) == "FAIL"
    assert C.grade_gain_ratio(1.0) == "PASS" and C.grade_gain_ratio(2.0) == "WARN" and C.grade_gain_ratio(4) == "WARN"
    assert C.grade_gain_ratio(float("inf")) == "WARN" and C.grade_gain_ratio(nan) == "FAIL"
    assert C.gain_ratio([40, 38, 36, 32, 20]) == 1.0 and abs(C.gain_ratio([40, 30, 28, 26, 20]) - 5.0) < 1e-9
    assert C.predict_totals([40, 38, 36, 32, 20], [24, 22.8, 21.6, 19.2, 12], [16, 24, 32]) == [12, 14.4, 19.2]
    assert C.grade_targets([12, 14.4, 19.2]) == "PASS" and C.grade_targets([12, 12.1, 19]) == "WARN"
    assert C.grade_targets([15, 15, 15]) == "WARN" and C.grade_targets([12, nan, 19]) == "FAIL"
    assert C.grade_targets([20, 21, 22]) == "WARN"
    assert C.grade_bpp(1500) == "PASS" and C.grade_bpp(30000) == "WARN" and C.grade_bpp(nan) == "FAIL"
    assert C.grade_dropfrac([0.0, 0.005]) == "PASS" and C.grade_dropfrac([0.02]) == "WARN" and C.grade_dropfrac(
        [0.1]) == "FAIL"
    assert C.grade_survive(0, {"A": True}) == "PASS" and C.grade_survive(0, {"A": False}) == "WARN" and \
        C.grade_survive(1, {"A": True}) == "FAIL"
    fits = [{"flooder": g, "dir": d, "G": 30.0 + (3 if d == "up" else 0), "tau": 0.8, "r2": 0.9} for g in F
            for d in ("up", "down")]
    assert C.grade_updown(C.updown(fits)) == "PASS" and C.grade_r2(fits) == "PASS"
    fits[0]["G"] = 60.0
    assert C.grade_updown(C.updown(fits)) == "FAIL"
    print("CHECK 13 OK: calib42 step fit recovers tau 0.8/0.3/2.0 s (< 10 %), the step size and the dead time; "
          "grades: steady (sd vs 0.1/0.2 x smallest target, cpu_ref >= 2), monotone, gain ratio < 1.5 else WARN, "
          "target span vs lqr 8.8-14.7, bytes/packet, drop fraction, survival, up/down agreement and R^2")

    # REAL calib42.main() on a simulated plant
    res, out, tmp, dur = run_calib(Plant())
    assert res["verdict"] == "PASS", out
    assert abs(res["tau"] - 0.8) / 0.8 < 0.15 and abs(res["G"] - 40.0) / 40.0 < 0.1 and abs(res["cpu_ref"] - 40) < 1, \
        (res["tau"], res["G"], res["cpu_ref"])
    assert abs(res["b"] - res["G"] / res["tau"]) < 1e-9 and abs(res["bytes_per_packet"] - 1500) < 2
    assert all(c[1] == "PASS" for c in res["checks"]) and len(res["checks"]) == 10
    assert sorted(os.listdir(tmp)) == ["calib42_result.json", "calib42_ts.csv"]
    on_disk = json.load(open(C.RESULT))
    assert on_disk["verdict"] == "PASS" and E.load_calib(C.RESULT)["tau"] == res["tau"]
    assert all(E.IF[g]["kind"] == "limit" for g in F)
    ts_lines = open(C.TS).read().splitlines()
    assert ts_lines[0].startswith("phase,t,vhA_cpu,drop_bpA") and len(ts_lines) > 900
    names = [c[0] for c in res["checks"]]
    assert names == ["vhost_signal_steady", "vhost_monotone_in_u", "step_fit_r2", "step_up_down_agreement",
                     "small_u_gain_ratio", "targets_span", "xdp_bytes_per_packet", "xdp_drop_fraction",
                     "xdp_swap_in_out", "ssh_iperf3_at_u_0.5"]
    assert all(f"CHECK {n}: PASS" in out for n in names) and "VERDICT: PASS" in out
    healthy_dur = dur
    # failure cases
    cases = ((Plant(broken_swap=True), "xdp_swap_in_out", "FAIL"),
             (Plant(hang={"B"}), "ssh_iperf3_at_u_0.5", "FAIL"),
             (Plant(gr=0.05), "vhost_monotone_in_u", "FAIL"),
             (Plant(noise=60.0), "vhost_signal_steady", "FAIL"))
    for plant, check, grade in cases:
        res, out, tmp, _ = run_calib(plant)
        got = {c[0]: c[1] for c in res["checks"]}
        assert got.get(check) == grade and res["verdict"] == "FAIL", (check, got, out[-2000:])
        expect_exit(lambda: E.load_calib(C.RESULT), "verdict FAIL", check)
        assert all(E.IF[g]["kind"] == "limit" for g in F) or check == "xdp_swap_in_out"
    # WARN-only checks: a steep small-u gain (ratio ~4.2) -> WARN with its meaning, verdict WARN, exp42 accepts
    res, out, tmp, _ = run_calib(Plant(small=lambda u: 1.0 - 4.0 * u if u <= 0.1 else 0.6 - 0.2 * (u - 0.1)))
    got = {c[0]: c[1] for c in res["checks"]}
    assert got["small_u_gain_ratio"] == "WARN" and res["verdict"] == "WARN", (got, out[-1500:])
    assert all(v == "PASS" for k, v in got.items() if k != "small_u_gain_ratio"), got
    assert "small_u_gain_ratio: WARN" in out and "WARN MEANING: near u = 0 the plant is" in out
    assert E.load_calib(C.RESULT)["verdict"] == "WARN"
    # flat targets in the analysis -> WARN with its meaning (and the PASS case carries no meaning text)
    a = {"sd_max": 0.5, "cpu_ref": 40.0, "levels_cpu": [[40, 38, 36, 32, 20]] * 4, "updown": {g: (0.0, 1.0) for g in F},
         "fits": [{"flooder": g, "dir": d, "G": 40.0, "tau": 0.8, "r2": 0.99} for g in F for d in ("up", "down")],
         "gain_ratio": 1.0, "targets": [16.0, 24.0, 32.0], "pred_totals": [15.0, 15.1, 15.2], "bpp": 1500.0,
         "drop_err": [0.0]}
    M = {"timeouts": 0, "json_ok": {g: True for g in F}, "wait_s": 0.5}
    ch = {c[0]: c for c in C.checks(a, True, True, M)}
    assert ch["targets_span"][1] == "WARN" and "same operating point" in ch["targets_span"][2]
    assert C.worst([c[1] for c in ch.values()]) == "WARN" and "WARN MEANING" not in ch["small_u_gain_ratio"][2]
    a["pred_totals"] = [20.0, 21.0, 22.0]
    ch = {c[0]: c for c in C.checks(a, True, True, M)}
    assert ch["targets_span"][1] == "WARN" and "different throughput range" in ch["targets_span"][2]
    print(f"CHECK 14 OK: the REAL calib42.main() on a simulated plant (tau 0.8 s, gain 40, noise): all 10 checks PASS, "
          f"identifies tau {0.8:g} / G 40 / cpu_ref 40 within 15 %, writes only calib42_result.json + calib42_ts.csv "
          f"(+ calib42_run.log via tee), xdp_limit back on all flooders, exp42.load_calib accepts it; simulated "
          f"duration {healthy_dur:.0f} s (+ checks/attach on the real host); broken swap, a flooder hanging at u = 0.5, "
          f"no control authority and a noisy signal each give FAIL and exp42 refuses; a steep small-u gain (ratio "
          f"> 3) and flat / off-range target totals give WARN with their meaning printed, and exp42 accepts WARN")


def main():
    global SHA_BEFORE
    SHA_BEFORE = sha(OLD_FILES)
    saved = (E.CSV, E.TS_CSV, E.REF_FILE, E.LOG, E.CALIB, C.TS, C.RESULT)
    try:
        part0(); part1(); part2(); part2_restore(); part3()
    finally:
        E.CSV, E.TS_CSV, E.REF_FILE, E.LOG, E.CALIB, C.TS, C.RESULT = saved
    assert sha(OLD_FILES) == SHA_BEFORE
    assert all(v is not None for v in SHA_BEFORE.values()), [k for k, v in SHA_BEFORE.items() if v is None]
    present = [n for n in OUTS + COUTS if os.path.exists(os.path.join(HERE, n))]
    assert not present, present
    print("CHECK 15 OK: sha256 of " + ", ".join(OLD_FILES) + " unchanged; no exp42 or calib42 output in the testbed")
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
