#!/usr/bin/env python3
# Pure-python self-test for exp45.py and run45.sh: no VM, no sudo, no network, no traffic, no sleeping (simulated
# clock), nothing attached; run45.sh is executed in a temp dir against fake nproc/ss/pgrep/sudo on PATH.
# Part 0: plan: exp44's scenarios unchanged, configs (vfrot, lqr_orig T40/T60, hyb T60 -> 15 slots), time budget,
#         rotation, headers extend exp44's; every exp44 function except the 8 that had to change is source-identical,
#         every exp44 constant except file names / plan / layout identical.
# Part 1: K SIGN (unchanged from exp44) + hyb param refusals.
# Part 2: EQUIVALENCE with exp44 for the unchanged controllers: exp45.run() == exp44.run() in every exp44 result column
#         and tick by tick, in S1, S2 and S3, for vfrot 0.8_1_2_0.94, lqr_orig T40_paperK / T60_paperK and lqr 85_4.
# Part 3: hyb RULES on hyb_step() directly: (a) raise, (b) cut, (c) hold / restore / guard, clamps, attribution
#         window, donors, relabelling symmetry (no tenant identity used), and NO SIGN FLIPS (randomised: raises only up,
#         cuts only down, targets in [floor, ceil], best-effort never above base, a hurt SLO tenant never cut; lqr_orig's
#         u never rises when its target is raised).
# Part 4: hyb on the REAL run(), testing what hyb does and no more: (i) reduces to lqr_orig T60 tick by tick when no
#         victim is hurt (S1, S2, S3 with victims below target); (ii) simulated S3 throttle: each throttled victim's
#         target rises step by step (x 1 / BETA per raise): A (36/40 %) until our drop stops -> exempt flag (ts
#         ex<g>, exempt<g>_s) on, below the ceiling; B (90/100 %) all the way to the ceiling (ceil<g>_s) -- whether
#         it then gets out of the flip-flop is recorded only; the flag equals its definition on every step; flooder
#         targets never rise; (iii) simulated flooder-hurts-victim (S1/S2): the flooder
#         target is cut step by step (x BETA per cut) down to the floor and logged (ts hyb_ev bit 2, hyb_cuts,
#         hyb_min_be_tgt). NO assertion on the drop fraction or the SLO in (ii)/(iii): lqr_orig flip-flops (0.95 / 0),
#         so rule (b) is expected to be nearly a no-op and (a) only helps once the target is above ~217 %.
#         ubelow<g> (u > 0 while CPU < target) is recorded and range-checked; xdp_limit back on all six; field counts.
# Part 5: SUMMARY45 on a synthetic CSV (paired vs vfrot per scenario includes hyb).
# Part 6: refusals of exp45.main(), FAIL FAST (rep-1 S3 reference), dry main() (swaps around lqr_orig AND hyb with the
#         scenario's tenants), run45.sh refusals against fake commands.
# Part 7: exp44/exp43/calib42/XDP files unchanged (sha256 snapshot taken when exp45 was built); no exp45 output.
import sys
sys.dont_write_bytecode = True
import contextlib, csv as csvmod, hashlib, inspect, io, json, math, os, random, shutil, signal, struct, subprocess
import tempfile, time
sys.argv = sys.argv[:1]
import numpy as np
import exp44, exp45
import scipy.stats   # test-only cross-check

E, E44 = exp45, exp44
HERE = os.path.dirname(os.path.abspath(E.__file__))
F, V, TEN = E.FLOODERS, E.VICTIMS, E.TENANTS
nan = float("nan")
OUTS = ("results_dyn45.csv", "ts_dyn45.csv", "ref_victim45.txt", "exp45.log")
FLOOD_GBIT = 6.0
CPU_PER_GBIT = 10.0      # simulated vhost CPU, % of one core per passed Gbit/s
REFS = {"V1": 9.0, "V2": 9.0}
REFS3 = {"V1": 9.0, "V2": E.V2_RATE_S3}
RREF = {"p50": 0.10, "p99": 0.25, "loss": 0.0, "n": 200}
CALIB_REAL = json.load(open(os.path.join(HERE, "calib42_result.json")))   # read only
CALIB_SIM = {"verdict": "WARN", "checks": [["x", "WARN", "sim"]], "cpu_ref": 50.0, "tau": 0.08, "b": 1000.0,
             "written": "test"}   # T40 20 %, T60 30 %; hyb ceiling 300 %, floor 10 %, guard 20, add 3.125 %/s
