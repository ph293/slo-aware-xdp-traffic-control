#!/usr/bin/env python3
# Pure-python self-test for exp41.py and pilot41.py: no VM, no sudo, no network, no traffic, no sleeping.
# exp41 = exp39/exp40 controllers (vfrot, vffair F0.25, lqr 79/85/90, static 2500/3500) under mixed traffic:
# V1 9G TCP, V2 2G TCP + ping every 0.2 s, A TCP x2, B UDP 6G, C bursty TCP 2 s on/2 s off, D TCP x8.
# Part 0: configs, headers, commands, duration.
# Part 1: pure functions: ping parser, percentile with lost pings as +inf, RTT SLO (p99 bound + loss <= 1 %), per-tick RTT, iperf3 sums,
#         paired t-test p-value (against scipy, test-only), burst schedule.
# Part 2: the REAL exp41.run() with a simulated clock, fake bpftool counters/caps, fake iperf3 and fake ping:
#         static/lqr/vfrot tick by tick against the REAL exp39.run() and vffair/vfrot against the REAL exp40.run()
#         when exp41 is fed exp39's traffic (controllers unchanged); then the new traffic (UDP not reduced by the
#         cap, C on/off, own-window means, RTT columns); the real reference() incl. its refusals.
# Part 3: the REAL exp41.main() with every side effect stubbed (slot order, CSV/ts, warnings, summary with known
#         answers), the refusals of main() and run41.sh; the REAL pilot41.main() simulated end to end (PASS / WARN
#         / FAIL cases), pilot41.sh refusals; no exp39/exp40 file changed and no exp41/pilot output appeared.
import sys
sys.dont_write_bytecode = True   # importing exp39/exp40/exp41 must not write anything into the testbed
import contextlib, hashlib, io, json, math, os, random, shutil, statistics, struct, subprocess, tempfile, time
sys.argv = sys.argv[:1]          # the experiments read REPS from argv: use the default (6)
import exp39, exp40, exp41, pilot41
import scipy.stats               # test-only cross-check of exp41.paired_p

E = exp41
HERE = os.path.dirname(os.path.abspath(E.__file__))
REPS = 6
ALONE = ("alone", "na")
REFS = {"V1": 9.0, "V2": 2.0}
REFS_OLD = {"V1": 9.0, "V2": 9.0}
RREF = {"p50": 0.33, "p99": 0.5, "loss": 0.0, "n": 200}
OUTS = ("results_dyn41.csv", "ts_dyn41.csv", "ref_victim41.txt", "exp41.log")
POUTS = ("pilot41_run.log", "pilot41_ts.csv", "pilot41_vfrot_ts.csv")
OLD_FILES = ("exp39.py", "run39.sh", "test_exp39.py", "results_dyn39.csv", "ts_dyn39.csv", "ref_victim39.txt",
             "exp39.log", "exp40.py", "run40.sh", "test_exp40.py", "results_dyn40.csv", "ts_dyn40.csv",
             "ref_victim40.txt", "exp40.log")
FLOOD_GBIT = 6.0
F = E.FLOODERS
REF_CFG = ("vfrot", "0.8_1_2_0.94")
CONFIGS = [("vfrot", "0.8_1_2_0.94"), ("vffair", "0.8_1_2_0.94_F0.25"), ("lqr", "79_4"), ("lqr", "85_4"),
           ("lqr", "90_4"), ("static", "2500"), ("static", "3500")]
G_BY_DOM = {v[0]: g for g, v in E.GUESTS.items()}
nan = float("nan")


def sha(names):
    return {n: (hashlib.sha256(open(os.path.join(HERE, n), "rb").read()).hexdigest()
                if os.path.exists(os.path.join(HERE, n)) else None) for n in names}


def name(c):
    return f"{c[0]} {c[1]}"


def same(a, b):
    return a == b or (isinstance(a, float) and isinstance(b, float) and a != a and b != b)


def rcol(n):
    return E.RESULT_NAMES.index(n)


def tcol(n):
    return E.TS_HEADER.split(",").index(n) - 4


def expect_exit(fn, *words):
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            fn()
    except SystemExit as e:
        msg = str(e)
        assert all(w in msg for w in words), (msg, words)
        return msg
    raise AssertionError(f"no SystemExit, expected {words}")


# ---------------- fake testbed: clock, bpftool, iperf3, ping ----------------

def parse_cmd(cmd):
    w = cmd.split()
    delay = float(w[1].rstrip(";")) if w[0] == "sleep" else 0.0
    par = int(w[w.index("-P") + 1]) if "-P" in w else 1
    rate = float(w[w.index("-b") + 1].rstrip("G")) * par if "-b" in w else None
    return {"kind": "iperf", "delay": delay, "secs": float(w[w.index("-t") + 1]), "rate": rate, "par": par,
            "udp": "-u" in w}


class FakeProc:
    def __init__(self, clock, end): self.clock, self.end = clock, end
    def poll(self): return 0 if self.clock[0] >= self.end else None
    def wait(self): self.clock[0] = max(self.clock[0], self.end); return 0
    def terminate(self): pass


class Sim:
    # victim v delivers vfac[v] x its -b rate; a flooder offers FLOOD_GBIT (TCP) or its -b rate (UDP, or
    # udp_actual if given) inside each of its iperf3 windows and passes min(offer, cap) (all of it if cap_leak);
    # ping(seq) -> RTT ms or None (lost); ping == "empty": no ping output at all (ssh failure)
    def __init__(self, seed=1, vfac=None, read_cost=0.002, ping=None, udp_actual=None, cap_leak=False, fail=()):
        self.rng, self.clock, self.vfac, self.read_cost = random.Random(seed), [1000.0], vfac or {}, read_cost
        self.ping = ping or (lambda s: 0.30 + 0.01 * (s % 7))
        self.udp_actual, self.cap_leak, self.fail = udp_actual, cap_leak, list(fail)
        self.launches, self.events, self.capb, self.cum, self.cpu_acc = {}, [], {}, {}, [0, 0, 0]

    def rate_of(self, p):
        return (self.udp_actual or p["rate"]) if p["udp"] else FLOOD_GBIT

    def sudo(self, args):
        if args == ["true"]:
            return ""
        assert args[:3] == ["bpftool", "map", "update"], args
        self.capb[args[4]] = struct.unpack("<Q", bytes.fromhex("".join(args[-8:])))[0]
        self.events.append(("set", args[4], self.capb[args[4]], self.clock[0]))
        return ""

    def counters(self, d):
        self.clock[0] += self.read_cost
        now, g = self.clock[0], d["cfg"][3:]
        c = self.cum.setdefault(g, [0.0, 0.0, now])
        L = [x for x in self.launches.get(g, []) if x[1]["kind"] == "iperf"]
        if g in E.VICTIMS:
            c[0] = sum(self.vfac.get(g, 1.0) * p["rate"] * 1e9 / 8 * max(0.0, min(now - t - p["delay"], p["secs"]))
                       for t, p in L)
        else:
            dt, c[2] = now - c[2], now
            off = sum(self.rate_of(p) * 1e9 / 8 * dt for t, p in L if t + p["delay"] <= now < t + p["delay"] + p["secs"])
            cap = self.capb.get(d["cfg"], 0)
            passed = off if cap == 0 or self.cap_leak else min(off, cap * dt)
            c[0] += passed; c[1] += off - passed
        return {0: 0, 1: 0, 2: int(c[0]), 3: int(c[1])}

    def cpu(self):
        self.cpu_acc[0] += 100; self.cpu_acc[1] += 40; self.cpu_acc[2] += 0
        return tuple(self.cpu_acc)

    def ssh(self, guest, cmd, out):
        now = self.clock[0]
        self.events.append(("ssh", guest, cmd, now))
        if cmd.startswith("ping"):
            w = cmd.split(); n = int(w[w.index("-c") + 1])
            self.launches.setdefault(guest, []).append((now, {"kind": "ping"}))
            if self.ping != "empty":
                lines, rx = [f"PING {E.HOST} ({E.HOST}): 56 data bytes"], 0
                for s in range(n):
                    r = self.ping(s)
                    if r is not None:
                        lines.append(f"64 bytes from {E.HOST}: seq={s} ttl=42 time={r:.3f} ms"); rx += 1
                lines += ["", f"--- {E.HOST} ping statistics ---",
                          f"{n} packets transmitted, {rx} packets received, {100 * (n - rx) // n}% packet loss"]
                out.write("\n".join(lines) + "\n"); out.flush()
            return FakeProc(self.clock, now + n * E.PING_I - 0.5)   # ends before the victims
        p = parse_cmd(cmd)
        self.launches.setdefault(guest, []).append((now, p))
        if any(guest == fg and sub in cmd for fg, sub in self.fail):
            out.write("iperf3: error - the server is busy running a test. try again later\n")
        else:
            gb = self.vfac.get(guest, 1.0) * p["rate"] if guest in E.VICTIMS else self.rate_of(p)
            out.write(json.dumps({"end": {"sum_received": {"bytes": int(gb * 1e9 / 8 * p["secs"]),
                                                           "bits_per_second": gb * 1e9}}}))
        out.flush()
        return FakeProc(self.clock, now + p["delay"] + p["secs"] + 0.3)

    def sleep(self, s):
        self.clock[0] += max(0.0, s) + self.rng.uniform(0.0, 0.04)


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


