#!/usr/bin/env python3
# Pure-python self-test for exp44.py and run44.sh: no VM, no sudo, no network, no traffic, no sleeping (simulated
# clock), nothing attached; run44.sh is executed in a temp dir against fake nproc/ss/pgrep/sudo on PATH.
# Part 0: scenarios (S1 A / S2 A+B / S3 steady4, V2 at V2_RATE_S3 in S3), configs (4 per scenario -> 15 slots),
#         time budget, scenario rotation, headers; controller / metric / safety functions source-identical to exp43.
# Part 1: K SIGN (as exp43): paper tau/b reproduce the paper K, the ZOH K on calib42 has K[0] < 0, refusals.
# Part 2: EQUIVALENCE: in an exp43-shaped scenario (steady4, both victims 9 Gbit/s, lqr_orig on the flooders only)
#         exp44.run() == exp43.run() tick by tick and in every exp43 result column, for vfrot, lqr 85_4, lqr_orig
#         T40_paperK and T60_paperK (same simulated testbed).
# Part 3: the REAL exp44.run() / reference() / dropfrac_tenants() per scenario on the simulated testbed: only the
#         scenario's flooders launch; nan columns for idle flooders; Jain nan in S1; lqr_orig on A (S1), A+B (S2),
#         ALL SIX (S3; victims dropped while above target, released at OFFSET + T before their iperf3 report, all
#         SLO windows before the release); xdp_limit back on all six after every lqr_orig slot and after a failure
#         inside it; S3 reference refuses in rep 1 (V2 vhost below 1.15 x T40, victim below 97 % of its rate),
#         warns later; CSV/ts field counts.
# Part 4: SUMMARY44 on a synthetic CSV: per-scenario stats, paired vs vfrot per scenario with known W/T/L, p ==
#         scipy, nan metrics skipped, the scenario x controller table, alone lines.
# Part 5: refusals of exp44.main() (outputs exist, nproc, calib, K[0] >= 0 before sudo); FAIL FAST (rep-1 S3 reference
#         refusal before any slot and before the 600 s rest); a dry main(): 2 repeats, rotation, ref -> alone ->
#         shuffled configs per scenario, swaps only around lqr_orig with the right tenants, CSV/ts lines, summary in
#         the log; run44.sh refusals against fake commands.
# Part 6: exp42/exp43/calib42/XDP files unchanged (sha256 snapshot taken when exp44 was built); no exp44 output.
import sys
sys.dont_write_bytecode = True
import contextlib, csv as csvmod, hashlib, inspect, io, json, math, os, random, shutil, signal, struct, subprocess
import tempfile, time
sys.argv = sys.argv[:1]
import numpy as np
import exp43, exp44
import scipy.linalg, scipy.stats   # test-only cross-checks

E, E43 = exp44, exp43
HERE = os.path.dirname(os.path.abspath(E.__file__))
F, V, TEN = E.FLOODERS, E.VICTIMS, E.TENANTS
nan = float("nan")
OUTS = ("results_dyn44.csv", "ts_dyn44.csv", "ref_victim44.txt", "exp44.log")
FLOOD_GBIT = 6.0
CPU_PER_GBIT = 10.0      # simulated vhost CPU, % of one core per passed Gbit/s
REFS = {"V1": 9.0, "V2": 9.0}
RREF = {"p50": 0.10, "p99": 0.25, "loss": 0.0, "n": 200}
CALIB_REAL = json.load(open(os.path.join(HERE, "calib42_result.json")))   # read only
CALIB_SIM = {"verdict": "WARN", "checks": [["x", "WARN", "sim"]], "cpu_ref": 50.0, "tau": 0.08, "b": 1000.0,
             "written": "test"}   # T40 target 20 %, T60 30 %