CHANGED = {"run", "csv_line", "ts_line", "summarize", "setup_configs", "check_static", "main", "swap_vnets_per_repeat"}
# sha256 snapshot taken when exp45 was built (2026-11-07), before any test run
SNAPSHOT = """
3252ad813fdd7d55f27ebfa3b7e28267a1c80e3a4769a3debec069310c2821cd exp44.py
c9532a7815ffc138d70246f9e4053abe0bbf42c22a5ee0e82317514187503de1 run44.sh
903a33c2532ac6106232a7482d9e2273edfdad305a60d95bde73e606d38f3b2e test_exp44.py
fcd783b5911b26a363bd0d4b6a9b4e1ad6a823cf55ef737ed5eb4b6642ac7912 results_dyn44.csv
a74fd458079a70320f88533ce81d4b271b1a56e60b9b35943f9759bdaad3deec ts_dyn44.csv
c7f8179f3e8e316fa037da46b8f35bb68f765e627374f1863e9abd2b08d5ba30 ref_victim44.txt
9ef901d00a66d7bf0f26b042b60b5641b3f90bdf40d208f0273f61931b18c720 exp44.log
4af307aefae532b31a7b80613a47af727e288d426013e7069ce8c5c7813ab8fd exp43.py
19c5ac2cb096a1c9ee9da3e3ade02b77a94390920b082843ed9f9d75d85563e9 calib42_result.json
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


@contextlib.contextmanager
def scenario(name):
    saved = (E.SCENARIO, E44.SCENARIO)
    try:
        E.set_scenario(name); E44.set_scenario(name)
        yield
    finally:
        E.SCENARIO, E44.SCENARIO = saved


# ---------------- simulated testbed (exp44's, plus optional victim contention) ----------------

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
    # exp44's simulated testbed. victim v offers v_eff x its -b rate inside its window and passes offer x (1 - drop_bp
    # / 1e4) under xdp_dropfrac; a flooder offers FLOOD_GBIT inside its window and passes min(offer, cap) under
    # xdp_limit or offer x (1 - drop_bp / 1e4) under xdp_dropfrac; vhost CPU = CPU_PER_GBIT x passed Gbit/s (x vh_victim
    # for the victims). exp45: contention c > 0 scales every victim's offered rate by (1 - c x flooders' last passed
    # rate / FLOOD_GBIT) -- the flooders hurt the victims without any drop on the victims
    def __init__(self, seed=1, read_cost=0.002, lost=(), rtt=None, v_eff=1.0, vh_victim=1.0, contention=0.0):
        self.rng, self.clock, self.read_cost = random.Random(seed), [1000.0], read_cost
        self.launches, self.capb, self.cum, self.kinds, self.ids = {}, {}, {}, {}, 100
        self.cpu_acc, self.lost = [0, 0, 0], set(lost)
        self.rtt = rtt or (lambda s: 0.1 + 0.01 * (s % 10))
        self.pings, self.ssh_log, self.writes = [], [], []
        self.v_eff, self.vh_victim, self.contention = v_eff, vh_victim, contention
        self.frate, self.vstate = {}, {}

    def sudo(self, args):
        if args == ["true"]:
            return ""
        assert args[:3] == ["bpftool", "map", "update"], args
        val = struct.unpack("<Q", bytes.fromhex("".join(args[-8:])))[0]
        self.capb[args[4]] = val
        self.writes.append((self.clock[0], args[4], val))
        return ""

    def counters(self, d):
        self.clock[0] += self.read_cost
        now, g = self.clock[0], str(d["cnt"]).split(":")[0][3:]
        L = self.launches.get(g, [])
        v = self.capb.get(d["cfg"], 0)
        if g in E.VICTIMS:
            c = self.cum.setdefault(d["cnt"], [0.0, 0.0, None, 0.0, 0.0])
            offc = self.v_off(g, now)
            d_off = offc - (offc if c[2] is None else c[2]); c[2] = offc
            if d.get("kind") == "dropfrac" and v:
                c[1] += d_off * v / 10000.0
            c[0] = offc - c[1]
        else:
            c = self.cum.setdefault(d["cnt"], [0.0, 0.0, now, 0.0, 0.0])
            dt, c[2] = now - c[2], now
            off = sum(FLOOD_GBIT * 1e9 / 8 * dt for t, p in L if t + p["delay"] <= now < t + p["delay"] + p["secs"])
            passed = off * (1.0 - v / 10000.0) if d.get("kind") == "dropfrac" else (off if v == 0 else min(off, v * dt))
            c[0] += passed; c[1] += off - passed
            c[3] += passed / 1500.0; c[4] += (off - passed) / 1500.0
            if dt > 0:
                self.frate[g] = passed * 8 / 1e9 / dt
        return {0: int(c[3]), 1: int(c[4]), 2: int(c[0]), 3: int(c[1])}

    def v_off_raw(self, g, now):
        return sum(p["rate"] * self.v_eff * 1e9 / 8 * max(0.0, min(now - t - p["delay"], p["secs"]))
                   for t, p in self.launches.get(g, []))

    def v_off(self, g, now):
        # victim g's cumulative offered bytes at `now` (incremental under contention)
        if not self.contention:
            return self.v_off_raw(g, now)
        t_l, cum = self.vstate.get(g, (None, 0.0))
        if t_l is None:
            self.vstate[g] = (now, self.v_off_raw(g, now)); return self.vstate[g][1]
        if now > t_l:
            f = max(0.0, 1.0 - self.contention * sum(self.frate.values()) / FLOOD_GBIT)
            cum += (self.v_off_raw(g, now) - self.v_off_raw(g, t_l)) * f
            self.vstate[g] = (now, cum)
        return cum

    def vhost_ns(self, path):
        g = path[2:]
        mine = [x for k, x in self.cum.items() if str(k).split(":")[0] == f"cnt{g}"]
        if g in E.VICTIMS:
            passed = self.v_off(g, self.clock[0]) - sum(x[1] for x in mine)
        else:
            passed = sum(x[0] for x in mine)
        scale = self.vh_victim if g in E.VICTIMS else 1.0
        return int(passed * 8 / 1e9 * CPU_PER_GBIT * scale / 100.0 * 1e9)

    def cpu(self):
        self.cpu_acc[0] += 100; self.cpu_acc[1] += 40
        return tuple(self.cpu_acc)

    def ssh(self, guest, cmd, out):
        now = self.clock[0]
        self.ssh_log.append((guest, cmd, now))
        p = parse_cmd(cmd)
        if p["ping"]:
            n = p["n"]
            self.pings.append((now, n, set(self.lost)))
            out.write(ping_text(n, self.lost, self.rtt)); out.flush()
            return FakeProc(self, guest, now + n * p["i"] + 0.1)
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
        mod.VH[g] = f"vh{g}"


def sim_run(mod, mode, param, sim, refs=None, kinds_during=None):
    refs = dict(refs or (REFS3 if mod.SCENARIO == "S3" else REFS))
    with patched(mod, sim):
        fake_if(mod, sim)
        with contextlib.redirect_stdout(io.StringIO()):
            args = (mode, param, refs, 50.0, [], dict(RREF))
            if mode in ("lqr_orig", "hyb"):
                with mod.dropfrac_tenants(mod.lqo_tenants()):
                    if kinds_during is not None:
                        kinds_during.update({g: mod.IF[g]["kind"] for g in mod.IF})
                    r, ts = mod.run(*args)
            else:
                r, ts = mod.run(*args)
    return r, ts


def setup(mod, configs, calib):
    mod.LQR.clear(); mod.LQO.clear(); mod.VFROT.clear()
    mod.setup_configs(configs, calib)


ALL_CFG = [("vfrot", "0.8_1_2_0.94"), ("lqr_orig", "T40_paperK"), ("lqr_orig", "T60_paperK"), ("lqr", "85_4")]


# ---------------- Part 0 ----------------

def part0():
    assert E.SCENARIOS == E44.SCENARIOS and E.SCEN_ORDER == E44.SCEN_ORDER and E.V2_RATE_S3 == E44.V2_RATE_S3 == 10.0
    assert E.CONFIGS == [("vfrot", "0.8_1_2_0.94"), ("lqr_orig", "T40_paperK"), ("lqr_orig", "T60_paperK"),
                         ("hyb", "T60_paperK")]
    assert all(E.SCEN_CONFIGS[s] == E.CONFIGS for s in E.SCENARIOS) and E.PAIRED_REF == E44.PAIRED_REF
    assert E.LQO_MODES == ("lqr_orig", "hyb") and "hyb" in E.MODES
    E.check_static()
    f44 = {n for n, o in vars(E44).items() if inspect.isfunction(o) and o.__module__ == "exp44"}
    f45 = {n for n, o in vars(E).items() if inspect.isfunction(o) and o.__module__ == "exp45"}
    assert f44 <= f45 and f45 - f44 == {"hyb_new", "hyb_classify", "hyb_step", "parse_hyb", "hyb_report"}, f45 - f44
    changed = {n for n in f44 if inspect.getsource(getattr(E, n)) != inspect.getsource(getattr(E44, n))}
    assert changed == CHANGED, changed
    for c in ("SudoError", "_SafeOut"):
        assert inspect.getsource(getattr(E, c)) == inspect.getsource(getattr(E44, c))
    c44 = {n: v for n, v in vars(E44).items() if n.isupper()}
    diff = {n for n in c44 if repr(c44[n]) != repr(getattr(E, n))}
    assert diff == {"CONFIGS", "SCEN_CONFIGS", "MODES", "CSV", "TS_CSV", "REF_FILE", "LOG", "RESULT_NAMES", "HEADER",
                    "TS_HEADER", "TS_RTT"}, diff
    # the hyb constants (reported values)
    assert (E.HYB_THRESH, E.HYB_BETA, E.HYB_T, E.HYB_CUT_HOLD) == (0.94, 0.8, 2.0, E.VFROT_CUT_HOLD)
    assert E.HYB_THRESH == E.parse_vfrot("vfrot", "0.8_1_2_0.94")[3] and E.HYB_BETA == E.parse_vfrot("vfrot", "0.8_1_2_0.94")[0]
    assert (E.HYB_ADD_FRAC, E.HYB_CEIL_FRAC, E.HYB_FLOOR_FRAC, E.HYB_GUARD_FRAC) == (1 / 16, 6.0, 0.2, 0.4)
    assert E.HYB_CEIL_FRAC * CALIB_REAL["cpu_ref"] > 217.0   # above the target needed to stop the flip-flop
    # headers extend exp44's
    assert E.RESULT_NAMES[:E.R_X45] == E44.RESULT_NAMES and E.RESULT_NAMES[E.R_X45:] == E.X45_NAMES
    assert E.HEADER == E44.HEADER + "," + ",".join(E.X45_NAMES)
    h44, h45 = E44.TS_HEADER.split(","), E.TS_HEADER.split(",")
    assert h45[:5 + E44.TS_RTT] == h44[:5 + E44.TS_RTT] and h45[-2:] == h44[-2:]
    assert h45[5 + E.TS_TGT:5 + E.TS_RTT] == ([f"tgt{g}" for g in TEN] + ["hyb_ev"] + [f"ex{g}" for g in TEN]
                                              + [f"ubelow{g}" for g in TEN])
    assert E.X45_NAMES[10:] == ([f"exempt{g}_s" for g in TEN] + [f"ceil{g}_s" for g in TEN]
                                + [f"ubelow{g}_frac" for g in TEN])
    assert E.TS_TGT == E44.TS_RTT and len(h45) == 5 + E.TS_RTT + 2
    # plan
    n, per, tot = E.expected_duration(6)
    _, perm, totm = E.measured_duration(6)
    assert n == 15 and E.swap_vnets_per_repeat() == 3 * 1 + 3 * 2 + 3 * 6
    assert perm <= E.BUDGET_MIN_PER_REP * 60 and 2.2 * 3600 <= totm <= 2.5 * 3600, (perm / 60, totm / 3600)
    assert [E.scen_order(r) for r in (1, 2, 3)] == [["S3", "S1", "S2"], ["S1", "S2", "S3"], ["S2", "S3", "S1"]]
    print(f"CHECK 0 OK: exp44's scenarios / rotation unchanged; configs vfrot, lqr_orig T40/T60, hyb T60 -> {n} slots, "
          f"~{perm / 60:.1f} min/repeat (budget {E.BUDGET_MIN_PER_REP:g}), R=6 ~{totm / 3600:.2f} h; "
          f"{len(f44) - len(CHANGED)} of {len(f44)} exp44 functions source-identical (changed only: "
          f"{', '.join(sorted(CHANGED))}); constants identical except file names / plan / layout; headers extend "
          f"exp44's (+{len(E.X45_NAMES)} result, +{3 * len(TEN) + 1} ts columns: target, hyb event, exempt flag, "
          f"u > 0 while CPU < target per tenant)")


# ---------------- Part 1: K sign (unchanged) + hyb params ----------------

def part1():
    cref = float(CALIB_REAL["cpu_ref"])
    for target in (10.0, 16.44, 24.66):
        k = E.lqo_gain(E.LQO_TAU_PAPER, E.LQO_B_PAPER, target)
        assert abs(k[0] - E.K_PAPER[0]) < 5e-4 and abs(k[1] - E.K_PAPER[1]) < 5e-4, k
    K60 = E.calib_gain(CALIB_REAL)
    assert K60 == E44.calib_gain(CALIB_REAL) and K60[0] < 0 and abs(K60[0] + 0.0133) < 5e-4
    assert E.lqo_gain(float(CALIB_REAL["tau"]), float(CALIB_REAL["b"]), 0.6 * cref)[0] > 0   # Euler bug, refused
    for bad in ((0.0092, 0.0087), (0.0, -0.1), (nan, -0.1)):
        expect_exit(lambda: E.check_gain_sign(bad, "t"), "REFUSING TO START", "K[0] >= 0")
    expect_exit(lambda: E.setup_configs([("lqr_orig", "T60")], CALIB_SIM), "malformed")
    expect_exit(lambda: E.setup_configs([("hyb", "T60_zoh")], CALIB_SIM), "only T<pct>_paperK")
    expect_exit(lambda: E.setup_configs([("hyb", "T15_paperK")], CALIB_SIM), "between the floor")
    expect_exit(lambda: E.setup_configs([("hyb", "60")], CALIB_SIM), "malformed")
    setup(E, E.all_configs(), CALIB_REAL)
    assert E.LQO["T60_paperK"][1] == E.K_PAPER and abs(E.LQO["T60_paperK"][0] - 0.6 * cref) < 1e-9
    saved = E.K_PAPER
    try:
        E.K_PAPER = (0.2562, 0.3031)
        expect_exit(lambda: setup(E, [("hyb", "T60_paperK")], CALIB_REAL), "K[0] >= 0")
    finally:
        E.K_PAPER = saved
    print(f"CHECK 1 OK (K SIGN, unchanged): paper tau/b -> K {E.K_PAPER}; ZOH K on calib42 [{K60[0]:.4f}, "
          f"{K60[1]:.4f}]; K[0] >= 0 / nan refused (also for hyb); hyb accepts only T<pct>_paperK between floor and "
          f"ceiling (T60 -> {0.6 * cref:.2f} %)")


# ---------------- Part 2: equivalence with exp44 ----------------

def part2():
    setup(E44, ALL_CFG, CALIB_SIM)
    setup(E, ALL_CFG, CALIB_SIM)
    n_ticks, n_runs = 0, 0
    for s in ("S1", "S2", "S3"):
        with scenario(s):
            for m, p in ALL_CFG:
                for kw in ({}, {"vh_victim": 0.35}):
                    kd44, kd45 = {}, {}
                    r44, ts44 = sim_run(E44, m, p, Sim(seed=11, **kw), kinds_during=kd44)
                    r45, ts45 = sim_run(E, m, p, Sim(seed=11, **kw), kinds_during=kd45)
                    assert kd44 == kd45
                    assert len(r45) == len(E.RESULT_NAMES) and len(r44) == len(E44.RESULT_NAMES)
                    bad = [(n, x, y) for n, x, y in zip(E44.RESULT_NAMES, r44, r45) if not same(x, y)]
                    assert not bad, (s, m, p, bad)
                    assert all(math.isnan(x) for x in r45[E.R_X45:E.R_X45 + 22]), (s, m, p)
                    ubf = {g: r45[rcol(f"ubelow{g}_frac")] for g in TEN}
                    ctl_set = set(E.lqo_tenants()) if m == "lqr_orig" else set()
                    assert all(math.isnan(ubf[g]) != (g in ctl_set) for g in TEN), (s, m, p, ubf)
                    assert all(0.0 <= ubf[g] <= 1.0 for g in ctl_set)
                    assert len(ts44) == len(ts45) > 100
                    for a, b_ in zip(ts44, ts45):
                        assert len(b_) == E.TS_RTT + 2
                        assert all(same(x, y) for x, y in zip(a[:E44.TS_RTT], b_[:E.TS_TGT])), (s, m, p)
                        assert all(same(x, y) for x, y in zip(a[E44.TS_RTT:], b_[E.TS_RTT:])), (s, m, p)
                        assert all(math.isnan(x) for x in b_[E.TS_TGT:E.TS_HEV]) and b_[E.TS_HEV] == 0
                        assert all(math.isnan(x) for x in b_[E.TS_EX:E.TS_UB])
                        for i, g in enumerate(TEN):
                            x = b_[E.TS_UB + i]
                            if m == "lqr_orig" and g in ctl_set and b_[E.TS_CTL] == 1 and b_[0] < E.OFFSET + E.T:
                                assert x in (0.0, 1.0), (s, m, g, x)
                            elif m != "lqr_orig" or g not in ctl_set or b_[E.TS_CTL] == 0:
                                assert math.isnan(x), (s, m, g, x)
                    n_ticks += len(ts45); n_runs += 1
    print(f"CHECK 2 OK (EQUIVALENCE): exp45.run() == exp44.run() in every exp44 result column and tick by tick "
          f"({n_runs} runs, {n_ticks} ticks) in S1, S2, S3 (victim vhost x1 and x0.35) for vfrot 0.8_1_2_0.94, lqr_orig "
          f"T40_paperK / T60_paperK and lqr 85_4; the hyb columns are nan / 0 for them, ubelow is 0/1 on lqr_orig's "
          f"controller steps of its tenants and nan elsewhere")


# ---------------- Part 3: hyb rules on hyb_step() ----------------

def st_new(tenants=("V1", "V2", "A", "B"), base=30.0, cref=50.0):
    return E.hyb_new(list(tenants), base, cref)


def part3():
    INF = math.inf
    # (a) a tenant with an SLO, hurt, with our drop on it within HYB_T: raised x 1.25 per step up to the ceiling
    s = st_new()
    s["u_pos"]["V2"] = 9.6
    ev = E.hyb_step(s, 10.0, {"V1": 1.0, "V2": 0.5, "A": INF, "B": INF}, {})
    assert ev == 1 and abs(s["tgt"]["V2"] - 37.5) < 1e-9 and s["tgt"]["V1"] == s["tgt"]["A"] == 30.0
    assert s["raises"] == 1 and s["first_raise"] == 10.0 and s["cuts"] == 0
    seq = []
    for k in range(1, 15):
        s["u_pos"]["V2"] = 10.0 + 0.5 * k - 0.1
        E.hyb_step(s, 10.0 + 0.5 * k, {"V1": 1.0, "V2": 0.5, "A": INF, "B": INF}, {})
        seq.append(round(s["tgt"]["V2"], 4))
    want = [round(min(300.0, 37.5 * 1.25 ** k), 4) for k in range(1, 15)]   # ceiling 6 x 50
    assert seq == want and seq[9:] == [300.0] * 5, seq
    assert s["raises"] == 11  # 30 x 1.25^10 = 279.4, then 300 (clamped), then no event at the ceiling
    # attribution window: last drop of ours 2.0 s ago -> still (a); 2.01 s -> (b)
    s = st_new(); s["u_pos"]["V1"] = 8.0
    E.hyb_step(s, 10.0, {"V1": 0.5, "V2": 1.0, "A": INF, "B": INF}, {})
    assert s["tgt"]["V1"] == 37.5 and s["cuts"] == 0
    s = st_new(); s["u_pos"]["V1"] = 7.99
    ev = E.hyb_step(s, 10.0, {"V1": 0.5, "V2": 1.0, "A": INF, "B": INF}, {})
    assert ev == 2 and s["tgt"]["V1"] == 30.0 and s["tgt"]["A"] == s["tgt"]["B"] == 24.0 and s["cuts"] == 1
    # (b) a hurt SLO tenant we never dropped (also one we do not control: S1/S2 victims): donors x 0.8, floor,
    #     at most once per HYB_CUT_HOLD; SLO tenants inside the band are NOT donors
    s = st_new(("A", "B"))
    tg = []
    for k in range(12):
        E.hyb_step(s, 10.0 + 0.5 * k, {"V1": 0.8, "V2": 1.0, "A": INF, "B": INF}, {})
        tg.append(round(s["tgt"]["A"], 4))
    assert tg == [24.0, 24.0, 19.2, 19.2, 15.36, 15.36, 12.288, 12.288, 10.0, 10.0, 10.0, 10.0], tg   # floor 0.2 x 50
    assert s["cuts"] == 5 and s["tgt"]["A"] == s["tgt"]["B"]   # 30 -> 24 -> 19.2 -> 15.36 -> 12.29 -> 10 (floor)
    s = st_new()
    E.hyb_step(s, 10.0, {"V1": 0.8, "V2": 1.05, "A": INF, "B": INF}, {})
    assert s["tgt"]["V2"] == 30.0 and s["tgt"]["A"] == 24.0          # V2 within [0.94, 1/0.94]: not a donor
    s = st_new()
    E.hyb_step(s, 10.0, {"V1": 0.8, "V2": 1.07, "A": INF, "B": INF}, {})
    assert s["tgt"]["V2"] == 24.0                                    # an SLO tenant above 1/0.94 IS a donor
    s = st_new(); s["tgt"]["V2"] = 50.0; s["last_act"]["V2"] = 9.5
    E.hyb_step(s, 10.0, {"V1": 0.8, "V2": 1.2, "A": INF, "B": INF}, {})
    assert s["tgt"]["V2"] == 50.0 and s["tgt"]["A"] == 24.0          # ... unless it holds a raise
    s = st_new(("A",))
    assert E.hyb_step(s, 10.0, {"V1": 0.5}, {}) == 2 and s["tgt"]["A"] == 24.0   # absent ratio = no SLO = donor
    # (c) hold, then restore: donors up only HYB_T after the last cut AND the last hurt-by-others; raised targets down
    #     toward max(base, min(ceil, CPU_eff + guard)), never below
    s = st_new(("A", "V1"))
    E.hyb_step(s, 10.0, {"V1": 0.8, "A": INF}, {})                  # cut A to 24 (V1 is ours to control, not hurt by us)
    assert s["tgt"]["A"] == 24.0 and s["tgt"]["V1"] == 30.0
    ok = {"V1": 1.0, "A": INF}
    for t_ in (10.5, 11.0, 11.5):
        assert E.hyb_step(s, t_, ok, {}) == 0 and s["tgt"]["A"] == 24.0   # hold for HYB_T
    assert E.hyb_step(s, 12.0, ok, {}) == 4 and abs(s["tgt"]["A"] - (24.0 + 50 / 16 * 0.5)) < 1e-9
    for k in range(10):
        E.hyb_step(s, 12.5 + 0.5 * k, ok, {})
    assert s["tgt"]["A"] == 30.0                                     # back to the base, never above
    s = st_new(("V1",)); s["tgt"]["V1"] = 75.0; s["last_act"]["V1"] = 10.0
    assert E.hyb_step(s, 11.0, {"V1": 1.0}, {"V1": 40.0}) == 0      # hold
    assert E.hyb_step(s, 12.0, {"V1": 1.0}, {"V1": 40.0}) == 8 and abs(s["tgt"]["V1"] - (75 - 1.5625)) < 1e-9
    for k in range(40):
        E.hyb_step(s, 12.5 + 0.5 * k, {"V1": 1.0}, {"V1": 40.0})
    assert abs(s["tgt"]["V1"] - 60.0) < 1e-9                         # guard: CPU_eff 40 + 0.4 x 50
    for k in range(40):
        E.hyb_step(s, 40.0 + 0.5 * k, {"V1": 1.0}, {"V1": 2.0})
    assert s["tgt"]["V1"] == 30.0                                    # idle tenant: back to the base
    s = st_new(("V1",)); s["tgt"]["V1"] = 60.0
    E.hyb_step(s, 20.0, {"V1": 0.9}, {"V1": 5.0})
    assert s["tgt"]["V1"] == 60.0                                    # hurt (not by us) -> hold, not restored
    # no SLO tenant hurt, all targets at the base -> nothing ever changes (hyb == lqr_orig)
    s = st_new()
    assert all(E.hyb_step(s, 1.0 + 0.5 * k, {"V1": 0.95 + 0.0011 * k, "V2": 1.0, "A": INF, "B": INF},
                          {g: 35.0 for g in TEN}) == 0 for k in range(80))
    assert all(x == 30.0 for x in s["tgt"].values()) and s["raises"] == s["cuts"] == 0
    # relabelling symmetry: the loop uses no tenant identity, only the keys it is given
    rng = random.Random(5)
    names = ["V1", "V2", "A", "B", "C", "D"]
    for trial in range(30):
        perm = names[:]; rng.shuffle(perm); mp = dict(zip(names, perm))
        s1, s2 = st_new(names), st_new(perm)
        for k in range(60):
            t_ = 1.0 + 0.5 * k
            rat = {g: (rng.choice([0.5, 0.9, 0.97, 1.0, 1.1]) if g in ("V1", "V2", "A") else INF) for g in names}
            eff = {g: rng.uniform(0, 70) for g in names}
            for g in names:
                if rng.random() < 0.3:
                    s1["u_pos"][g] = t_ - rng.uniform(0, 3); s2["u_pos"][mp[g]] = s1["u_pos"][g]
            e1 = E.hyb_step(s1, t_, rat, eff)
            e2 = E.hyb_step(s2, t_, {mp[g]: x for g, x in rat.items()}, {mp[g]: x for g, x in eff.items()})
            assert e1 == e2 and all(abs(s1["tgt"][g] - s2["tgt"][mp[g]]) < 1e-12 for g in names)
    # NO SIGN FLIPS (randomised): raise events only increase hurt-by-us SLO targets, cuts only decrease donors,
    # targets stay in [floor, ceil], best-effort targets never above base, a hurt SLO tenant is never cut
    n_ev = [0, 0, 0, 0]
    for trial in range(300):
        s = st_new(names)
        for k in range(80):
            t_ = 1.0 + 0.5 * k
            rat = {g: (rng.choice([0.3, 0.9, 0.95, 1.0, 1.07]) if g in ("V1", "V2") else INF) for g in names}
            for g in names:
                if rng.random() < 0.4:
                    s["u_pos"][g] = t_ - 0.5
            before = dict(s["tgt"])
            ours, others, donors = E.hyb_classify(s, t_, rat)
            ev = E.hyb_step(s, t_, rat, {g: rng.uniform(0, 70) for g in names})
            for i, b in enumerate((1, 2, 4, 8)):
                n_ev[i] += bool(ev & b)
            for g in names:
                d_ = s["tgt"][g] - before[g]
                assert s["floor"] - 1e-9 <= s["tgt"][g] <= s["ceil"] + 1e-9
                if g in ours:
                    assert d_ >= 0, ("raise must not lower", g)
                if g in donors and ev & 2:
                    assert d_ <= 0, ("cut must not raise", g)
                if rat[g] < E.HYB_THRESH:
                    assert d_ >= 0, ("a hurt SLO tenant is never lowered", g)
                if rat[g] == INF:
                    assert s["tgt"][g] <= s["base"] + 1e-9, ("best effort never above base", g)
                    assert g not in ours
    assert all(n_ev), n_ev
    # ... and the inner loop has the right sign for every target move: a higher target never gives a higher u
    K = E.K_PAPER
    for trial in range(400):
        cpus = [rng.uniform(0, 90) for _ in range(rng.randint(1, 8))]
        lo_t = rng.uniform(8, 60); hi_t = lo_t * rng.uniform(1.0, 1.6)
        sa, sb = E.lqo_new(), E.lqo_new()
        for c_ in cpus[:-1]:
            E.lqo_step(sa, c_, lo_t, K); E.lqo_step(sb, c_, lo_t, K)
        ua = E.lqo_step(sa, cpus[-1], lo_t, K)[0]      # target held
        ub = E.lqo_step(sb, cpus[-1], hi_t, K)[0]      # target raised at this step (edot falls too)
        assert ub <= ua, (cpus, lo_t, hi_t, ua, ub)
    print(f"CHECK 3 OK (hyb RULES): (a) hurt + our drop within {E.HYB_T:g} s -> x1.25 per step, clamp ceiling 6 x "
          f"cpu_ref; (b) hurt + no drop of ours -> donors x0.8, floor 0.2 x cpu_ref, once per {E.HYB_CUT_HOLD:g} s, "
          f"in-band SLO tenants and raised tenants are not donors; (c) {E.HYB_T:g} s hold, restore at cpu_ref/16 per s, "
          f"raised target guarded at CPU_eff + 0.4 x cpu_ref; nothing moves when nobody is hurt; relabelling symmetric "
          f"(30 x 60 steps); NO SIGN FLIPS in 300 x 80 random steps (events raise/cut/up/down {n_ev}) and lqr_orig's u "
          f"never rises when its target rises (400 cases)")


# ---------------- Part 4: hyb on the real run() ----------------

def tgt_path(ts, g, until=math.inf):
    # (t, target) of tenant g on the controller steps up to `until` (s), and its exempt flag on those steps
    i = TEN.index(g)
    return [(row[0], row[E.TS_TGT + i], row[E.TS_EX + i], int(row[E.TS_HEV])) for row in ts
            if row[E.TS_CTL] == 1 and row[0] <= until + 1e-9 and not math.isnan(row[E.TS_TGT + i])]


def changes(path):
    out = []
    for (t0_, a, _, _), (t1_, b, _, ev) in zip(path, path[1:]):
        if abs(b - a) > 1e-9:
            out.append((t1_, a, b, ev))
    return out


def part4():
    setup(E, E.all_configs(), CALIB_SIM)
    base = 0.6 * CALIB_SIM["cpu_ref"]
    ceil = E.HYB_CEIL_FRAC * CALIB_SIM["cpu_ref"]
    floor = E.HYB_FLOOR_FRAC * CALIB_SIM["cpu_ref"]
    U_COLS = range(E.TS_VH, E.TS_TGT)   # vhost, CPU_eff, e, u, ctl and the victim columns
    # (i) no victim hurt -> hyb == lqr_orig T60 tick by tick
    cases = [("S1", {}), ("S2", {}), ("S3", {"vh_victim": 0.25})]   # S3: victims 22-25 % < T60 30 %: never dropped
    for s, kw in cases:
        with scenario(s):
            ro, tso = sim_run(E, "lqr_orig", "T60_paperK", Sim(seed=21, **kw))
            rh, tsh = sim_run(E, "hyb", "T60_paperK", Sim(seed=21, **kw))
            bad = [(n, x, y) for n, x, y in zip(E.RESULT_NAMES[:E.R_X45], ro, rh) if not same(x, y)]
            assert not bad, (s, bad)
            assert all(same(ro[rcol(f"ubelow{g}_frac")], rh[rcol(f"ubelow{g}_frac")]) for g in TEN)
            assert len(tso) == len(tsh)
            for a, b_ in zip(tso, tsh):
                assert all(same(a[i], b_[i]) for i in U_COLS), s
                assert all(same(a[i], b_[i]) for i in range(E.TS_RATE, E.TS_BUDGET))   # passed rates
                assert all(same(a[i], b_[i]) for i in range(E.TS_UB, E.TS_RTT))         # u > 0 while CPU < target
            assert rh[rcol("hyb_raises")] == 0 and rh[rcol("hyb_cuts")] == 0 and math.isnan(rh[rcol("hyb_first_raise_s")])
            ctl_t = [row[E.TS_TGT + i] for row in tsh for i, g in enumerate(TEN) if row[E.TS_CTL] == 1 and
                     not math.isnan(row[E.TS_TGT + i])]
            assert ctl_t and all(x == base for x in ctl_t)
            assert all(row[E.TS_HEV] == 0 for row in tsh)
            assert all(row[E.TS_EX + i] == 0 for row in tsh for i in range(len(TEN)) if not math.isnan(row[E.TS_EX + i]))
            assert all(math.isnan(rh[rcol(f"exempt{g}_s")]) and math.isnan(rh[rcol(f"ceil{g}_s")]) for g in TEN)
            assert rh[rcol("hyb_min_be_tgt")] == base
            if s == "S3":
                assert rh[rcol("uV1_mean")] == 0 and rh[rcol("slo_ok_worst")] == 1.0
    # (ii) simulated S3 throttle: what rule (a) does to a throttled victim's target, nothing asserted about u or SLO.
    #      A: victims at 36 / 40 % (> T60 30 %): the target rises x 1/BETA per raise until our drop stops, then the
    #         exempt flag turns on (in the simulator the cycle breaks below the ceiling);
    #      B: victims at 90 / 100 %: the target rises x 1/BETA per raise all the way to the ceiling (logged ceil<v>_s);
    #         whether it then becomes exempt is recorded, not asserted.
    info2 = []
    for case, vh in (("A", 0.4), ("B", 1.0)):
        with scenario("S3"):
            kd = {}
            ro, tso = sim_run(E, "lqr_orig", "T60_paperK", Sim(seed=31, vh_victim=vh))
            sim = Sim(seed=31, vh_victim=vh)
            rh, tsh = sim_run(E, "hyb", "T60_paperK", sim, kinds_during=kd)
            assert all(kd[g] == "dropfrac" for g in TEN) and all(sim.kinds[f"vnet{g}"] == "limit" for g in TEN)
            for j, v in enumerate(V):
                ex, ce = rh[rcol(f"exempt{v}_s")], rh[rcol(f"ceil{v}_s")]
                # the flag is exactly "target above base and no drop of ours on v for HYB_T s" on every hyb step
                last_u = -math.inf
                for row in tsh:
                    if row[E.TS_CTL] != 1 or row[0] >= E.OFFSET + E.T:
                        continue
                    if row[E.TS_VU + j] > 0:
                        last_u = row[0]
                    want = 1.0 if (row[E.TS_TGT + j] > base + 1e-9 and row[0] - last_u >= E.HYB_T - 1e-9) else 0.0
                    assert row[E.TS_EX + j] == want, (case, v, row[0], row[E.TS_EX + j], want)
                if case == "A":
                    assert not math.isnan(ex) and ex < E.OFFSET + E.T, (v, ex)
                    path = tgt_path(tsh, v, ex)
                    ch = changes(path)
                    assert path[0][1] == base and len(ch) >= 3 and path[-1][2] == 1
                    assert all(f == 0 for _, _, f, _ in path[:-1])
                    for t_, a, b, ev in ch:   # every move up to the exemption is a raise x 1/BETA (or the clamp)
                        assert ev & 1 and (abs(b - a / E.HYB_BETA) < 1e-9 or (b == ceil and a / E.HYB_BETA > ceil)), (v, a, b)
                    info2.append(f"A {v}: {base:g} -> {path[-1][1]:.1f} in {len(ch)} raises, exempt at {ex:.2f} s"
                                 + (", ceiling not reached" if math.isnan(ce) else f", ceiling at {ce:.2f} s"))
                else:
                    assert not math.isnan(ce) and ce < E.OFFSET + E.T, (v, ce)
                    path = tgt_path(tsh, v, ce)
                    ch = changes(path)
                    n_steps = math.ceil(math.log(ceil / base) / math.log(1 / E.HYB_BETA))
                    assert path[0][1] == base and path[-1][1] == ceil and path[-1][0] == ce and len(ch) == n_steps
                    for t_, a, b, ev in ch:
                        assert ev & 1 and (abs(b - a / E.HYB_BETA) < 1e-9 or (b == ceil and a / E.HYB_BETA > ceil)), (v, a, b)
                    info2.append(f"B {v}: {base:g} -> ceiling {ceil:g} in {n_steps} raises by {ce:.2f} s, "
                                 + ("NOT exempt (still flip-flopping at the ceiling)" if math.isnan(ex)
                                    else f"exempt at {ex:.2f} s"))
                assert 0.0 <= rh[rcol(f"ubelow{v}_frac")] <= 1.0
            fl_t = [row[E.TS_TGT + E.NV + i] for row in tsh for i in range(E.NF)
                    if not math.isnan(row[E.TS_TGT + E.NV + i])]
            assert fl_t and max(fl_t) <= base + 1e-9                 # the flooders' targets are never raised
            assert all(math.isnan(rh[rcol(f"exempt{g}_s")]) for g in F)
            assert all(row[E.TS_HEV] == 0 for row in tsh if row[0] >= E.OFFSET + E.T)   # loop stops at the window end
            rel = rh[rcol("victim_release_s")]
            assert E.OFFSET + E.T <= rel < E.OFFSET + E.T + E.DT + 0.1 and rh[rcol("victim_win_after_release")] == 0
            info2.append(f"{case} for the record (not asserted): slo_ok_worst lqr_orig {ro[rcol('slo_ok_worst')]:.3f} / "
                         f"hyb {rh[rcol('slo_ok_worst')]:.3f}, uV2 {ro[rcol('uV2_mean')]:.3f} / {rh[rcol('uV2_mean')]:.3f}, "
                         f"u>0 while CPU<target V2 {ro[rcol('ubelowV2_frac')]:.3f} / {rh[rcol('ubelowV2_frac')]:.3f}")
            line = E.csv_line(1, "S3", 0, "hyb", "T60_paperK", rh, REFS3, RREF, {"V1": 30.0, "V2": 33.0})
            assert len(line.strip().split(",")) == len(E.HEADER.split(","))
            assert all(len(E.ts_line(1, "S3", 0, "hyb", "x", row).strip().split(",")) == len(E.TS_HEADER.split(","))
                       for row in tsh)
    # (iii) simulated flooder-hurts-victim (victims uncontrolled in S1 / S2; their delivered rate falls with the
    #       flooders' passed rate): the flooder targets are cut step by step (x BETA per cut, at most one cut per
    #       HYB_CUT_HOLD) down to the floor, and logged. Rule (b) is expected to be nearly a no-op on u (lqr_orig
    #       flip-flops), so NOTHING is asserted about the drop fraction, the flooder rate or the SLO.
    info3 = []
    for s, c in (("S1", 0.25), ("S2", 0.15)):
        with scenario(s):
            ro, tso = sim_run(E, "lqr_orig", "T60_paperK", Sim(seed=41, contention=c))
            sim = Sim(seed=41, contention=c)
            rh, tsh = sim_run(E, "hyb", "T60_paperK", sim)
            assert ro[rcol("slo_ok_worst")] < 0.9, ro[rcol("slo_ok_worst")]   # the setup: the contention really hurts
            n_cuts = math.ceil(math.log(base / floor) / math.log(1 / E.HYB_BETA))
            assert rh[rcol("hyb_cuts")] >= n_cuts and rh[rcol("hyb_raises")] == 0, (rh[rcol("hyb_cuts")], n_cuts)
            assert abs(rh[rcol("hyb_min_be_tgt")] - floor) < 1e-9
            for g in E.active():
                path = tgt_path(tsh, g)
                t_floor = min(t_ for t_, x, _, _ in path if abs(x - floor) < 1e-9)
                ch = changes([p_ for p_ in path if p_[0] <= t_floor])
                assert len(ch) == n_cuts, (s, g, ch)
                for t_, a, b, ev in ch:
                    assert ev & 2 and (abs(b - a * E.HYB_BETA) < 1e-9 or (b == floor and a * E.HYB_BETA < floor)), (a, b)
                cut_t = [t_ for t_, _, _, _ in ch]
                assert all(b_ - a_ >= E.HYB_CUT_HOLD - 1e-9 for a_, b_ in zip(cut_t, cut_t[1:]))
                assert all(x >= floor - 1e-9 for _, x, _, _ in path)
            assert all(sim.kinds[f"vnet{g}"] == "limit" for g in TEN)
            assert math.isnan(rh[rcol("tgtV1_mean")]) and math.isnan(rh[rcol("uV1_mean")])   # victims not controlled
            info3.append(f"{s}: {int(rh[rcol('hyb_cuts')])} cuts, {base:g} -> floor {floor:g} in {n_cuts} cuts (logged "
                         f"as hyb_ev bit 2); for the record (not asserted) uA lqr_orig {ro[rcol('uA_mean')]:.3f} vs hyb "
                         f"{rh[rcol('uA_mean')]:.3f}")
            line = E.csv_line(1, s, 0, "hyb", "T60_paperK", rh, REFS, RREF, {"V1": 30.0, "V2": 33.0})
            assert len(line.strip().split(",")) == len(E.HEADER.split(","))
            assert all(len(E.ts_line(1, s, 0, "hyb", "x", row).strip().split(",")) == len(E.TS_HEADER.split(","))
                       for row in tsh)
    # a failure INSIDE a hyb slot still restores xdp_limit on all six
    with scenario("S3"):
        sim = Sim(seed=6)
        saved_run = E.run
        try:
            E.run = lambda *a, **k: (_ for _ in ()).throw(SystemExit("stopped by signal SIGTERM"))
            with patched(E, sim):
                fake_if(E, sim)
                with contextlib.redirect_stdout(io.StringIO()):
                    try:
                        with E.dropfrac_tenants(E.lqo_tenants()):
                            E.run("hyb", "T60_paperK", REFS3, 50.0, [], dict(RREF))
                    except SystemExit as e:
                        assert "SIGTERM" in str(e)
            assert all(sim.kinds[f"vnet{g}"] == "limit" for g in TEN)
        finally:
            E.run = saved_run
    print(f"CHECK 4 OK (hyb on the real run()): (i) == lqr_orig T60 tick by tick when no victim is hurt (S1, S2, S3 "
          f"with victims below target): no event, no exempt flag, targets stay {base:g}; (ii) S3 throttle: "
          + "; ".join(info2) + "; exempt flag == (target > base and no drop of ours for 2 s) on every step, flooder "
          "targets never raised; (iii) flooder "
          "hurts victims: " + "; ".join(info3) + "; xdp_limit restored after a failure inside a hyb slot; CSV/ts "
          "field counts")


# ---------------- Part 5: summary ----------------

def part5():
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "res.csv")
        names = [m for m, _, _ in E.SUMMARY_METRICS] + ["slo_ok_v2_rtt", "v2_rtt_p50_ms", "v2_loss_pct"]
        cols = ["rep", "scenario", "slot", "mode", "param"] + list(dict.fromkeys(names))
        ref, HY = ("vfrot", "0.8_1_2_0.94"), ("hyb", "T60_paperK")
        base = {m: 0.5 for m in cols[5:]}
        def row(rep, s, c, **kw):
            d = dict(base); d.update(kw)
            return [rep, s, 0, c[0], c[1]] + [d[m] for m in cols[5:]]
        with open(path, "w") as f:
            w = csvmod.writer(f); w.writerow(cols)
            for rep in range(1, 5):
                w.writerow(row(rep, "S3", ("alone", "na"), slo_ok_worst=1.0, slo_ok_v2_rtt=1.0))
                w.writerow(row(rep, "S3", ref, slo_ok_worst=0.93, flooder_total_gbps=17.0))
                w.writerow(row(rep, "S3", HY, slo_ok_worst=[0.95, 0.9, 0.93, 0.97][rep - 1],
                               flooder_total_gbps=[40, 45, 30, 50][rep - 1]))
        order, stats, diffs, alone = E.summary_data(path)
        assert order == [("S3",) + ref, ("S3",) + HY]
        d = diffs["S3"][("S3",) + HY]
        assert d["slo_ok_worst"][3] == (2, 1, 1) and d["flooder_total_gbps"][3] == (4, 0, 0)
        p = scipy.stats.ttest_rel([40, 45, 30, 50], [17] * 4).pvalue
        assert abs(d["flooder_total_gbps"][4] - p) < 1e-9
        txt = E.summarize(path)
        for w_ in ("SUMMARY45", "PAIRED S3 vs vfrot 0.8_1_2_0.94", "hyb", "TABLE scenario x controller", "ALONE S3: 4 slots"):
            assert w_ in txt, w_
    finally:
        shutil.rmtree(tmp)
    print("CHECK 5 OK: SUMMARY45 with hyb rows: paired vs vfrot per scenario (W/T/L 2/1/1 and 4/0/0, p == scipy), table, "
          "alone lines")


# ---------------- Part 6: refusals, fail fast, dry main(), run45.sh ----------------

def fake_result(s, m, p, rep):
    r = [0.5] * len(E.RESULT_NAMES)
    r[rcol("flooder_total_gbps")] = 10.0 + 3 * E.CONFIGS.index((m, p)) + 0.1 * rep if m != "alone" else 0.0
    for c in ("flooder_wait_timeouts", "victim_wait_timeouts", "victim_win_after_release", "steal_mean", "steal_max",
              "hyb_raises", "hyb_cuts"):
        r[rcol(c)] = 0.0
    r[rcol("tick_mean_s")] = 0.25
    r[rcol("cpu_mean")] = 85.0
    return tuple(r)


def fake_ts_row():
    return tuple([1.0] * E.TS_CTL + [1] + [1.0] * (E.TS_HEV - E.TS_CTL - 1) + [0] + [1.0] * (E.TS_RTT - E.TS_EX)
                 + [0.2, 0])


class DryMain:
    def __init__(self, tmp, calib=CALIB_SIM, ref_fail=None, nproc=6):
        self.tmp, self.calib, self.ref_fail, self.nproc = tmp, calib, ref_fail, nproc
        self.calls, self.sleeps, self.runs, self.swaps = [], [], [], []

    def __enter__(self):
        cpath = os.path.join(self.tmp, "calib.json")
        if self.calib is not None:
            json.dump(self.calib, open(cpath, "w"))
        elif os.path.exists(cpath):
            os.remove(cpath)
        stubs = {
            "CSV": os.path.join(self.tmp, "results_dyn45.csv"), "TS_CSV": os.path.join(self.tmp, "ts_dyn45.csv"),
            "REF_FILE": os.path.join(self.tmp, "ref_victim45.txt"), "LOG": os.path.join(self.tmp, "exp45.log"),
            "CALIB": cpath, "REPS": 2,
            "host_cpus": lambda: (self.nproc, self.nproc),
            "sudo": lambda a: self.calls.append(("sudo", a)) or "",
            "check_servers": lambda: self.calls.append(("servers",)),
            "check_guests": lambda: self.calls.append(("guests",)),
            "resolve_vhost": lambda: self.calls.append(("vhost",)),
            "vnet": lambda dom: "vnet" + dom, "attach": lambda dev, cap: {"dev": dev, "prog": 1, "cfg": dev, "cnt": dev,
                                                                       "kind": "limit"},
            "restore_tenants": lambda: self.calls.append(("restore",)) or [], "vhost_ns": lambda p: 0,
            "set_cap": lambda d, c: None,
            "dropfrac_tenants": self.dropfrac, "reference": self.reference, "run": self.run,
        }
        self.saved = {k: getattr(E, k) for k in stubs}
        self.saved_scen = E.SCENARIO
        for k, v in stubs.items():
            setattr(E, k, v)
        self.real_sleep = time.sleep
        time.sleep = lambda s: self.sleeps.append(s)
        E.VH.update({g: "x" for g in TEN})
        return self

    def __exit__(self, *a):
        time.sleep = self.real_sleep
        for k, v in self.saved.items():
            setattr(E, k, v)
        E.SCENARIO = self.saved_scen

    @contextlib.contextmanager
    def dropfrac(self, tenants):
        self.swaps.append((E.SCENARIO, list(tenants))); self.calls.append(("swap_in",))
        yield
        self.calls.append(("swap_out",))

    def reference(self, rep, procs):
        self.calls.append(("reference", rep, E.SCENARIO))
        if (rep, E.SCENARIO) == self.ref_fail:
            raise SystemExit(f"REFUSING TO CONTINUE: reference rep {rep} {E.SCENARIO}: V2's vhost CPU 12.00% of one "
                             f"core < 23.00%")
        return dict(REFS), 55.0, dict(RREF), {"V1": 30.0, "V2": 33.0}, []

    def run(self, m, p, refs, base, procs, rref):
        self.calls.append(("run", E.SCENARIO, m, p)); self.runs.append((E.SCENARIO, m, p))
        return fake_result(E.SCENARIO, m, p, 1), [fake_ts_row(), fake_ts_row()]


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


FAKE_BIN = {
    "nproc": "#!/bin/sh\necho ${FAKE_NPROC:-6}\n",
    "ss": "#!/bin/sh\nfor p in 5201 5202 5203 5204 5205 5206; do echo \"LISTEN 0 5 0.0.0.0:$p 0.0.0.0:*\"; done\n",
    "pgrep": "#!/bin/sh\nexit 1\n",
    "sudo": "#!/bin/sh\necho \"$@\" >> \"$FAKE_LOG\"\nexit 1\n",
    "nohup": "#!/bin/sh\necho nohup \"$@\" >> \"$FAKE_LOG\"\nexit 0\n",
}


def run45(tmp, name, calib=CALIB_REAL, extra_py="", outputs=(), env=None, pgrep=None):
    d = os.path.join(tmp, "sh_" + name); os.mkdir(d)
    b = os.path.join(d, "bin"); os.mkdir(b)
    for k, v in dict(FAKE_BIN, **({"pgrep": pgrep} if pgrep else {})).items():
        open(os.path.join(b, k), "w").write(v); os.chmod(os.path.join(b, k), 0o755)
    shutil.copy(os.path.join(HERE, "run45.sh"), d)
    open(os.path.join(d, "exp45.py"), "w").write(open(os.path.join(HERE, "exp45.py")).read() + extra_py)
    if calib is not None:
        json.dump(calib, open(os.path.join(d, "calib42_result.json"), "w"))
    for o in outputs:
        open(os.path.join(d, o), "w").close()
    log = os.path.join(d, "fake.log")
    e = dict(os.environ, PATH=b + ":" + os.environ["PATH"], FAKE_LOG=log, PYTHONDONTWRITEBYTECODE="1", **(env or {}))
    p = subprocess.run(["sh", os.path.join(d, "run45.sh"), "6"], capture_output=True, text=True, env=e, cwd=d,
                       timeout=120)
    calls = open(log).read() if os.path.exists(log) else ""
    return p.returncode, p.stdout + p.stderr, calls, d


def run45_checks(tmp):
    rc, out, calls, d = run45(tmp, "ok")
    assert rc == 1 and "-v" in calls and "nohup" not in calls, (rc, out, calls)   # fake sudo -v fails: stops there
    assert "re-identified K (ZOH, T60) = [-0.0133, -0.0012] (K[0] < 0: OK)" in out, out
    assert "T40 target 16.44 %, S3 V2 vhost check >= 18.90 %" in out, out
    assert "hyb T60_paperK" in out and "HYB T60_paperK: lqr_orig (paper K [-0.2562, -0.3031]" in out, out
    assert "ceiling 246.59 % (T600)" in out and "floor 8.22 % (T20)" in out, out
    assert "15 slots (3 scenarios x (1 alone + 4 configs)) + 3 references" in out and "fits" in out, out
    assert not os.path.exists(os.path.join(d, "exp45.log"))
    rc, out, calls, d = run45(tmp, "kpaper", extra_py="\nK_PAPER = (0.2562, 0.3031)\n")
    assert rc == 1 and "K[0] >= 0" in out and calls == "", (out, calls)
    rc, out, calls, d = run45(tmp, "kpos", extra_py="\ndef lqo_gain_zoh(*a, **k):\n    return (0.0092, 0.0087)\n")
    assert rc == 1 and "K[0] >= 0" in out and calls == "", (out, calls)
    rc, out, calls, d = run45(tmp, "nocal", calib=None)
    assert rc == 1 and "calib42_result.json not found" in out and calls == ""
    rc, out, calls, d = run45(tmp, "fail", calib=dict(CALIB_REAL, verdict="FAIL"))
    assert rc == 1 and "verdict FAIL" in out and calls == ""
    rc, out, calls, d = run45(tmp, "nproc", env={"FAKE_NPROC": "4"})
    assert rc == 1 and "nproc = 4" in out and calls == ""
    rc, out, calls, d = run45(tmp, "busy44", pgrep='#!/bin/sh\ncase "$*" in *exp44.py*) exit 0;; esac\nexit 1\n')
    assert rc == 1 and "exp44.py is running" in out and calls == ""
    for o in OUTS:
        rc, out, calls, d = run45(tmp, "out_" + o.replace(".", "_"), outputs=(o,))
        assert rc == 1 and f"{o} already exists" in out and calls == ""
    return ("refuses (exit 1, sudo never called) on each existing output, nproc 4, missing / FAIL calibration, ZOH "
            "and paper K[0] >= 0, a running exp44; with everything OK it prints K, the S3 vhost threshold 18.90 %, the "
            "hyb constants (ceiling 246.59 %, floor 8.22 %), 15 slots, 'fits', then asks sudo -v (fake fails -> nothing "
            "started)")


def part6():
    tmp = tempfile.mkdtemp()
    try:
        for name in OUTS:
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
        saved = E.K_PAPER
        try:
            E.K_PAPER = (0.2562, 0.3031)
            dm = DryMain(d)
            main_exit(dm, "REFUSING TO START", "K[0] >= 0")
            assert dm.calls == [] and dm.sleeps == []
        finally:
            E.K_PAPER = saved
        d2 = os.path.join(tmp, "ff"); os.mkdir(d2)
        dm = DryMain(d2, ref_fail=(1, "S3"))
        main_exit(dm, "REFUSING TO CONTINUE", "S3")
        assert dm.runs == [] and E.SESSION_REST_S not in dm.sleeps and sum(dm.sleeps) < 60, dm.sleeps
        assert [c for c in dm.calls if c[0] == "reference"] == [("reference", 1, "S3")] and dm.calls[-1] == ("restore",)
        d3 = os.path.join(tmp, "dry"); os.mkdir(d3)
        dm = DryMain(d3)
        txt = run_main(dm)
        assert len(dm.runs) == 2 * 15
        random.seed(7)
        expect = []
        for rep in (1, 2):
            for s in E.scen_order(rep):
                o = E.SCEN_CONFIGS[s][:]; random.shuffle(o)
                expect += [(s, "alone", "na")] + [(s,) + c for c in o]
        assert dm.runs == expect
        assert sorted(dm.swaps) == sorted([("S1", ["A"])] * 6 + [("S2", ["A", "B"])] * 6 + [("S3", V + F)] * 6)
        inside = False
        for c in dm.calls:
            if c[0] == "swap_in": inside = True
            elif c[0] == "swap_out": inside = False
            elif c[0] == "run": assert inside == (c[2] in ("lqr_orig", "hyb")), c
        assert dm.sleeps.count(E.SESSION_REST_S) == 1
        lines = open(os.path.join(d3, "results_dyn45.csv")).read().splitlines()
        assert lines[0] == E.HEADER and len(lines) == 31
        assert all(len(l.split(",")) == len(E.HEADER.split(",")) for l in lines)
        tsl = open(os.path.join(d3, "ts_dyn45.csv")).read().splitlines()
        assert tsl[0] == E.TS_HEADER and len(tsl) == 61 and all(len(l.split(",")) == len(E.TS_HEADER.split(",")) for l in tsl)
        for w in ("lqr_orig / hyb on V1, V2, A, B, C, D (ALL SIX", "HYB T60_paperK: lqr_orig (paper K",
                  "S3 CHECK: V2's vhost CPU in each S3 reference must be >= 23.00 %", "EXPECTED DURATION: 15 slots/repeat",
                  ": fits;", "SUMMARY45", "PAIRED S1 vs vfrot", "PAIRED S3 vs vfrot", "HYB rep 1 S3 slot",
                  "LQR_ORIG rep 1 S3 slot", "DEVIATION (lqr_orig and hyb",
                  "HYB NOTE: rule (b) is expected to be NEARLY A NO-OP", "is flagged EXEMPT",
                  "HYB EXEMPT rep 1 S3 slot", "HYB CEILING rep 1 S3 slot", "(rule (b), expected near no-op)",
                  "FLIPFLOP rep 1 S3 slot", "u > 0 while CPU < target V1 0.500"):
            assert w in txt, w
        sh = run45_checks(tmp)
    finally:
        shutil.rmtree(tmp)
    print(f"CHECK 6 OK: exp45.main() refuses on each existing output, nproc 4, missing / FAIL calibration, K[0] >= 0 "
          f"(before sudo); FAIL FAST on rep 1's S3 reference before any slot and the 600 s rest; dry main(): 2 x 15 "
          f"slots, rotation and seed-7 order, xdp_dropfrac around lqr_orig AND hyb only (A / A+B / all six), CSV/ts "
          f"field counts, HYB lines and SUMMARY45 in the log; run45.sh: {sh}")


# ---------------- Part 7 ----------------

def part7(before):
    after = sha(list(SNAP))
    bad = [n for n in SNAP if after[n] != SNAP[n] or before[n] != SNAP[n]]
    assert not bad, f"protected files changed or missing: {bad}"
    present = [n for n in OUTS if os.path.exists(os.path.join(HERE, n))]
    assert not present, present
    print(f"CHECK 7 OK: sha256 of {len(SNAP)} exp44/exp43/calib42/XDP files == the snapshot taken when exp45 was built "
          f"(before and after this test); no exp45 output in the testbed")


def main():
    before = sha(list(SNAP))
    saved = (E.CSV, E.TS_CSV, E.REF_FILE, E.LOG, E.CALIB, E.REPS, E.SCENARIO, dict(E.VH_MIN))
    try:
        part0(); part1(); part2(); part3(); part4(); part5(); part6()
    finally:
        E.CSV, E.TS_CSV, E.REF_FILE, E.LOG, E.CALIB, E.REPS, E.SCENARIO = saved[:7]
        E.VH_MIN.clear(); E.VH_MIN.update(saved[7])
    part7(before)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