def fake_if(mod):
    mod.IF.clear()
    for g in mod.VICTIMS + mod.FLOODERS:
        mod.IF[g] = {"dev": f"vnet{g}", "prog": 0, "cfg": f"cfg{g}", "cnt": f"cnt{g}"}
    mod.last_cap.clear()


def sim_run(mod, mode, param, sim, refs=None):
    with patched(mod, sim):
        fake_if(mod)
        with contextlib.redirect_stdout(io.StringIO()):
            if mod is E:
                r, ts = mod.run(mode, param, dict(refs or REFS), 50.0, [], dict(RREF))
            else:
                r, ts = mod.run(mode, param, dict(refs or REFS_OLD), 50.0, [])
    return r, ts, sim.events


@contextlib.contextmanager
def old_traffic():
    # exp41 fed exp39/exp40's traffic: 9G + 9G victims, 2-stream TCP flooders (one command each)
    saved = (E.flooder_cmds, dict(E.V_RATE))
    E.flooder_cmds = lambda: {g: [c] for g, c in exp39.flooder_cmds().items()}
    E.V_RATE.update({"V1": 9.0, "V2": 9.0})
    try:
        yield
    finally:
        E.flooder_cmds = saved[0]; E.V_RATE.clear(); E.V_RATE.update(saved[1])


# ---------------- main() dry run ----------------

def redirect_outputs(tmp):
    E.CSV, E.TS_CSV = os.path.join(tmp, OUTS[0]), os.path.join(tmp, OUTS[1])
    E.REF_FILE, E.LOG = os.path.join(tmp, OUTS[2]), os.path.join(tmp, OUTS[3])


def fake_result(tick_mean=0.25, steal=(0.0, 0.0), **over):
    v = dict.fromkeys(E.RESULT_NAMES, 1.0)
    v.update({"jain_fairness": 0.9, "cpu_mean": 80.0, "cpu_max": 90.0, "steal_mean": steal[0], "steal_max": steal[1],
              "tick_mean_s": tick_mean, "tick_max_s": tick_mean + 0.1, "budget_ceiling_share": 0.5,
              "budget_floor_share": 0.0, "flooder_total_gbps": 4.0, "v2_rtt_p50_ms": 0.4, "v2_rtt_p99_ms": 0.8,
              "v2_loss_pct": 0.0})
    v.update(over)
    return tuple(v[n] for n in E.RESULT_NAMES)


def fake_ts_row(budget=16000.0):
    return ((20.0, 0.25, 0.01, 60.0, 0.0) + (9.0,) * E.NV + (9.0,) * E.NV
            + (budget - 600, 200.0, 200.0, 200.0) + (11.0, 0.2, 0.2, 0.2) + (budget, 0.99) + (0.4, 0))


def run_main_dry(slow=(), steal=(), results=None):
    tmp = tempfile.mkdtemp(prefix="test_exp41_")
    redirect_outputs(tmp)
    E.OBJ = os.path.abspath(E.__file__)
    calls, rep_now, sleeps = [], [0], []
    saved = {k: getattr(E, k) for k in ("sudo", "check_servers", "check_guests", "vnet", "attach", "set_cap",
                                        "reference", "run", "host_cpus")}
    E.sudo = lambda args: ""
    E.host_cpus = lambda: (6, 6)
    E.check_servers = lambda: None
    E.check_guests = lambda: None
    E.vnet = lambda dom: "vnet-" + dom
    E.attach = lambda dev, cap: {"dev": dev, "prog": 0, "cfg": dev, "cnt": dev}
    E.set_cap = lambda d, mbps: None

    def fake_reference(rep, procs):
        rep_now[0] = rep
        return dict(REFS), 50.0, dict(RREF)

    def fake_run(mode, param, refs, base_cpu, procs, rref):
        c = (mode, param)
        calls.append((rep_now[0], c, refs, rref))
        if results and (rep_now[0], c) in results:
            return results[(rep_now[0], c)], [fake_ts_row()]
        return (fake_result(0.31 if c in slow else 0.25, (1.5, 2.0) if c in steal else (0.0, 0.0)), [fake_ts_row()])

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
    return calls, out.getvalue(), tmp, sleeps


def fake_tools(bindir, d, ports, nproc, py_real):
    for tool, body in (("ss", "printf '%s\\n' " + " ".join(f"'LISTEN 0 5 *:{p} *:*'" for p in ports)),
                       ("nproc", f"echo {nproc}"), ("sudo", f"echo sudo >> {d}/MARK; exit 1"),
                       ("nohup", f"echo nohup >> {d}/MARK; exit 1"), ("pgrep", "exit 1"), ("tee", f"echo tee >> {d}/MARK"),
                       ("python3", f'exec {sys.executable} -B "$@"' if py_real else f"echo python3 >> {d}/MARK; exit 1")):
        with open(os.path.join(bindir, tool), "w") as fh:
            fh.write(f"#!/bin/sh\n{body}\n")
        os.chmod(os.path.join(bindir, tool), 0o755)


# ---------------- pilot dry run ----------------

def run_pilot_sim(sim):
    tmp = tempfile.mkdtemp(prefix="test_pilot41_")
    pilot41.TS, pilot41.VTS = os.path.join(tmp, POUTS[1]), os.path.join(tmp, POUTS[2])
    E.OBJ = os.path.abspath(E.__file__)

    def attach(dev, cap):
        d = {"dev": dev, "prog": 0, "cfg": "cfg" + G_BY_DOM[dev[5:]], "cnt": "cnt" + G_BY_DOM[dev[5:]]}
        E.set_cap(d, cap)
        return d

    extra = {"host_cpus": lambda: (6, 6), "check_servers": lambda: None, "check_guests": lambda: None,
             "vnet": lambda dom: "vnet-" + dom, "attach": attach}
    out = io.StringIO()
    with patched(E, sim, extra):
        E.IF.clear(); E.last_cap.clear()
        t_start = sim.clock[0]
        with contextlib.redirect_stdout(out):
            res = pilot41.main()
        elapsed = sim.clock[0] - t_start
    return {n: g for n, g, _ in res}, res, out.getvalue(), tmp, elapsed