X43 = {"win": dict(E.STEADY["steady4"]), "v_rate": {"V1": 9.0, "V2": 9.0}, "lqo_victims": False}
# sha256 snapshot taken when exp44 was built (2026-11-07), before any test run
SNAPSHOT = """
00ce4364f9fcfd8722ca2c7fe5819839a845eeb716c8719eb3c983cbc425e700 exp42.py
4af307aefae532b31a7b80613a47af727e288d426013e7069ce8c5c7813ab8fd exp43.py
409f8f0c09525e79d45572cc1bcbfe200a6856f3bc63b046d7b1eb997f840349 run43.sh
dfc98d18d7ec6753c46a551c592cb3450622a1a235296b76631c86d292e1a2fb test_exp43.py
a18800a13072014eec579c8d314091388327fb0e62b3de767358149212ca6d86 results_dyn43.csv
4048b6eaa4ec6326ef67a73f12ed9b520fa31152a8b3398e0a2951824cca441d ts_dyn43.csv
c6eb9fa7a067248572d75d911c665bbf4e5b094378b144a43d158135c2c060a6 ref_victim43.txt
701450f6c5a73dd44a6c0f86bf24f2d906ecfa9ebd4ebb1bd8fc64d79afd7ea4 exp43.log
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


@contextlib.contextmanager
def scenario(name, spec=None):
    # select a scenario (optionally a test-only one), restore afterwards
    saved = E.SCENARIO
    if spec is not None:
        E.SCENARIOS[name] = spec
    try:
        E.set_scenario(name)
        yield
    finally:
        E.SCENARIO = saved
        if spec is not None:
            del E.SCENARIOS[name]


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
    # victim v offers v_eff x its -b rate inside its window and passes offer x (1 - drop_bp / 1e4) under
    # xdp_dropfrac (all of it under xdp_limit); a flooder offers FLOOD_GBIT inside its iperf3 window and passes
    # min(offer, cap) under xdp_limit (cap 0 = all) or offer x (1 - drop_bp / 1e4) under xdp_dropfrac; vhost CPU =
    # CPU_PER_GBIT x passed Gbit/s (x vh_victim for the victims); ping: replies with rtt(seq) ms except `lost`
    def __init__(self, seed=1, read_cost=0.002, lost=(), rtt=None, v_eff=1.0, vh_victim=1.0):
        self.rng, self.clock, self.read_cost = random.Random(seed), [1000.0], read_cost
        self.launches, self.capb, self.cum, self.kinds, self.ids = {}, {}, {}, {}, 100
        self.cpu_acc, self.lost = [0, 0, 0], set(lost)
        self.rtt = rtt or (lambda s: 0.1 + 0.01 * (s % 10))
        self.pings, self.ssh_log, self.writes = [], [], []
        self.v_eff, self.vh_victim = v_eff, vh_victim

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
        return {0: int(c[3]), 1: int(c[4]), 2: int(c[0]), 3: int(c[1])}

    def v_off(self, g, now):
        # victim g's cumulative offered bytes at `now`
        return sum(p["rate"] * self.v_eff * 1e9 / 8 * max(0.0, min(now - t - p["delay"], p["secs"]))
                   for t, p in self.launches.get(g, []))

    def vhost_ns(self, path):
        g = path[2:]
        mine = [x for k, x in self.cum.items() if str(k).split(":")[0] == f"cnt{g}"]
        if g in E.VICTIMS:   # the vhost thread runs whether or not a counter was read: offered so far - dropped
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
    refs = dict(refs or REFS)
    with patched(mod, sim):
        fake_if(mod, sim)
        with contextlib.redirect_stdout(io.StringIO()):
            args = (mode, param, refs, 50.0, [], dict(RREF))
            if mode == "lqr_orig":
                ctx = mod.dropfrac_tenants(mod.lqo_tenants()) if mod is E else mod.dropfrac_flooders()
                with ctx:
                    if kinds_during is not None:
                        kinds_during.update({g: mod.IF[g]["kind"] for g in mod.IF})
                    r, ts = mod.run(*args)
            else:
                r, ts = mod.run(*args)
    return r, ts


def setup(mod, configs, calib):
    mod.LQR.clear(); mod.LQO.clear(); mod.VFROT.clear()
    mod.setup_configs(configs, calib)


# ---------------- Part 0 ----------------

IDENTICAL = ("parse_vfrot", "vf_params", "active_flooders", "high_cap", "next_after", "hold_caps",
             "lowest_delivered_pick", "pidrotf_caps", "pf_update", "budget_shares", "highcap_shares",
             "lqo_filter", "lqo_soft", "lqo_quant", "lqo_step", "lqo_plant", "lqo_gain", "expm_np", "lqo_plant_zoh",
             "lqo_gain_zoh", "check_gain_sign", "lqo_eig", "lqo_new", "drop_model", "parse_lqo", "setup_lqo",
             "load_calib", "lqo_report", "parse_lqr", "lqr_plant", "lqr_gain", "lqr_report", "riccati",
             "jain", "p5", "nanstat", "dip_metrics", "q_incl", "parse_ping", "rtt_stats", "rtt_thresh", "rtt_ok",
             "ping_ticks", "ping_cmd", "iperf_sum", "ssh", "iperf_cmd", "wait_all", "check_servers", "check_guests",
             "vnet", "xdp_prog", "prog_maps", "load", "attach", "swap", "counters", "write_u64", "set_cap",
             "set_drop", "vhost_task", "vhost_ns", "vh_pct", "cpu", "host_cpus", "busy_steal", "cum_at", "rate_mbit",
             "sudo", "ms", "paired_p", "betainc", "_betacf")
SAME_CONSTS = ("VFROT_VS_TAU", "VFROT_THRESH", "VFROT_HOLD", "VFROT_CUT_HOLD", "VF_B0", "VF_BMAX", "VF_BMAX_RANGE",
               "CAP_MIN", "CAP_MAX", "DFS_ACTIVE_MBIT", "HIGH_CAP_FRAC", "SMOOTH", "LQO_DT", "LQO_EVERY", "LQO_MED_N",
               "LQO_TREND_W", "LQO_R", "LQO_Q", "LQO_ALPHA_NUM", "K_PAPER", "LQO_MARGIN", "LQO_UMAX", "LQO_LEVELS_BP",
               "LQO_WAIT_S", "T", "DT", "OFFSET", "V_RATE", "SLO_FRAC", "STEADY", "PAR", "GUESTS", "SESSION_REST_S",
               "PAUSE", "COOLDOWN", "TICK_WARN_S", "STEAL_WARN_MEAN", "STEAL_WARN_MAX", "NPROC_REQUIRED", "REF_RUNS",
               "REF_T", "PING_V", "PING_I", "RTT_FRAC", "RTT_FLOOR_MS", "RTT_LOSS_MAX", "REF_LOSS_MAX",
               "REF_RTT_MIN_N", "DIP_LEVELS", "LQR_TAU", "LQR_Q", "LQR_R", "K_CPU", "INIT_CAP_MBIT", "KP", "KI",
               "XDP_BURST_NS", "HOST", "VICTIMS", "FLOODERS")


def part0():
    S = E.SCENARIOS
    assert list(S) == ["S1", "S2", "S3"] and sorted(E.SCEN_ORDER) == sorted(S)
    assert S["S1"]["win"] == {"A": (0, 40)} and S["S2"]["win"] == {"A": (0, 40), "B": (6, 34)}
    assert S["S3"]["win"] == E43.STEADY["steady4"]
    assert S["S1"]["v_rate"] == S["S2"]["v_rate"] == {"V1": 9.0, "V2": 9.0}
    assert S["S3"]["v_rate"] == {"V1": 9.0, "V2": E.V2_RATE_S3} and E.V2_RATE_S3 > 9.0
    assert [S[s]["lqo_victims"] for s in S] == [False, False, True]
    assert E.CONFIGS == [("vfrot", "0.8_1_2_0.94"), ("lqr_orig", "T40_paperK"), ("lqr_orig", "T60_paperK"),
                         ("lqr", "85_4")]
    assert all(E.SCEN_CONFIGS[s] == E.CONFIGS for s in S) and E.PAIRED_REF == ("vfrot", "0.8_1_2_0.94")
    E.check_static()
    for name in IDENTICAL:
        assert inspect.getsource(getattr(E, name)) == inspect.getsource(getattr(E43, name)), name
    for k in SAME_CONSTS:
        assert getattr(E, k) == getattr(E43, k), k
    setup(E, E.all_configs(), CALIB_REAL)
    assert E.LQO["T40_paperK"][1] == E.LQO["T60_paperK"][1] == E.K_PAPER
    assert abs(E.LQO["T40_paperK"][0] - 0.4 * CALIB_REAL["cpu_ref"]) < 1e-9
    assert E.VFROT[("vfrot", "0.8_1_2_0.94")][4] == 16000.0
    # scenario helpers
    with scenario("S1"):
        assert E.active() == ["A"] and E.overlap() == (0.0, 40.0) and E.lqo_tenants() == ["A"]
        fc = E.flooder_cmds()
        assert list(fc) == ["A"] and fc["A"] == "iperf3 -c 10.10.0.1 -p 5202 -t 40 -P 2 -J"
        assert E.victim_cmds(40) == {"V1": "iperf3 -c 10.10.0.1 -p 5201 -t 40 -b 9G -J",
                                     "V2": "iperf3 -c 10.10.0.1 -p 5205 -t 40 -b 9G -J"}
    with scenario("S2"):
        assert E.active() == ["A", "B"] and E.overlap() == (6.0, 40.0) and E.lqo_tenants() == ["A", "B"]
        assert E.flooder_cmds()["B"] == "sleep 6; iperf3 -c 10.10.0.1 -p 5203 -t 34 -P 2 -J"
    with scenario("S3"):
        assert E.active() == F and E.overlap() == (18.0, 28.0) and E.lqo_tenants() == V + F
        assert E.flooder_cmds() == E43.flooder_cmds()
        assert E.victim_cmds(40)["V2"] == f"iperf3 -c 10.10.0.1 -p 5205 -t 40 -b {E.V2_RATE_S3:g}G -J"
    expect_exit(lambda: E.set_scenario("S4"), "unknown scenario")
    # rotation: rep 1 starts with S3; over 6 repeats every scenario twice in every position
    orders = [E.scen_order(r) for r in range(1, 7)]
    assert orders[0] == ["S3", "S1", "S2"] and orders[1] == ["S1", "S2", "S3"] and orders[2] == ["S2", "S3", "S1"]
    for pos in range(3):
        assert sorted(o[pos] for o in orders) == ["S1", "S1", "S2", "S2", "S3", "S3"]
    # headers / layout
    assert len(E.RESULT_NAMES) == E.R_X44 + len(E.X44_NAMES) and E.RESULT_NAMES[:E.R_X44] == E43.RESULT_NAMES
    assert E.HEADER.startswith("rep,scenario,slot,mode,param,")
    assert E.HEADER.endswith("ref_v2_rtt_p50_ms,ref_v2_rtt_p99_ms,ref_vhostV1_cpu,ref_vhostV2_cpu")
    h43 = E43.HEADER.split(","); h44 = E.HEADER.split(",")
    assert h44[:1] + h44[2:2 + len(h43) - 3] == h43[:-2], "exp44 header must extend exp43's (plus scenario)"
    assert len(E.TS_HEADER.split(",")) == 5 + E.TS_RTT + 2
    assert E.TS_HEADER.split(",")[5:5 + E.TS_CTL + 1] == E43.TS_HEADER.split(",")[4:4 + E43.TS_CTL + 1]
    # time budget
    n, per, tot = E.expected_duration(6)
    _, perm, totm = E.measured_duration(6)
    assert n == 15 and E.slots_per_repeat() == 15 and E.swap_vnets_per_repeat() == 2 * 1 + 2 * 2 + 2 * 6
    assert perm <= E.BUDGET_MIN_PER_REP * 60, perm / 60
    assert 2.2 * 3600 <= totm <= 2.5 * 3600, totm / 3600
    print(f"CHECK 0 OK: S1 A 0-40 / S2 A 0-40 + B 6-34 / S3 steady4 with V2 at {E.V2_RATE_S3:g} Gbit/s; lqr_orig on "
          f"A / A+B / all six; 4 configs x 3 scenarios + 3 alone = {n} slots, ~{perm / 60:.1f} min/repeat at exp43's "
          f"measured rate (nominal {per / 60:.1f}; budget {E.BUDGET_MIN_PER_REP:g}), R=6 ~{totm / 3600:.2f} h; rep 1 "
          f"starts with S3, rotation balanced; {len(IDENTICAL)} controller/metric/safety functions source-identical "
          f"to exp43, {len(SAME_CONSTS)} constants identical; headers extend exp43's")


# ---------------- Part 1: K sign ----------------

def part1():
    tau, b, cref = float(CALIB_REAL["tau"]), float(CALIB_REAL["b"]), float(CALIB_REAL["cpu_ref"])
    for target in (10.0, 16.44, 24.66):
        k = E.lqo_gain(E.LQO_TAU_PAPER, E.LQO_B_PAPER, target)
        assert abs(k[0] - E.K_PAPER[0]) < 5e-4 and abs(k[1] - E.K_PAPER[1]) < 5e-4, k
    K60 = E.calib_gain(CALIB_REAL)
    assert K60 == E43.calib_gain(CALIB_REAL)   # only its comment differs (run44.sh)
    assert K60[0] < 0 and K60[1] < 0 and abs(K60[0] + 0.0133) < 5e-4, K60
    A, B = E.lqo_plant_zoh(tau, b)
    Ac = np.array([[0.0, 1.0], [-1.0 / tau, -1.0 / tau]]); Bc = np.array([[0.0], [-b]])
    M = np.zeros((3, 3)); M[:2, :2] = Ac; M[:2, 2:] = Bc
    X = scipy.linalg.expm(M * E.LQO_DT)
    assert np.allclose(A, X[:2, :2], rtol=1e-9, atol=1e-12) and np.allclose(B, X[:2, 2:], rtol=1e-9, atol=1e-12)
    assert E.lqo_gain(tau, b, 0.6 * cref)[0] > 0   # exp42's Euler bug still reproduced (and refused)
    for bad in ((0.0092, 0.0087), (0.0, -0.1), (nan, -0.1)):
        expect_exit(lambda: E.check_gain_sign(bad, "t"), "REFUSING TO START", "K[0] >= 0")
    expect_exit(lambda: E.setup_configs([("lqr_orig", "T60")], CALIB_SIM), "malformed")
    print(f"CHECK 1 OK (K SIGN): paper tau/b -> K {E.K_PAPER}; ZOH K on calib42 [{K60[0]:.4f}, {K60[1]:.4f}] (K[0] < 0, "
          f"== scipy expm plant); exp42's Euler sign bug reproduced and K[0] >= 0 / nan refused; plain T60 refused")


# ---------------- Part 2: equivalence with exp43.run() ----------------

def part2():
    cfg = [("vfrot", "0.8_1_2_0.94"), ("lqr", "85_4"), ("lqr_orig", "T40_paperK"), ("lqr_orig", "T60_paperK")]
    setup(E43, cfg, CALIB_SIM)
    setup(E, cfg, CALIB_SIM)
    n_ticks = 0
    with scenario("X43", X43):
        for m, p in cfg:
            r43, ts43 = sim_run(E43, m, p, Sim(seed=11))
            r44, ts44 = sim_run(E, m, p, Sim(seed=11))
            assert len(r43) == len(E43.RESULT_NAMES) and len(r44) == len(E.RESULT_NAMES)
            assert all(same(x, y) for x, y in zip(r43, r44[:E.R_X44])), (m, p, [
                (n, x, y) for n, x, y in zip(E43.RESULT_NAMES, r43, r44) if not same(x, y)])
            assert len(ts43) == len(ts44) > 100
            for a, b_ in zip(ts43, ts44):
                assert len(b_) == E.TS_RTT + 2
                assert all(same(x, y) for x, y in zip(a[:E43.TS_CTL + 1], b_[:E.TS_CTL + 1])), (m, p)
                assert all(same(x, y) for x, y in zip(a[E43.TS_RTT:], b_[E.TS_RTT:])), (m, p)
            n_ticks += len(ts44)
            # victim extras: vhost logged for the victims in every mode; victim u only under S3-style lqr_orig
            assert r44[rcol("vhostV1_cpu_mean")] > 80 and math.isnan(r44[rcol("uV1_mean")])
            assert r44[rcol("n_active_flooders")] == 4 and math.isnan(r44[rcol("victim_release_s")])
    print(f"CHECK 2 OK (EQUIVALENCE): exp43-shaped scenario (steady4, both victims 9 Gbit/s, lqr_orig on the "
          f"flooders): exp44.run() == exp43.run() in every exp43 result column and tick by tick ({n_ticks} ticks) "
          f"for vfrot 0.8_1_2_0.94, lqr 85_4, lqr_orig T40_paperK and T60_paperK")


# ---------------- Part 3: scenarios on the real run() ----------------

def launched(sim):
    return sorted({g for g, c, t in sim.ssh_log if not c.startswith("ping")})


def part3():
    setup(E, E.all_configs(), CALIB_SIM)
    T40 = 0.4 * CALIB_SIM["cpu_ref"]
    # ---- S1 ----
    with scenario("S1"):
        sim = Sim(seed=3)
        r, ts = sim_run(E, "vfrot", "0.8_1_2_0.94", sim)
        assert launched(sim) == ["A", "V1", "V2"]
        for g in ("B", "C", "D"):
            for c in (f"flooder{g}_iperf_gbps", f"flooder{g}_ebpf_gbps", f"offered{g}_gbps", f"vhost{g}_cpu_mean"):
                assert math.isnan(r[rcol(c)]), c
        assert abs(r[rcol("flooder_total_gbps")] - r[rcol("flooderA_ebpf_gbps")]) < 1e-12
        assert r[rcol("flooderA_ebpf_gbps")] > 4.0 and math.isnan(r[rcol("jain_fairness")])
        assert r[rcol("n_active_flooders")] == 1 and r[rcol("slo_ok_worst")] == 1.0
        assert not math.isnan(r[rcol("dip95_ovl")])
        kd = {}
        sim = Sim(seed=3)
        r, ts = sim_run(E, "lqr_orig", "T40_paperK", sim, kinds_during=kd)
        assert kd == {"V1": "limit", "V2": "limit", "A": "dropfrac", "B": "limit", "C": "limit", "D": "limit"}, kd
        assert all(sim.kinds[f"vnet{g}"] == "limit" for g in TEN)   # all six restored
        assert r[rcol("uA_mean")] > 0 and math.isnan(r[rcol("uB_mean")]) and math.isnan(r[rcol("uV1_mean")])
        assert all(math.isnan(row[E.TS_U + 1]) for row in ts)   # B's u column is nan (not controlled)
        assert r[rcol("target_cpu")] == T40 and r[rcol("track_errA")] > 0 and math.isnan(r[rcol("track_errB")])
        assert math.isnan(r[rcol("victim_release_s")]) and r[rcol("victim1_ebpf_gbps")] > 8.9
    # ---- S2 ----
    with scenario("S2"):
        kd = {}
        sim = Sim(seed=4)
        r, ts = sim_run(E, "lqr_orig", "T60_paperK", sim, kinds_during=kd)
        assert [g for g in TEN if kd[g] == "dropfrac"] == ["A", "B"]
        assert launched(sim) == ["A", "B", "V1", "V2"]
        assert 0 < r[rcol("jain_fairness")] <= 1.0 and r[rcol("n_active_flooders")] == 2
        assert abs(r[rcol("flooder_total_gbps")] - r[rcol("flooderA_ebpf_gbps")] - r[rcol("flooderB_ebpf_gbps")]) < 1e-9
        assert r[rcol("min_flooder_own_gbps")] == min(r[rcol("flooderA_ebpf_gbps")], r[rcol("flooderB_ebpf_gbps")])
        r, ts = sim_run(E, "lqr", "85_4", Sim(seed=4))
        assert math.isnan(r[rcol("flooderC_ebpf_gbps")]) and r[rcol("flooderB_ebpf_gbps")] > 0
    # ---- S3 ----
    with scenario("S3"):
        refs3 = {"V1": 9.0, "V2": E.V2_RATE_S3}
        sim = Sim(seed=5)
        r, ts = sim_run(E, "vfrot", "0.8_1_2_0.94", sim, refs=refs3)
        assert any(g == "V2" and f"-b {E.V2_RATE_S3:g}G" in c for g, c, t in sim.ssh_log)
        assert r[rcol("v2_rate_gbps")] == E.V2_RATE_S3 and abs(r[rcol("victim2_ebpf_gbps")] - E.V2_RATE_S3) < 0.1
        assert r[rcol("slo_ok_worst")] == 1.0 and r[rcol("n_active_flooders")] == 4
        assert math.isnan(r[rcol("uV2_mean")]) and r[rcol("vhostV2_cpu_mean")] > 90
        kd = {}
        sim = Sim(seed=5)
        r, ts = sim_run(E, "lqr_orig", "T40_paperK", sim, refs=refs3, kinds_during=kd)
        assert all(kd[g] == "dropfrac" for g in TEN), kd
        assert all(sim.kinds[f"vnet{g}"] == "limit" for g in TEN)
        # the victims are controlled like any tenant: above target -> dropped -> SLO lost
        assert r[rcol("uV1_mean")] > 0.05 and r[rcol("uV2_mean")] > 0.05, (r[rcol("uV1_mean")], r[rcol("uV2_mean")])
        assert r[rcol("victim2_ebpf_gbps")] < 0.9 * E.V2_RATE_S3 and r[rcol("slo_ok_worst")] < 0.5
        assert r[rcol("track_errV2")] >= 0 and not math.isnan(r[rcol("track_errV2")])
        # release of the victims at OFFSET + T, before their iperf3 clients end and before the flooders' release
        rel = r[rcol("victim_release_s")]
        assert E.OFFSET + E.T <= rel < E.OFFSET + E.T + E.DT + 0.1, rel
        assert rel <= r[rcol("u_release_s")]
        assert r[rcol("victim_win_after_release")] == 0 and r[rcol("victim_wait_timeouts")] == 0
        vend = min(t + p["delay"] + p["secs"] + 0.3 for g, L in sim.launches.items() if g in V for t, p in L)
        t0 = min(t for g, c, t in sim.ssh_log if g in F)
        assert t0 + rel < vend, (rel, vend - t0)
        # after the release: no controller step on the victims, u 0 in the ts rows
        after = [row for row in ts if row[0] >= rel - 1e-9]
        assert after and all(row[E.TS_VU] == 0.0 and row[E.TS_VU + 1] == 0.0 for row in after)
        before = [row for row in ts if E.OFFSET + 2 <= row[0] < rel and row[E.TS_CTL] == 1]
        assert any(row[E.TS_VU + 1] > 0 for row in before)
        last = {}
        for t, cfg, val in sim.writes:
            last[cfg] = (t, val)
        vic_cfgs = [c for c in last if c.startswith("cfgV")]
        assert vic_cfgs and all(last[c][1] == 0 for c in vic_cfgs)   # victims' drop ends at 0
        # a failure INSIDE an S3 lqr_orig slot still restores xdp_limit on all six
        sim = Sim(seed=6)
        saved_run = E.run
        def boom(*a, **k):
            raise SystemExit("stopped by signal SIGTERM")
        try:
            E.run = boom
            with patched(E, sim):
                fake_if(E, sim)
                with contextlib.redirect_stdout(io.StringIO()):
                    try:
                        with E.dropfrac_tenants(E.lqo_tenants()):
                            assert all(E.IF[g]["kind"] == "dropfrac" for g in TEN)
                            E.run("lqr_orig", "T40_paperK", refs3, 50.0, [], dict(RREF))
                    except SystemExit as e:
                        assert "SIGTERM" in str(e)
            assert all(sim.kinds[f"vnet{g}"] == "limit" for g in TEN) and all(E.IF[g]["kind"] == "limit" for g in TEN)
        finally:
            E.run = saved_run
    # SIGTERM / SIGHUP -> SystemExit (the finally blocks run)
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
    try:
        E.exit_on_term_signals()
        h = signal.getsignal(signal.SIGTERM)
        expect_exit(lambda: h(signal.SIGTERM, None), "stopped by signal SIGTERM")
    finally:
        for s, hd in saved.items():
            signal.signal(s, hd)
    # CSV / ts line field counts
    with scenario("S3"):
        line = E.csv_line(1, "S3", 0, "lqr_orig", "T40_paperK", r, refs3, RREF, {"V1": 30.0, "V2": 33.0})
        assert len(line.strip().split(",")) == len(E.HEADER.split(","))
        assert all(len(E.ts_line(1, "S3", 0, "lqr_orig", "x", row).strip().split(",")) == len(E.TS_HEADER.split(","))
                   for row in ts)
    # the REAL reference(): per-scenario rates, victim vhost, S3 checks (rep 1 refuses, later WARNING)
    tmp = tempfile.mkdtemp()
    saved = E.REF_FILE
    try:
        E.REF_FILE = os.path.join(tmp, "ref.txt")
        E.VH_MIN["pct"] = E.S3_VH_MIN_FRAC * E.VH_MIN["frac"] * CALIB_SIM["cpu_ref"]   # 23 %
        def ref(sim, rep, scen):
            with scenario(scen), patched(E, sim):
                fake_if(E, sim)
                with contextlib.redirect_stdout(io.StringIO()):
                    return E.reference(rep, [])
        refs, base, rref, vh, w = ref(Sim(seed=2), 1, "S3")
        out = io.StringIO()
        with scenario("S3"), patched(E, Sim(seed=2)) as _, contextlib.redirect_stdout(out):
            fake_if(E, Sim(seed=2)); E.reference(1, [])
        line = [l for l in out.getvalue().splitlines() if l.startswith("S3 VHOST CHECK rep 1:")]
        assert len(line) == 1 and "threshold 23.00 % (1.15 x T40 target 20.00 %): PASS" in line[0], out.getvalue()
        assert f"V2 vhost CPU {vh['V2']:.2f} % of one core" in line[0]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                with scenario("S3"), patched(E, Sim(seed=2, vh_victim=0.2)):
                    fake_if(E, Sim(seed=2)); E.reference(1, [])
            except SystemExit:
                pass
        assert "S3 VHOST CHECK rep 1: V2 vhost CPU 19." in out.getvalue() and ": FAIL" in out.getvalue()
        assert abs(refs["V2"] - E.V2_RATE_S3) < 0.05 and abs(refs["V1"] - 9.0) < 0.05 and w == []
        # window = 20 s of traffic + the 0.3 s ssh tail of the fake process -> 20/20.3 of the rate
        assert abs(vh["V2"] / (CPU_PER_GBIT * E.V2_RATE_S3) - 1) < 0.03 and abs(vh["V1"] / (CPU_PER_GBIT * 9.0) - 1) < 0.03, vh
        txt = open(E.REF_FILE).read()
        assert ", S3, " in txt and f", V2, tenant5, {E.V2_RATE_S3:g}, " in txt
        # V2 vhost below 1.15 x T40 (sim: 0.2 x 10 %/Gbit x 10 Gbit/s ~ 19.7 % < 23 %)
        expect_exit(lambda: ref(Sim(seed=2, vh_victim=0.2), 1, "S3"), "REFUSING TO CONTINUE", "S3",
                    "V2's vhost CPU 19.", "< 23.00%")
        refs, base, rref, vh, w = ref(Sim(seed=2, vh_victim=0.2), 2, "S3")
        assert len(w) == 1 and "V2's vhost CPU" in w[0]
        refs, base, rref, vh, w = ref(Sim(seed=2, vh_victim=0.2), 1, "S1")   # the vhost check is S3 only
        assert w == []
        # a victim below 97 % of its offered rate
        expect_exit(lambda: ref(Sim(seed=2, v_eff=0.9), 1, "S1"), "REFUSING TO CONTINUE", "reached", "< 0.97")
        refs, base, rref, vh, w = ref(Sim(seed=2, v_eff=0.9), 3, "S2")
        assert len(w) == 2
        # ping refusal kept (12 % loss)
        expect_exit(lambda: ref(Sim(seed=2, lost=set(range(0, 100, 10)) | {1, 2}), 1, "S3"),
                    "reference ping failed", "loss 12.0%")
    finally:
        E.REF_FILE = saved
        shutil.rmtree(tmp)
    print("CHECK 3 OK (real run()/reference() per scenario): S1 launches A only, B/C/D columns nan, total == A, Jain "
          "nan, lqr_orig on A only (others xdp_limit); S2 lqr_orig on A+B, Jain over 2; S3 V2 at "
          f"{E.V2_RATE_S3:g} Gbit/s, lqr_orig on all six: victims above target get dropped (SLO lost), their drop is "
          "released at OFFSET + T before their iperf3 ends, no SLO window after it, victims' last drop write 0; "
          "xdp_limit back on all six after every lqr_orig slot and after a SIGTERM-style failure inside it; "
          "SIGTERM -> SystemExit; CSV/ts field counts; reference(): per-scenario rates and victim vhost CPU, S3 V2 "
          "vhost < 1.15 x T40 and victim < 97 % refuse in rep 1 and WARN later, ping refusal kept")


# ---------------- Part 4: summary ----------------

def part4():
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "res.csv")
        names = [m for m, _, _ in E.SUMMARY_METRICS] + ["slo_ok_v2_rtt", "v2_rtt_p50_ms", "v2_loss_pct"]
        cols = ["rep", "scenario", "slot", "mode", "param"] + list(dict.fromkeys(names))
        ref, T40 = ("vfrot", "0.8_1_2_0.94"), ("lqr_orig", "T40_paperK")
        base = {m: 0.5 for m in cols[5:]}
        def row(rep, s, c, **kw):
            d = dict(base); d.update(kw)
            if s == "S1":
                for g in ("B", "C", "D"):
                    d[f"flooder{g}_ebpf_gbps"] = nan
                d["jain_fairness"] = nan
            return [rep, s, 0, c[0], c[1]] + [d[m] for m in cols[5:]]
        with open(path, "w") as f:
            w = csvmod.writer(f); w.writerow(cols)
            for rep in range(1, 5):
                for s in ("S1", "S3"):
                    w.writerow(row(rep, s, ("alone", "na"), slo_ok_worst=1.0, slo_ok_v2_rtt=1.0 if rep < 4 else 0.0))
                    w.writerow(row(rep, s, ref, flooder_total_gbps=[16, 17, 16, 17][rep - 1], slo_ok_worst=0.9))
                    w.writerow(row(rep, s, T40, flooder_total_gbps=[5, 6, 18, 5][rep - 1],
                                   slo_ok_worst=[0.95, 0.9, 0.85, 0.97][rep - 1] if s == "S1" else 0.2,
                                   v2_rtt_p99_ms=[0.4, 0.5, 0.6, 0.5][rep - 1]))
        order, stats, diffs, alone = E.summary_data(path)
        assert order == [("S1",) + ref, ("S1",) + T40, ("S3",) + ref, ("S3",) + T40]
        d = diffs["S1"][("S1",) + T40]
        assert d["flooder_total_gbps"][3] == (1, 0, 3) and d["slo_ok_worst"][3] == (2, 1, 1)
        assert d["v2_rtt_p99_ms"][3] == (1, 2, 1)   # lower is better: 0.4 wins, 0.6 loses vs 0.5
        assert d["jain_fairness"][2] == 0 and d["flooderB_ebpf_gbps"][2] == 0   # undefined in S1: skipped
        assert diffs["S3"][("S3",) + T40]["slo_ok_worst"][3] == (0, 0, 4)
        p = scipy.stats.ttest_rel([5, 6, 18, 5], [16, 17, 16, 17]).pvalue
        assert abs(d["flooder_total_gbps"][4] - p) < 1e-9
        txt = E.summarize(path)
        for w_ in ("SUMMARY44", "PAIRED S1 vs vfrot 0.8_1_2_0.94", "PAIRED S3 vs vfrot 0.8_1_2_0.94",
                   "TABLE scenario x controller", "ALONE S1: 4 slots", "RTT SLO passed in 3/4"):
            assert w_ in txt, w_
        assert not any(" jain " in l and "S1" in l and "n=0" in l for l in txt.splitlines())
        tab = txt[txt.index("TABLE"):].splitlines()[2:6]
        assert tab[0].startswith("S1   vfrot") and "0.50/-/-/-" in tab[0] and tab[3].startswith("S3   lqr_orig"), tab
    finally:
        shutil.rmtree(tmp)
    print("CHECK 4 OK: SUMMARY44 per scenario (order S1, S3), paired vs vfrot per scenario with known W/T/L (fl_total "
          "1/0/3, ok_worst 2/1/1, rtt_p99 lower-is-better 1/2/1), p == scipy ttest_rel, metrics undefined in S1 "
          "(Jain, fl_B..D) skipped, scenario x controller table with '-' for idle flooders, ALONE lines per scenario")


# ---------------- Part 5: refusals, fail fast, dry main(), run44.sh ----------------

def fake_result(s, m, p, rep):
    r = [0.5] * len(E.RESULT_NAMES)
    r[rcol("flooder_total_gbps")] = 10.0 + 3 * E.CONFIGS.index((m, p)) + 0.1 * rep if m != "alone" else 0.0
    for c in ("flooder_wait_timeouts", "victim_wait_timeouts", "victim_win_after_release", "steal_mean", "steal_max"):
        r[rcol(c)] = 0.0
    r[rcol("tick_mean_s")] = 0.25
    r[rcol("cpu_mean")] = 85.0
    return tuple(r)


def fake_ts_row():
    return tuple([1.0] * E.TS_CTL + [1] + [1.0] * (E.TS_RTT - E.TS_CTL - 1) + [0.2, 0])


class DryMain:
    # every side effect of exp44.main() stubbed; records the call order
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
            "CSV": os.path.join(self.tmp, "results_dyn44.csv"), "TS_CSV": os.path.join(self.tmp, "ts_dyn44.csv"),
            "REF_FILE": os.path.join(self.tmp, "ref_victim44.txt"), "LOG": os.path.join(self.tmp, "exp44.log"),
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
        w = ["reference S2: victim V1 reached 8.0 Gbit/s"] if (rep, E.SCENARIO) == (2, "S2") else []
        return dict(REFS), 55.0, dict(RREF), {"V1": 30.0, "V2": 33.0}, w

    def run(self, m, p, refs, base, procs, rref):
        self.calls.append(("run", E.SCENARIO, m, p)); self.runs.append((E.SCENARIO, m, p))
        rep = sum(1 for c in self.calls if c[0] == "reference" and c[2] == "S3")
        return fake_result(E.SCENARIO, m, p, rep), [fake_ts_row(), fake_ts_row()]


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
        # K[0] >= 0 (a wrong-sign paper K): refused BEFORE sudo, servers, attach or any traffic
        saved = E.K_PAPER
        try:
            E.K_PAPER = (0.2562, 0.3031)
            dm = DryMain(d)
            main_exit(dm, "REFUSING TO START", "K[0] >= 0", "T40_paperK")
            assert dm.calls == [] and dm.sleeps == []
        finally:
            E.K_PAPER = saved
        # FAIL FAST: rep 1's FIRST reference is S3's; its refusal stops main() before any slot and before the rest
        d2 = os.path.join(tmp, "ff"); os.mkdir(d2)
        dm = DryMain(d2, ref_fail=(1, "S3"))
        main_exit(dm, "REFUSING TO CONTINUE", "S3")
        assert dm.runs == [] and E.SESSION_REST_S not in dm.sleeps and sum(dm.sleeps) < 60, dm.sleeps
        assert [c[0] for c in dm.calls][:4] == ["sudo", "servers", "guests", "vhost"]
        assert [c for c in dm.calls if c[0] == "reference"] == [("reference", 1, "S3")]
        assert dm.calls[-1] == ("restore",)   # the finally block restored all six
        # dry main(): 2 repeats
        d3 = os.path.join(tmp, "dry"); os.mkdir(d3)
        dm = DryMain(d3)
        txt = run_main(dm)
        assert len(dm.runs) == 2 * 15
        refs_seen = [(c[1], c[2]) for c in dm.calls if c[0] == "reference"]
        assert refs_seen == [(1, "S3"), (1, "S1"), (1, "S2"), (2, "S1"), (2, "S2"), (2, "S3")], refs_seen
        random.seed(7)
        expect = []
        for rep in (1, 2):
            for s in E.scen_order(rep):
                o = E.SCEN_CONFIGS[s][:]; random.shuffle(o)
                expect += [(s, "alone", "na")] + [(s,) + c for c in o]
        assert dm.runs == expect, (dm.runs, expect)
        # each block: reference first, then alone, then the configs
        seq = [c for c in dm.calls if c[0] in ("reference", "run")]
        for i, c in enumerate(seq):
            if c[0] == "reference":
                assert seq[i + 1][0] == "run" and seq[i + 1][2] == "alone" and seq[i + 1][1] == c[2]
        # swaps only around lqr_orig, with the scenario's tenants
        assert sorted(dm.swaps) == sorted([("S1", ["A"])] * 4 + [("S2", ["A", "B"])] * 4 + [("S3", V + F)] * 4)
        inside = False
        for c in dm.calls:
            if c[0] == "swap_in": inside = True
            elif c[0] == "swap_out": inside = False
            elif c[0] == "run": assert inside == (c[2] == "lqr_orig"), c
        assert dm.sleeps.count(E.SESSION_REST_S) == 1
        lines = open(os.path.join(d3, "results_dyn44.csv")).read().splitlines()
        assert lines[0] == E.HEADER and len(lines) == 1 + 30
        assert all(len(l.split(",")) == len(E.HEADER.split(",")) for l in lines)
        slots = [(l.split(",")[0], l.split(",")[1], int(l.split(",")[2])) for l in lines[1:]]
        assert [s[2] for s in slots] == list(range(15)) * 2
        tsl = open(os.path.join(d3, "ts_dyn44.csv")).read().splitlines()
        assert tsl[0] == E.TS_HEADER and len(tsl) == 1 + 60
        assert all(len(l.split(",")) == len(E.TS_HEADER.split(",")) for l in tsl)
        for w in ("SCENARIO S1: flooder A (tenant2) t=0-40 s", "SCENARIO S2:", "lqr_orig on V1, V2, A, B, C, D (ALL SIX",
                  "SCENARIO S3:", f"VICTIM CMD S3 V2: ssh alpine@10.10.0.15 'iperf3 -c 10.10.0.1 -p 5205 -t 40 -b "
                  f"{E.V2_RATE_S3:g}G -J'", "S3 CHECK: V2's vhost CPU in each S3 reference must be >= 23.00 %",
                  "LQR_ORIG T40_paperK: target 20.00", "PAPER K, unchanged", "EXPECTED DURATION: 15 slots/repeat",
                  ": fits;", "SUMMARY44", "TABLE scenario x controller", "PAIRED S1 vs vfrot", "PAIRED S3 vs vfrot",
                  "ALONE S2: 2 slots", "WARNINGS (", "rep 2 S2 slot -1 reference: reference S2: victim V1 reached",
                  "REFERENCES per repeat and scenario"):
            assert w in txt, w
        sh_checks = run44_checks(tmp)
    finally:
        shutil.rmtree(tmp)
    print("CHECK 5 OK: exp44.main() refuses on existing outputs (each of the 4), nproc 4, missing / FAIL calibration, "
          "K[0] >= 0 (before sudo, servers or attach); FAIL FAST: rep 1's first reference is S3's and its refusal stops "
          "main() before any slot and before the 600 s rest, restore in finally; dry main(): 2 x 15 slots, scenario "
          "rotation S3/S1/S2 then S1/S2/S3, reference -> alone -> seed-7 shuffled configs per block, xdp_dropfrac "
          "only around lqr_orig (A / A+B / all six), one 600 s rest, CSV/ts lines with the header's field count, "
          f"reference warnings logged, SUMMARY44/TABLE/PAIRED/ALONE in the log; run44.sh: {sh_checks}")


FAKE_BIN = {
    "nproc": "#!/bin/sh\necho ${FAKE_NPROC:-6}\n",
    "ss": "#!/bin/sh\nfor p in 5201 5202 5203 5204 5205 5206; do echo \"LISTEN 0 5 0.0.0.0:$p 0.0.0.0:*\"; done\n",
    "pgrep": "#!/bin/sh\nexit 1\n",
    "sudo": "#!/bin/sh\necho \"$@\" >> \"$FAKE_LOG\"\nexit 1\n",
    "nohup": "#!/bin/sh\necho nohup \"$@\" >> \"$FAKE_LOG\"\nexit 0\n",
}


def run44(tmp, name, calib=CALIB_REAL, extra_py="", outputs=(), env=None):
    d = os.path.join(tmp, "sh_" + name); os.mkdir(d)
    b = os.path.join(d, "bin"); os.mkdir(b)
    for k, v in FAKE_BIN.items():
        open(os.path.join(b, k), "w").write(v); os.chmod(os.path.join(b, k), 0o755)
    shutil.copy(os.path.join(HERE, "run44.sh"), d)
    src = open(os.path.join(HERE, "exp44.py")).read() + extra_py
    open(os.path.join(d, "exp44.py"), "w").write(src)
    if calib is not None:
        json.dump(calib, open(os.path.join(d, "calib42_result.json"), "w"))
    for o in outputs:
        open(os.path.join(d, o), "w").close()
    log = os.path.join(d, "fake.log")
    e = dict(os.environ, PATH=b + ":" + os.environ["PATH"], FAKE_LOG=log, PYTHONDONTWRITEBYTECODE="1", **(env or {}))
    p = subprocess.run(["sh", os.path.join(d, "run44.sh"), "6"], capture_output=True, text=True, env=e, cwd=d,
                       timeout=120)
    calls = open(log).read() if os.path.exists(log) else ""
    return p.returncode, p.stdout + p.stderr, calls, d


def run44_checks(tmp):
    rc, out, calls, d = run44(tmp, "ok")
    assert rc == 1 and "-v" in calls and "nohup" not in calls, (rc, out, calls)   # fake sudo -v fails: stops there
    assert "re-identified K (ZOH, T60) = [-0.0133, -0.0012] (K[0] < 0: OK)" in out, out
    assert "T40 target 16.44 %, S3 V2 vhost check >= 18.90 %" in out, out
    assert "15 slots (3 scenarios x (1 alone + 4 configs)) + 3 references" in out and "fits" in out, out
    assert f"S3 V2 rate {E.V2_RATE_S3:g} Gbit/s" in out, out
    assert not os.path.exists(os.path.join(d, "exp44.log"))
    rc, out, calls, d = run44(tmp, "kpos", extra_py="\ndef lqo_gain_zoh(*a, **k):\n    return (0.0092, 0.0087)\n")
    assert rc == 1 and "K[0] >= 0" in out and calls == "", (out, calls)
    rc, out, calls, d = run44(tmp, "kpaper", extra_py="\nK_PAPER = (0.2562, 0.3031)\n")
    assert rc == 1 and "K[0] >= 0" in out and "T40_paperK" in out and calls == "", (out, calls)
    rc, out, calls, d = run44(tmp, "nocal", calib=None)
    assert rc == 1 and "calib42_result.json not found" in out and calls == ""
    rc, out, calls, d = run44(tmp, "fail", calib=dict(CALIB_REAL, verdict="FAIL"))
    assert rc == 1 and "verdict FAIL" in out and calls == ""
    rc, out, calls, d = run44(tmp, "nproc", env={"FAKE_NPROC": "4"})
    assert rc == 1 and "nproc = 4" in out and calls == ""
    for o in OUTS:
        rc, out, calls, d = run44(tmp, "out_" + o.replace(".", "_"), outputs=(o,))
        assert rc == 1 and f"{o} already exists" in out and calls == ""
    return ("refuses (exit 1, sudo never called) on each existing output, nproc 4, missing calib42_result.json, "
            "verdict FAIL, ZOH K[0] >= 0 and paper K[0] >= 0; with everything OK it prints K = [-0.0133, -0.0012], "
            "the S3 vhost threshold 18.90 %, 15 slots, 'fits', then asks sudo -v itself (fake sudo fails -> nothing "
            "started)")


# ---------------- Part 6 ----------------

def part6(before):
    after = sha(list(SNAP))
    bad = [n for n in SNAP if after[n] != SNAP[n] or before[n] != SNAP[n]]
    assert not bad, f"protected files changed or missing: {bad}"
    present = [n for n in OUTS if os.path.exists(os.path.join(HERE, n))]
    assert not present, present
    print(f"CHECK 6 OK: sha256 of {len(SNAP)} exp42/exp43/calib42/XDP files == the snapshot taken when exp44 was "
          f"built (before and after this test); no exp44 output in the testbed")


def main():
    before = sha(list(SNAP))
    saved = (E.CSV, E.TS_CSV, E.REF_FILE, E.LOG, E.CALIB, E.REPS, E.SCENARIO, dict(E.VH_MIN))
    try:
        part0(); part1(); part2(); part3(); part4(); part5()
    finally:
        E.CSV, E.TS_CSV, E.REF_FILE, E.LOG, E.CALIB, E.REPS, E.SCENARIO = saved[:7]
        E.VH_MIN.clear(); E.VH_MIN.update(saved[7])
    part6(before)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