if __name__ == "__main__":
    before_old = sha(OLD_FILES)
    before_new = sha(OUTS + POUTS)

    # ================= part 0: configs, headers, commands =================
    assert E.CONFIGS == CONFIGS, E.CONFIGS
    E.setup_configs(CONFIGS)
    assert E.VFROT[REF_CFG] == exp40.parse_vfrot(*REF_CFG) and E.VFROT[REF_CFG][:4] == exp39.parse_vfrot(REF_CFG[1])
    assert E.VFROT[("vffair", "0.8_1_2_0.94_F0.25")] == exp40.parse_vfrot("vffair", "0.8_1_2_0.94_F0.25")
    for p in ("79_4", "85_4", "90_4"):
        assert E.LQR[p][:2] == exp39.parse_lqr(p) and (E.LQR[p][2] == exp39.lqr_gain(exp39.parse_lqr(p)[1])).all()
    for m, p, w in (("static", "x", "malformed"), ("static", "100", "out of range"), ("lqr", "96_4", "out of range"),
                    ("vfrot", "0.8_1_2_0.94_F0.5", "only for vffair"), ("burst", "1", "not supported")):
        expect_exit(lambda: E.setup_configs([(m, p)]), w)
    expect_exit(lambda: E.setup_configs([REF_CFG, REF_CFG]), "duplicate")
    assert E.HEADER.startswith(exp40.HEADER + ","), "exp40 result columns must come first, unchanged"
    assert E.HEADER[len(exp40.HEADER) + 1:].split(",") == [f"flooder{g}_offered_gbps" for g in F] + [
        "v2_rtt_p50_ms", "v2_rtt_p99_ms", "v2_loss_pct", "slo_ok_v2_rtt", "slo_ok_worst_with_rtt",
        "ref_v2_rtt_p50_ms", "ref_v2_rtt_p99_ms"]
    assert E.TS_HEADER == exp40.TS_HEADER + ",v2_rtt_ms_max,v2_ping_lost"
    assert E.victim_cmds(40) == {"V1": "iperf3 -c 10.10.0.1 -p 5201 -t 40 -b 9G -J",
                                 "V2": "iperf3 -c 10.10.0.1 -p 5205 -t 40 -b 2G -J"}
    assert E.ping_cmd(40) == "ping -c 200 -i 0.2 -W 1 -w 42 10.10.0.1"
    assert E.ping_cmd(20) == "ping -c 100 -i 0.2 -W 1 -w 22 10.10.0.1"
    fc = E.flooder_cmds()
    assert fc["A"] == ["iperf3 -c 10.10.0.1 -p 5202 -t 28 -P 2 -J"] == [exp39.flooder_cmds()["A"]]
    assert fc["B"] == ["sleep 6; iperf3 -c 10.10.0.1 -p 5203 -t 28 -u -b 6G -J"]
    assert fc["C"] == [f"sleep {t}; iperf3 -c 10.10.0.1 -p 5204 -t 2 -P 2 -J" for t in (12, 16, 20, 24, 28, 32, 36)]
    assert fc["D"] == ["sleep 18; iperf3 -c 10.10.0.1 -p 5206 -t 22 -P 8 -J"]
    assert E.overlap() == exp40.overlap() == (18.0, 28.0) and [E.flooder_dur(g) for g in F] == [28, 28, 28, 22]
    n_slots, per_rep, total = E.expected_duration(7, REPS)
    _, per_rep_m, total_m = E.measured_duration(7, REPS)
    assert n_slots == 15 and per_rep <= 15 * 60 and per_rep_m <= 15 * 60
    print(f"CHECK 0 OK: 7 configs (vfrot/vffair parse as exp40, lqr gain == exp39's, static range) and malformed / "
          f"duplicate / unknown configs refused; results header = exp40 header + offered A-D, RTT p50/p99, loss, "
          f"slo_ok_v2_rtt, slo_ok_worst_with_rtt, ref RTT p50/p99; ts header = exp40 + v2_rtt_ms_max, v2_ping_lost; "
          f"exact commands for V1 9G, V2 2G, ping (200 / ref 100 probes), A TCP x2, B UDP 6G, C 7 x 2 s bursts at "
          f"12..36 s, D TCP x8; overlap 18-28 s and own windows 28/28/28/22 s as exp40")
    print(f"  duration: {n_slots} slots/repeat, formula {per_rep / 60:.2f} min/repeat -> {total / 3600:.2f} h; "
          f"exp39/40 measured rate {per_rep_m / 60:.2f} min/repeat -> {total_m / 3600:.2f} h")

    # ================= part 1: pure functions =================
    rng = random.Random(41)
    for _ in range(3000):
        n = rng.randint(2, 300)
        xs = [rng.choice([rng.uniform(0.1, 5), rng.expovariate(2)]) for _ in range(n)]
        qs = statistics.quantiles(xs, n=100, method="inclusive")
        pct = rng.randint(1, 99)
        assert abs(E.q_incl(xs, pct) - qs[pct - 1]) < 1e-12, (n, pct)
        assert abs(E.q_incl(xs, 50) - statistics.median(xs)) < 1e-12
    fin = [0.3 + 0.001 * i for i in range(200)]
    assert E.q_incl(fin[:199] + [math.inf], 99) == (fin[197] * 99 + fin[198]) / 100   # 1 lost of 200: finite
    assert E.q_incl(fin[:198] + [math.inf] * 2, 99) == math.inf                         # 2 lost of 200: inf
    assert E.q_incl([0.4], 99) == 0.4 and math.isnan(E.q_incl([], 50)) and E.q_incl([math.inf] * 3, 50) == math.inf
    print("CHECK 1 OK: q_incl == statistics.quantiles(inclusive) on 3000 random samples (n 2-300, any pct); "
          "with 200 probes 1 lost ping (+inf) leaves p99 finite, 2 lost make it +inf; n=1, empty, all-inf handled")

    PH = "PING 10.10.0.1 (10.10.0.1): 56 data bytes\n"
    ok_txt = PH + "".join(f"64 bytes from 10.10.0.1: seq={s} ttl=42 time=0.{300 + s:03d} ms\n" for s in range(5)) + \
        "\n--- 10.10.0.1 ping statistics ---\n5 packets transmitted, 5 packets received, 0% packet loss\n" \
        "round-trip min/avg/max = 0.300/0.302/0.304 ms\n"
    got, sent = E.parse_ping(ok_txt, 5)
    assert sent == 5 and got == {0: 0.3, 1: 0.301, 2: 0.302, 3: 0.303, 4: 0.304}
    gap = ok_txt.replace("seq=1 ttl=42 time=0.301 ms\n", "").replace("seq=3 ", "seq=1 ")
    gap = gap.replace("5 packets received", "4 packets received")
    got, sent = E.parse_ping(gap, 5)
    assert sent == 5 and sorted(got) == [0, 1, 2, 4] and got[1] == 0.303, got          # seq 3 missing (1 dup'ed)
    dup = ok_txt.replace("seq=2 ttl=42 time=0.302 ms\n", "seq=2 ttl=42 time=0.302 ms\n64 bytes from 10.10.0.1: "
                                                         "seq=2 ttl=42 time=9.000 ms (DUP!)\n")
    assert E.parse_ping(dup, 5)[0][2] == 0.302, "a DUP reply must not replace the first"
    killed = PH + "64 bytes from 10.10.0.1: seq=0 ttl=42 time=0.500 ms\n"                # no summary
    assert E.parse_ping(killed, 200) == ({0: 0.5}, 200)
    assert E.parse_ping("", 200) == ({}, 200)                                             # ssh failed
    short = ok_txt.replace("5 packets transmitted", "3 packets transmitted")              # deadline hit
    assert E.parse_ping(short, 5) == ({0: 0.3, 1: 0.301, 2: 0.302}, 3)
    p50, p99, loss, p99s = E.rtt_stats({s: 0.3 for s in range(198)}, 200)
    assert (p50, p99, loss, p99s) == (0.3, 0.3, 1.0, math.inf)
    p50, p99, loss, p99s = E.rtt_stats({}, 200)
    assert math.isnan(p50) and math.isnan(p99) and loss == 100.0 and p99s == math.inf
    assert E.rtt_thresh(0.5) == 2.0 and E.rtt_thresh(1.5) == 3.0                     # max(2 x ref, 2.0 ms)
    assert E.rtt_ok(2.0, 0.5) == 1.0 and E.rtt_ok(2.0 + 1e-9, 0.5) == 0.0           # 2 ms floor
    assert E.rtt_ok(3.0, 1.5) == 1.0 and E.rtt_ok(3.0 + 1e-9, 1.5) == 0.0           # 2 x ref above the floor
    assert E.rtt_ok(0.3, 0.5, 1.0) == 1.0 and E.rtt_ok(0.3, 0.5, 1.0 + 1e-9) == 0.0  # loss <= 1 % passes
    assert E.rtt_ok(0.3, 0.5, 0.5) == 1.0 and E.rtt_ok(2.5, 0.5, 0.0) == 0.0         # both terms needed
    assert E.rtt_ok(math.inf, 0.5) == 0.0 and E.rtt_ok(nan, 0.5, 100.0) == 0.0 and math.isnan(E.rtt_ok(0.3, nan))
    pt = E.ping_ticks([1.0, 1.25, 1.5, 1.75], 0.5, {0: 0.3, 1: 0.9, 3: 0.4}, 5)   # sends at 0.5 0.7 0.9 1.1 1.3
    assert pt[0] == (0.9, 1) and pt[1] == (0.4, 0), pt                             # seq 2 lost in row 0
    assert math.isnan(pt[2][0]) and pt[2][1] == 1 and math.isnan(pt[3][0]) and pt[3][1] == 0, pt
    assert E.ping_ticks([], 0.5, {}, 3) == []
    print("CHECK 2 OK: busybox ping parser: clean run, missing seq, DUP ignored, killed (no summary -> 200 "
          "expected), empty output (all lost), deadline (transmitted < expected); rtt_stats p50/p99 over replies, "
          "loss %; rtt_ok: p99 == max(2 x ref, 2.0 ms) passes (floor and 2 x ref branch), above fails; loss == 1 % "
          "passes, above fails; inf / no pings fail, no reference -> nan; per-tick max RTT and lost count by send time")

    def jfile(gb, secs):
        f = tempfile.TemporaryFile("w+")
        f.write(json.dumps({"end": {"sum_received": {"bytes": int(gb * 1e9 / 8 * secs), "bits_per_second": 1}}}))
        return f
    fs = [jfile(6.0, 2) for _ in range(7)]
    assert abs(E.iperf_sum_files(fs, 28) - 3.0) < 1e-6
    one = jfile(6.0, 28)
    assert E.iperf_sum_files([one], 28) == E.iperf_sum(one, 28) == exp40.iperf_sum(one, 28)
    bad = tempfile.TemporaryFile("w+"); bad.write("iperf3: error - the server is busy")
    assert math.isnan(E.iperf_sum_files(fs[:6] + [bad], 28)) and math.isnan(E.iperf_sum_files([], 28))
    udp = tempfile.TemporaryFile("w+")
    udp.write(json.dumps({"end": {"sum": {"bytes": 1, "lost_percent": 0}, "sum_sent": {"bytes": 750000000},
                                  "sum_received": {"bytes": 600000000, "bits_per_second": 1}}}))
    assert abs(E.iperf_sum(udp, 1) - 4.8) < 1e-9, "UDP: the receiver's bytes (iperf3 >= 3.13 sum_received)"
    assert E.burst_starts("C") == [12, 16, 20, 24, 28, 32, 36] and E.burst_starts("C")[-1] + E.BURST_ON <= 40
    print("CHECK 3 OK: iperf_sum_files: 7 C bursts of 2 s at 6 Gbit/s -> 3.0 Gbit/s over the 28 s envelope; one "
          "file == exp40's iperf_sum; one failed burst -> nan; UDP uses sum_received; bursts 12..36 s end by 38 s")

    for _ in range(400):
        n = rng.randint(2, 12)
        ds = [rng.gauss(rng.uniform(-0.05, 0.05), rng.uniform(0.001, 0.1)) for _ in range(n)]
        ref = scipy.stats.ttest_rel([x + 1.0 for x in ds], [1.0] * n).pvalue
        assert abs(E.paired_p(ds) - ref) < 1e-9 * max(1.0, ref) + 1e-12, (ds, E.paired_p(ds), ref)
    xs6 = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
    assert abs(E.paired_p(xs6) - scipy.stats.ttest_1samp(xs6, 0).pvalue) < 1e-12
    assert E.paired_p([0.0] * 6) == 1.0 and E.paired_p([0.1] * 6) == 0.0 and math.isnan(E.paired_p([0.3]))
    assert abs(E.betainc(2.5, 0.5, 5 / (5 + 2.570582 ** 2)) - 0.05) < 1e-6   # t = 2.5706, df 5 -> p = 0.05
    print("CHECK 4 OK: paired_p == scipy.stats.ttest_rel p-value on 400 random samples (n 2-12, rel. err < 1e-9); "
          "t = 2.5706 with df 5 -> p = 0.050; all differences 0 -> 1.0, constant non-zero -> 0.0, n = 1 -> nan")

    # ================= part 2: the real run() =================
    exp39.VFROT[REF_CFG[1]] = exp39.parse_vfrot(REF_CFG[1])
    for p in ("79_4", "85_4", "90_4"):
        exp39.LQR[p] = (*exp39.parse_lqr(p), exp39.lqr_gain(exp39.parse_lqr(p)[1]))
    exp40.setup_configs([REF_CFG, ("vffair", "0.8_1_2_0.94_F0.25")])
    n_eq = 0
    for old, cfgs in ((exp39, [REF_CFG, ("lqr", "79_4"), ("lqr", "85_4"), ("lqr", "90_4"), ("static", "2500"),
                               ("static", "3500"), ALONE]),
                      (exp40, [REF_CFG, ("vffair", "0.8_1_2_0.94_F0.25"), ALONE])):
        for c in cfgs:
            for seed, fac in ((1, None), (2, {"V1": 1.0, "V2": 0.90}), (3, {"V1": 0.96, "V2": 0.93})):
                ro, tso, evo = sim_run(old, *c, Sim(seed, fac))
                with old_traffic():
                    rn, tsn, evn = sim_run(E, *c, Sim(seed, fac), REFS_OLD)
                assert len(tso) == len(tsn) > 150, (c, len(tso), len(tsn))
                assert all(same(a, b) for x, y in zip(tso, tsn) for a, b in zip(x, y[:len(x)])), f"{c}: ts differ"
                assert [e for e in evo if e[0] == "set"] == [e for e in evn if e[0] == "set"], f"{c}: cap writes differ"
                assert len(ro) <= E.R_N and all(same(a, b) for a, b in zip(ro, rn)), f"{c}: results differ"
                n_eq += 1
    print(f"CHECK 5 OK: controllers unchanged: with exp39's traffic, exp41.run() == REAL exp39.run() for vfrot, lqr "
          f"79_4/85_4/90_4, static 2500/3500, alone and == REAL exp40.run() for vfrot, vffair F0.25, alone, tick by "
          f"tick (every ts row, every cap write with its time, every result column of the old file), for victims "
          f"1.00/1.00, 1.00/0.90, 0.96/0.93 ({n_eq} runs)")

    # new traffic
    res = {c: sim_run(E, *c, Sim(4)) for c in CONFIGS + [ALONE]}
    for c in CONFIGS + [ALONE]:
        ev = [e for e in res[c][2] if e[0] == "ssh"]
        fl = sorted(e[2] for e in ev if e[1] in F)
        assert fl == ([] if c == ALONE else sorted(x for cmds in E.flooder_cmds().values() for x in cmds)), (c, fl)
        assert [e[2] for e in ev if e[1] in E.VICTIMS] == list(E.victim_cmds(40).values()) + [E.ping_cmd(40)]
        assert all(e[3] == ev[0][3] for e in ev if e[1] in F), "all flooder commands launched at slot start"
    r, ts, _ = res[REF_CFG]
    off = {g: r[rcol(f"flooder{g}_offered_gbps")] for g in F}
    assert abs(off["A"] - 6.0) < 0.05 and abs(off["B"] - 6.0) < 0.05 and abs(off["C"] - 3.0) < 0.05 \
        and abs(off["D"] - 6.0) < 0.05, off
    rs, _, _ = res[("static", "2500")]
    assert abs(rs[rcol("flooderB_offered_gbps")] - 6.0) < 0.05 and abs(rs[rcol("flooderB_ebpf_gbps")] - 2.5) < 0.05
    assert abs(rs[rcol("flooderC_iperf_gbps")] - 3.0) < 1e-6 and abs(rs[rcol("flooderC_ebpf_gbps")] - 1.25) < 0.05
    capC, rateC = tcol("capC_mbit"), tcol("rateC_gbps")
    on = [row for row in ts if any(s + 0.3 < row[0] < s + 1.9 for s in E.burst_starts("C"))]
    offr = [row for row in ts if any(s + 2.3 < row[0] < s + 3.9 for s in E.burst_starts("C"))]
    assert on and offr and all(row[rateC] > 0.15 for row in on) and all(row[rateC] == 0.0 for row in offr)
    assert all(row[capC] == E.CAP_MIN for row in offr if row[0] > 14.5), "an inactive C never holds the high cap"
    ho = [row[0] for row in ts if 18 < row[0] < 40 and row[capC] > E.CAP_MIN]
    assert ho, "C gets the high cap during its bursts"
    for c in CONFIGS:
        rr = res[c][0]
        assert abs(rr[rcol("flooder_total_gbps")] - sum(rr[rcol(f"flooder{g}_ebpf_gbps")] for g in F)) < 1e-9
    print(f"CHECK 6 OK: new traffic in the real run(): every config launches the 10 flooder commands at slot start "
          f"(alone none) and V1, V2, ping right after; offered (eBPF passed + dropped) A/B/C/D = "
          + "/".join(f"{off[g]:.2f}" for g in F) + " Gbit/s over the own windows (C 7 x 2 s of 6G over 28 s); static "
          f"2500: B offered {rs[rcol('flooderB_offered_gbps')]:.2f} but passed {rs[rcol('flooderB_ebpf_gbps')]:.2f} "
          f"(UDP not reduced by the cap), C iperf 3.00 / eBPF {rs[rcol('flooderC_ebpf_gbps')]:.2f}; vfrot: C's rate "
          f"is > 0 in every burst and 0 between bursts, where its cap is {E.CAP_MIN:g} (inactive, never holder), and "
          f"it holds the high cap in {len(ho)} burst ticks")

    lossy = lambda s: None if s in (10, 11) else (5.0 if s == 50 else 0.30 + 0.01 * (s % 7))
    r2, ts2, _ = sim_run(E, *REF_CFG, Sim(4, ping=lossy))
    assert r2[rcol("v2_loss_pct")] == 1.0 and r2[rcol("slo_ok_v2_rtt")] == 1.0   # 2 of 200 lost = 1 %: tolerated
    assert r2[rcol("v2_rtt_p99_ms")] < 0.5, "the single 5 ms spike is above p99 of 198 replies"
    assert r2[rcol("slo_ok_worst_with_rtt")] == min(r2[rcol("slo_ok_v1")], r2[rcol("slo_ok_v2")])
    assert r2[rcol("slo_ok_worst")] == r[rcol("slo_ok_worst")]
    assert sum(row[-1] for row in ts2) == 2 and max(row[-2] for row in ts2 if row[-2] == row[-2]) == 5.0
    t50 = [row[0] for row in ts2 if row[-2] == 5.0][0]
    assert abs(t50 - (E.OFFSET + 50 * E.PING_I)) < 0.3, t50
    one_lost = lambda s: None if s == 10 else 0.30 + 0.01 * (s % 7)
    r3, _, _ = sim_run(E, *REF_CFG, Sim(4, ping=one_lost))
    assert r3[rcol("v2_loss_pct")] == 0.5 and r3[rcol("slo_ok_v2_rtt")] == 1.0
    assert r3[rcol("slo_ok_worst_with_rtt")] == min(r3[rcol("slo_ok_v1")], r3[rcol("slo_ok_v2")])
    three_lost = lambda s: None if s in (10, 11, 12) else 0.30
    r3b, _, _ = sim_run(E, *REF_CFG, Sim(4, ping=three_lost))   # 3 of 200 = 1.5 % > 1 %: fail, p99 still 0.30
    assert r3b[rcol("v2_loss_pct")] == 1.5 and r3b[rcol("v2_rtt_p99_ms")] == 0.3 and r3b[rcol("slo_ok_v2_rtt")] == 0.0
    assert r3b[rcol("slo_ok_worst_with_rtt")] == 0.0
    spike = lambda s: 1.2 if s < 3 else 0.30
    r4, _, _ = sim_run(E, *REF_CFG, Sim(4, ping=spike))   # 3 of 200 at 1.2 ms > 2 x 0.5 but <= 2.0 ms floor -> pass
    assert r4[rcol("v2_loss_pct")] == 0.0 and r4[rcol("v2_rtt_p99_ms")] == 1.2 and r4[rcol("slo_ok_v2_rtt")] == 1.0
    spike2 = lambda s: 2.5 if s < 3 else 0.30
    r4b, _, _ = sim_run(E, *REF_CFG, Sim(4, ping=spike2))   # 3 of 200 at 2.5 ms > max(1.0, 2.0) -> fail
    assert r4b[rcol("v2_loss_pct")] == 0.0 and r4b[rcol("v2_rtt_p99_ms")] == 2.5 and r4b[rcol("slo_ok_v2_rtt")] == 0.0
    r5, _, _ = sim_run(E, *REF_CFG, Sim(4, vfac={"V1": 0.9}))   # throughput SLO missed, RTT fine
    assert r5[rcol("slo_ok_v2_rtt")] == 1.0 and r5[rcol("slo_ok_worst_with_rtt")] == r5[rcol("slo_ok_worst")] < 0.5
    r6, _, _ = sim_run(E, *REF_CFG, Sim(4, ping="empty"))
    assert r6[rcol("v2_loss_pct")] == 100.0 and r6[rcol("slo_ok_v2_rtt")] == 0.0 and math.isnan(r6[rcol("v2_rtt_p99_ms")])
    ra = res[ALONE][0]
    assert ra[rcol("slo_ok_v2_rtt")] == 1.0 and ra[rcol("flooder_total_gbps")] == 0.0
    r7, _, _ = sim_run(E, *REF_CFG, Sim(4, fail=[("C", "sleep 24;")]))
    assert math.isnan(r7[rcol("flooderC_iperf_gbps")]) and not math.isnan(r7[rcol("flooderC_ebpf_gbps")])
    r8, _, _ = sim_run(E, *REF_CFG, Sim(4, read_cost=0.06))
    assert r8[rcol("tick_mean_s")] > E.TICK_WARN_S
    print(f"CHECK 7 OK: V2 latency in the real run(): seq 10+11 lost and one 5 ms spike -> loss 1.0 % (tolerated), "
          f"p99 {r2[rcol('v2_rtt_p99_ms')]:.3f} ms, rtt_ok 1 (ok_worst unchanged), 2 lost in the ts lost column, the "
          f"spike in the tick at {t50:.2f} s (= 0.5 + 50 x 0.2); 1 lost -> rtt_ok 1 and ok_w_rtt = min(ok_v1, ok_v2); "
          f"3 lost (1.5 %) -> rtt_ok 0, ok_w_rtt 0; 3 of 200 at 1.2 ms -> p99 1.2 <= max(2 x 0.5, 2.0) -> 1; at 2.5 ms "
          f"-> 0; V1 at 90 % -> ok_w_rtt = ok_worst = {r5[rcol('slo_ok_worst')]:.2f}; "
          f"no ping output -> loss 100 %, rtt_ok 0; alone: ping runs, rtt_ok 1; a failed C burst -> C iperf nan, "
          f"eBPF kept; 60 ms counter reads -> mean tick {r8[rcol('tick_mean_s')]:.3f} s > 0.30")

    tmp = tempfile.mkdtemp(prefix="test_exp41_ref_")
    redirect_outputs(tmp)
    sim = Sim(5, ping=lambda s: 0.25 + 0.002 * s)
    with patched(E, sim):
        fake_if(E)
        with contextlib.redirect_stdout(io.StringIO()):
            refs, base, rref = E.reference(1, [])
    rt = [0.25 + 0.002 * s for s in range(100)] * 2
    assert refs == {"V1": 9.0, "V2": 2.0} and rref["n"] == 200 and rref["loss"] == 0.0
    assert abs(rref["p99"] - E.q_incl(rt, 99)) < 1e-9 and abs(rref["p50"] - statistics.median(rt)) < 1e-9
    assert [e[2] for e in sim.events if e[0] == "ssh"][:3] == list(E.victim_cmds(20).values()) + [E.ping_cmd(20)]
    lines = open(E.REF_FILE).read().splitlines()
    assert len(lines) == 3 and lines[2].split(", ")[2] == "V2" and lines[2].split(", ")[10] == f"{rref['p99']:.3f}"
    assert len(lines[2].split(", ")[13].split()) == 200 and lines[1].split(", ")[9:] == ["na"] * 5
    for pm, w in ((lambda s: None if s % 10 == 0 else 0.3, "loss 10.0%"), ("empty", "0 replies")):
        redirect_outputs(tempfile.mkdtemp(prefix="test_exp41_ref_"))
        with patched(E, Sim(5, ping=pm)):
            fake_if(E)
            expect_exit(lambda: E.reference(1, []), "reference ping failed", w)
    print(f"CHECK 8 OK: reference(): 2 x 20 s with ping -c 100 each, V1/V2 refs 9.0/2.0, RTT p50/p99 "
          f"{rref['p50']:.3f}/{rref['p99']:.3f} ms over the 200 pooled replies (== q_incl), ref_victim41.txt has "
          f"the RTT fields and all 200 samples on the V2 line; 10 % loss or no ping output -> refuses")

    # ================= part 3: main(), summary, refusals =================
    calls, log, tmp, sleeps = run_main_dry()
    seqs = [[c for r, c, _, _ in calls if r == rep] for rep in range(1, REPS + 1)]
    print("\n--- exp41.log header lines (as main() prints them) ---")
    print("\n".join(l for l in log.splitlines() if l.startswith(("SCENARIO", "BURSTS", "CONFIGS", "REFERENCE CONFIG",
                                                                   "VICTIMS", "LATENCY", "PING CMD", "FLOODER CMD",
                                                                   "METRICS", "EXPECTED"))))
    print("\nconfig order per repeat (slot -> config):")
    w = max(len(name(c)) for c in CONFIGS + [ALONE])
    print("slot " + " ".join(f"{'r' + str(r):{w}s}" for r in range(1, REPS + 1)))
    for slot in range(n_slots):
        print(f"{slot:4d} " + " ".join(f"{name(seq[slot]):{w}s}" for seq in seqs))
    random.seed(7)
    for rep, seq in enumerate(seqs, 1):
        assert len(seq) == 15 and sorted(c for c in seq if c != ALONE) == sorted(CONFIGS), rep
        assert seq[0::2] == [ALONE] * 8 and all(c != ALONE for c in seq[1::2]), rep
        order = CONFIGS[:]; random.shuffle(order)
        assert seq == [ALONE] + [x for c in order for x in (c, ALONE)], f"repeat {rep}: order differs"
    assert all(c[3] == RREF for c in calls), "every run gets its repeat's RTT reference"
    assert [s for s in sleeps if s == 600] == [600] * (REPS - 1) and "cool-down 45 s" not in log
    rows = open(E.CSV).read().splitlines()
    assert rows[0] == E.HEADER and len(rows) == 1 + REPS * 15
    assert all(len(r.split(",")) == len(rows[0].split(",")) for r in rows)
    tsr = open(E.TS_CSV).read().splitlines()
    assert tsr[0] == E.TS_HEADER and all(len(r.split(",")) == len(tsr[0].split(",")) for r in tsr)
    assert f"EXPECTED DURATION: 15 slots/repeat, ~{per_rep / 60:.1f} min/repeat" in log
    assert len([l for l in log.splitlines() if l.startswith("HOLDER rep")]) == REPS * 2
    assert len([l for l in log.splitlines() if l.startswith("LQR TARGETS rep")]) == REPS
    assert "WARNINGS (mean tick > 0.3 s, steal mean > 1% or max > 5%, missing flooder iperf3 report): 0" in log
    assert "SUMMARY41 from results_dyn41.csv" in log and "PAIRED vs vfrot 0.8_1_2_0.94" in log
    assert "ALONE controls: 48 slots, RTT SLO passed in 48/48" in log
    print(f"\nCHECK 9 OK: 15 slots per repeat, alone at 0,2,...,14, each of the 7 configs once, order == an "
          f"independent random.seed(7) shuffle; every run gets the repeat's RTT reference; {REPS - 1} x 600 s rests; "
          f"CSV/ts widths match their headers, 1 + 6 x 15 rows; HOLDER lines for vfrot/vffair, LQR TARGETS per "
          f"repeat, SUMMARY41 + PAIRED + ALONE lines at the end of the log")
    print("  CSV header: " + rows[0])

    _, log_w, _, _ = run_main_dry(slow={("lqr", "90_4")}, steal={("static", "3500")},
                                  results={(2, ("vffair", "0.8_1_2_0.94_F0.25")): fake_result(flooderC_iperf_gbps=nan)})
    wl = [l for l in log_w.splitlines() if l.startswith("WARNING: rep")]
    assert sum("lqr 90_4: mean control tick 0.310 s > 0.3 s" in l for l in wl) == REPS
    assert sum("static 3500: steal mean 1.50% / max 2.00%" in l for l in wl) == REPS
    assert [l for l in wl if "iperf3 report missing" in l] == [
        "WARNING: rep 2 slot " + l.split(" slot ")[1].split()[0] + " vffair 0.8_1_2_0.94_F0.25: iperf3 report missing "
        "or unparsable for flooder(s) C (eBPF columns unaffected)" for l in wl if "iperf3 report missing" in l]
    assert len(wl) == 2 * REPS + 1 and f"missing flooder iperf3 report): {2 * REPS + 1}" in log_w
    print(f"CHECK 10 OK: tick > 0.3 s, steal > 1 % and a missing flooder iperf3 report each give a WARNING line and "
          f"are counted at the end ({len(wl)})")

    known = {}
    okr, okv = [0.90, 1.00, 0.95, 1.0, 1.0, 1.0], [1.00, 1.00, 0.90, 1.0, 1.0, 1.0]
    for rep in range(1, REPS + 1):
        known[(rep, REF_CFG)] = fake_result(slo_ok_worst=okr[rep - 1], slo_ok_worst_with_rtt=okr[rep - 1],
                                            jain_fairness=0.6, flooder_total_gbps=18.0, v2_rtt_p99_ms=1.0)
        known[(rep, ("vffair", "0.8_1_2_0.94_F0.25"))] = fake_result(
            slo_ok_worst=okv[rep - 1], slo_ok_worst_with_rtt=okv[rep - 1] * (rep != 4), slo_ok_v2_rtt=float(rep != 4),
            jain_fairness=0.6 + 0.01 * rep, flooder_total_gbps=17.0, v2_rtt_p99_ms=1.0 - 0.1 * (rep % 2),
            v2_rtt_p50_ms=0.4 + 0.05 * rep, v2_loss_pct=0.5 * (rep == 2))
        known[(rep, ("lqr", "79_4"))] = fake_result(v2_rtt_p99_ms=0.9 + 0.2 * rep)
    known[(3, ALONE)] = fake_result(slo_ok_v2_rtt=0.0)
    _, log_s, _, _ = run_main_dry(results=known)
    order, stats, diffs, alone = E.summary_data(E.CSV)
    assert order == CONFIGS
    dv = diffs[("vffair", "0.8_1_2_0.94_F0.25")]
    okd = [b - a for a, b in zip(okr, okv)]
    assert dv["slo_ok_worst"][3] == (1, 4, 1) and abs(dv["slo_ok_worst"][0] - statistics.mean(okd)) < 1e-9
    assert abs(dv["slo_ok_worst"][4] - scipy.stats.ttest_rel(okv, okr).pvalue) < 1e-9
    owr = [b - a for a, b in zip(okr, [okv[i] * (i != 3) for i in range(6)])]
    assert dv["slo_ok_worst_with_rtt"][3] == (1, 3, 2) and abs(dv["slo_ok_worst_with_rtt"][0] - statistics.mean(owr)) < 1e-9
    assert dv["slo_ok_v2_rtt"][3] == (0, 5, 1)
    assert dv["jain_fairness"][3] == (6, 0, 0) and abs(dv["jain_fairness"][0] - 0.035) < 1e-9
    assert dv["flooder_total_gbps"][3] == (0, 0, 6) and dv["flooder_total_gbps"][4] == 0.0 and dv["flooder_total_gbps"][5]
    assert dv["v2_rtt_p99_ms"][3] == (3, 3, 0), "rtt_p99: lower is better"
    p50v = [0.4 + 0.05 * r for r in range(1, 7)]; lossv = [0.5 * (r == 2) for r in range(1, 7)]
    sv = stats[("vffair", "0.8_1_2_0.94_F0.25")]
    assert abs(sv["v2_rtt_p50_ms"][0] - statistics.mean(p50v)) < 1e-9 and abs(sv["v2_rtt_p50_ms"][1] - statistics.stdev(p50v)) < 1e-9
    assert abs(sv["v2_loss_pct"][0] - statistics.mean(lossv)) < 1e-9 and abs(sv["v2_loss_pct"][1] - statistics.stdev(lossv)) < 1e-9
    assert stats[REF_CFG]["v2_rtt_p50_ms"][:2] == (0.4, 0.0) and stats[REF_CFG]["v2_loss_pct"][:2] == (0.0, 0.0)
    assert dv["v2_rtt_p50_ms"][3] == (0, 0, 6) and dv["v2_loss_pct"][3] == (0, 5, 1), "p50 / loss: lower is better"
    dl = diffs[("lqr", "79_4")]["v2_rtt_p99_ms"]
    assert dl[3] == (0, 0, 6) and abs(dl[0] - statistics.mean([0.9 + 0.2 * r - 1.0 for r in range(1, 7)])) < 1e-9
    assert len(alone) == REPS * 8 and sum(a["slo_ok_v2_rtt"] for a in alone) == REPS * 8 - 8   # all 8 alone slots of rep 3
    off = subprocess.run([sys.executable, "-B", os.path.join(HERE, "exp41.py"), "--summary", E.CSV],
                         capture_output=True, text=True)
    assert off.returncode == 0 and off.stdout.strip() == E.summarize(E.CSV) and off.stdout.strip() in log_s, off.stderr
    assert "ALONE controls: 48 slots, RTT SLO passed in 40/48 (false-alarm rate of the RTT SLO with no flooders 0.167), " \
        "rtt_p50 0.400 +- 0.000 ms, rtt_p99 0.800 +- 0.000 ms, loss 0.00 +- 0.00 %" in off.stdout
    hdr = [l for l in off.stdout.splitlines() if l.startswith("mode")][0].split()
    assert hdr[-3:] == ["rtt_p50", "rtt_p99", "loss_pct"], hdr
    vl = [l for l in off.stdout.splitlines() if l.startswith("vffair") and " +- " in l][0]
    assert f"{statistics.mean(p50v):6.3f} +- {statistics.stdev(p50v):5.3f}" in vl, vl
    print("CHECK 11 OK: summary with known values: vffair vs vfrot ok_worst W/T/L 1/4/1 with p == scipy ttest_rel; "
          "ok_w_rtt 1/3/2 (the RTT failure in rep 4 zeroes it); rtt_ok 0/5/1; Jain +0.035 6/0/0; flooder total "
          "-1.0 0/0/6 p=0 (sd0); rtt_p99 lower-is-better 3/3/0 (lqr 79_4: 0/0/6); rtt_p50 and loss_pct mean +- sd "
          "per config and lower-is-better W/T/L (0/0/6, 0/5/1); alone false-alarm 8/48 (rep 3); the "
          "offline 'python3 exp41.py --summary FILE' prints exactly the block at the end of the log:")
    print("\n".join("  " + l for l in off.stdout.splitlines()))

    for fake in ((4, 4), (None, 6), (8, 8)):
        d = tempfile.mkdtemp(prefix="test_exp41_np_")
        redirect_outputs(d)
        real_hc, real_sudo = E.host_cpus, E.sudo
        E.host_cpus = lambda _f=fake: _f
        E.sudo = lambda args: (_ for _ in ()).throw(AssertionError("sudo called before the nproc guard"))
        try:
            expect_exit(E.main, f"REFUSING TO START: nproc = {fake[0]} (os.cpu_count() = {fake[1]}), exp41 requires "
                                "exactly 6")
        finally:
            E.host_cpus, E.sudo = real_hc, real_sudo
        assert os.listdir(d) == []
    for f in OUTS:
        d = tempfile.mkdtemp(prefix="test_exp41_ow_")
        redirect_outputs(d)
        open(os.path.join(d, f), "w").write("KEEP\n")
        expect_exit(E.main, "REFUSING TO START", f)
        assert open(os.path.join(d, f)).read() == "KEEP\n" and os.listdir(d) == [f]
        shutil.copy(os.path.join(HERE, "run41.sh"), d)
        p = subprocess.run(["sh", os.path.join(d, "run41.sh")], capture_output=True, text=True)
        assert p.returncode == 1 and p.stdout.strip() == (f"REFUSING TO START: {f} already exists (move or delete "
                                                          "it first; exp41 never appends)"), (p.returncode, p.stdout)
        assert open(os.path.join(d, f)).read() == "KEEP\n"
    real_gh = E.guest_has
    E.guest_has = lambda ip, tools: not (ip == "10.10.0.15" and "ping" in tools)
    try:
        expect_exit(E.check_guests, "tenant5 (10.10.0.15) needs iperf3+ping")
    finally:
        E.guest_has = real_gh
    print("CHECK 12 OK: main() refuses with nproc 4 / unknown / 8 before sudo and before creating any file; with "
          "any one of " + ", ".join(OUTS) + " present main() and run41.sh refuse and leave it unchanged; "
          "check_guests refuses when tenant5 has no ping")

    full = [5201, 5202, 5203, 5204, 5205, 5206]
    for script, plan_start, mark_ok in (("run41.sh", "planned: SCENARIO steady4 mixed traffic, 6 repeats x 15 slots "
                                         "(7 configs + 8 alone)", "sudo\n"),
                                        ("pilot41.sh", "planned: pilot41 4 phases x 20 s", "sudo\n")):
        d = tempfile.mkdtemp(prefix="test_exp41_sh_")
        for f in (script, "exp41.py", "pilot41.py"):
            shutil.copy(os.path.join(HERE, f), d)
        bindir = os.path.join(d, "bin"); os.mkdir(bindir)
        env = dict(os.environ, PATH=bindir + ":" + os.environ["PATH"])
        fake_tools(bindir, d, full[:5], 6, False)
        p = subprocess.run(["sh", os.path.join(d, script)], capture_output=True, text=True, env=env)
        assert p.returncode == 1 and p.stdout.strip() == ("host CPUs: 6\nno iperf3 server listening on host port 5206; "
                                                          "start it first: iperf3 -s -D -p 5206"), (p.stdout, p.stderr)
        assert not os.path.exists(os.path.join(d, "MARK"))
        fake_tools(bindir, d, full, 4, False)
        p = subprocess.run(["sh", os.path.join(d, script)], capture_output=True, text=True, env=env)
        assert p.returncode == 1 and p.stdout.startswith("REFUSING TO START: nproc = 4"), p.stdout
        assert not os.path.exists(os.path.join(d, "MARK"))
        fake_tools(bindir, d, full, 6, True)
        p = subprocess.run(["sh", os.path.join(d, script), "6"], capture_output=True, text=True, env=env)
        plan = p.stdout.strip().splitlines()
        assert p.returncode == 1 and plan[0] == "host CPUs: 6" and plan[1].startswith(plan_start), (p.stdout, p.stderr)
        assert open(os.path.join(d, "MARK")).read() == mark_ok, "must stop at sudo -v (fake, fails) before starting"
        assert not any(os.path.exists(os.path.join(d, f)) for f in OUTS + POUTS)
        if script == "pilot41.sh":
            pilot_plan = plan[1]
            for f in POUTS:
                open(os.path.join(d, f), "w").write("KEEP\n")
                p = subprocess.run(["sh", os.path.join(d, script)], capture_output=True, text=True, env=env)
                assert p.returncode == 1 and p.stdout.strip() == (f"REFUSING TO START: {f} already exists (move or "
                                                                  "delete it first; pilot41 never appends)"), p.stdout
                assert open(os.path.join(d, f)).read() == "KEEP\n"
                os.remove(os.path.join(d, f))
        else:
            run_plan = plan[1]
    print(f"CHECK 13 OK: run41.sh and pilot41.sh: port 5206 missing -> exit before python3/sudo; nproc 4 -> refuse; "
          f"all checks pass -> plan line, then sudo -v (fake, failing) and stop before nohup/the pilot; pilot41.sh "
          f"refuses with any of {', '.join(POUTS)} present:\n  {run_plan}\n  {pilot_plan}")

    # ---------------- pilot41 ----------------
    assert pilot41.grade_tick(0.25, 0.3) == "PASS" and pilot41.grade_tick(0.31, 0.4) == "WARN"
    assert pilot41.grade_tick(0.25, 1.2) == "WARN" and pilot41.grade_tick(0.36, 0.4) == "FAIL"
    assert pilot41.grade_offered(5.5, True) == "PASS" and pilot41.grade_offered(5.3, True) == "WARN"
    assert pilot41.grade_offered(5.5, False) == "WARN" and pilot41.grade_offered(1.9, True) == "FAIL"
    assert pilot41.grade_cap(2600, 6000, 2500) == "PASS" and pilot41.grade_cap(2700, 6000, 2500) == "WARN"
    assert pilot41.grade_cap(2900, 6000, 2500) == "FAIL" and pilot41.grade_cap(2000, 6000, 2500) == "WARN"
    assert pilot41.grade_cap(1500, 1600, 2500) == "PASS", "cap above the offered rate: passed ~ offered"
    assert pilot41.grade_rtt_base(100, 0) == "PASS" and pilot41.grade_rtt_base(99, 1) == "WARN"
    assert pilot41.grade_rtt_base(90, 10) == "FAIL" and pilot41.grade_rtt_base(0, 100) == "FAIL"
    assert pilot41.grade_rtt_flood(100, 0, 4.9, 0.5) == "PASS" and pilot41.grade_rtt_flood(100, 0, 5.1, 0.5) == "WARN"
    assert pilot41.grade_rtt_flood(90, 10, 0.5, 0.5) == "WARN" and pilot41.grade_rtt_flood(0, 100, nan, 0.5) == "FAIL"
    assert pilot41.grade_cpu(95.0) == "PASS" and pilot41.grade_cpu(95.1) == "WARN"
    assert pilot41.worst(["PASS", "WARN"]) == "WARN" and pilot41.worst(["WARN", "FAIL"]) == "FAIL"

    g_ok, res_ok, log_ok, tmp_ok, el_ok = run_pilot_sim(Sim(6))
    assert all(v == "PASS" for v in g_ok.values()) and "PILOT41 OVERALL: PASS" in log_ok, res_ok
    assert sorted(os.listdir(tmp_ok)) == sorted(POUTS[1:]), os.listdir(tmp_ok)
    pts = open(pilot41.TS).read().splitlines()
    assert len({l.split(",")[0] for l in pts[1:]}) == 4 and all(len(l.split(",")) == 11 for l in pts)
    vts = open(pilot41.VTS).read().splitlines()
    assert vts[0] == E.TS_HEADER and len(vts) > 150 and all(len(l.split(",")) == len(vts[0].split(",")) for l in vts)
    assert len(g_ok) == 13, g_ok
    g_slow, _, log_slow, _, _ = run_pilot_sim(Sim(6, udp_actual=3.0))
    assert g_slow["udp_offered"] == "WARN" and g_slow["udp_cap 2500"] == "PASS" and "OVERALL: WARN" in log_slow
    g_leak, _, log_leak, _, _ = run_pilot_sim(Sim(6, cap_leak=True))
    assert g_leak["udp_cap 2500"] == "FAIL" and g_leak["udp_cap 200"] == "FAIL" and "OVERALL: FAIL" in log_leak
    g_tick, _, _, _, _ = run_pilot_sim(Sim(6, read_cost=0.07))
    assert all(g_tick[f"tick {n}"] == "FAIL" for n, _ in pilot41.PHASES) and g_tick["vfrot_slot"] == "FAIL"
    g_np, _, log_np, _, _ = run_pilot_sim(Sim(6, ping="empty"))
    assert g_np["rtt_base"] == "FAIL" and g_np["vfrot_slot"] == "FAIL" and "P4 skipped" in log_np
    d = tempfile.mkdtemp(prefix="test_pilot41_ow_")
    pilot41.TS, pilot41.VTS = os.path.join(d, POUTS[1]), os.path.join(d, POUTS[2])
    open(pilot41.TS, "w").write("KEEP\n")
    expect_exit(pilot41.main, "REFUSING TO START", "pilot41_ts.csv")
    pilot41.TS = os.path.join(d, "none.csv")
    real_hc = E.host_cpus; E.host_cpus = lambda: (4, 4)
    try:
        expect_exit(pilot41.main, "REFUSING TO START: nproc = 4")
    finally:
        E.host_cpus = real_hc
    assert sorted(os.listdir(d)) == ["pilot41_ts.csv"] and open(os.path.join(d, POUTS[1])).read() == "KEEP\n"
    print("\n--- pilot41 checks in the healthy simulation ---")
    print("\n".join(l for l in log_ok.splitlines() if l[:4] in ("PASS", "WARN", "FAIL") or l.startswith("PILOT41")))
    print(f"CHECK 14 OK: pilot41 verdict thresholds; the REAL pilot41.main() on the simulated testbed: healthy -> "
          f"all 13 checks PASS, only pilot41_ts.csv (4 phases) and pilot41_vfrot_ts.csv (exp41 ts layout) written; "
          f"B offering 3 of 6 Gbit/s -> udp_offered WARN; a token bucket that lets UDP through -> udp_cap FAIL; "
          f"70 ms counter reads -> every tick check FAIL and the vfrot slot FAIL; no ping output -> rtt_base FAIL "
          f"and the slot skipped (FAIL); refuses if pilot41_ts.csv exists or nproc != 6. Simulated pilot time "
          f"{el_ok / 60:.2f} min (estimate {pilot41.expected_s() / 60:.2f} min)")

    assert sha(OLD_FILES) == before_old, "an exp39/exp40 file changed"
    assert sha(OUTS + POUTS) == before_new, "an exp41 / pilot41 output file appeared or changed in the testbed"
    print("CHECK 15 OK: sha256 of " + ", ".join(OLD_FILES) + " unchanged; no exp41 or pilot41 output in the testbed")
    print("ALL TESTS PASSED")
