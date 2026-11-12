#!/usr/bin/env python3
# Pure-python self-test for exp40.py: no VM, no sudo, no network, no traffic, no sleeping.
# exp40 = vfrot sensitivity study (BMAX 12000/20000, BETA 0.9, ADD 2, T 1) + vffair (floor 0.5 / 0.25) on exp39's
# scenario, 8 configs -> 17 slots per repeat.
# Part 1: the vffair cap logic as pure functions (property test, edge cases, holder decision identical to vfrot).
# Part 2: the REAL exp40.run() tick loop with a simulated clock, fake bpftool counters/caps and fake iperf3 (as
#         test_exp39), including a tick-by-tick comparison of exp40 vfrot 0.8_1_2_0.94 against the REAL exp39.run().
# Part 3: the REAL exp40.main() with every side effect stubbed (slot order, CSV/ts, warnings, summary), summarize()
#         on a synthetic CSV with known answers, the refusals of main() and run40.sh (fake tools first on PATH, so
#         the real sudo can never be reached), and a check that no exp39 file changed and no exp40 output appeared.
import sys
sys.dont_write_bytecode = True   # importing exp39/exp40 must not write anything into the testbed
import contextlib, hashlib, io, json, math, os, random, shutil, struct, subprocess, tempfile, time
sys.argv = sys.argv[:1]          # exp39/exp40 read REPS from argv: use the default (6)
import exp39, exp40

HERE = os.path.dirname(os.path.abspath(exp40.__file__))
REPS = 6
ALONE = ("alone", "na")
REFS = {"V1": 9.0, "V2": 9.0}
OUTS = ("results_dyn40.csv", "ts_dyn40.csv", "ref_victim40.txt", "exp40.log")
EXP39_FILES = ("exp39.py", "run39.sh", "test_exp39.py", "results_dyn39.csv", "ts_dyn39.csv", "ref_victim39.txt",
               "exp39.log")
FLOOD_GBIT = 6.0
F = exp40.FLOODERS
REF_CFG = ("vfrot", "0.8_1_2_0.94")


def sha(names):
    return {n: (hashlib.sha256(open(os.path.join(HERE, n), "rb").read()).hexdigest()
                if os.path.exists(os.path.join(HERE, n)) else None) for n in names}


def name(c):
    return f"{c[0]} {c[1]}"


def redirect_outputs(tmp):
    exp40.CSV, exp40.TS_CSV = os.path.join(tmp, OUTS[0]), os.path.join(tmp, OUTS[1])
    exp40.REF_FILE, exp40.LOG = os.path.join(tmp, OUTS[2]), os.path.join(tmp, OUTS[3])


def fake_result(tick_mean=0.25, steal=(0.0, 0.0), **over):
    # run() result tuple in RESULT_NAMES order; keyword arguments override single named fields
    v = dict.fromkeys(exp40.RESULT_NAMES, 1.0)
    v.update({"jain_fairness": 0.9, "cpu_mean": 80.0, "cpu_max": 90.0, "steal_mean": steal[0], "steal_max": steal[1],
              "tick_mean_s": tick_mean, "tick_max_s": tick_mean + 0.1, "budget_ceiling_share": 0.5,
              "budget_floor_share": 0.0, "flooder_total_gbps": 4.0})
    v.update(over)
    return tuple(v[n] for n in exp40.RESULT_NAMES)


def fake_ts_row(budget=16000.0):
    return ((20.0, 0.25, 0.01, 60.0, 0.0) + (9.0,) * exp40.NV + (9.0,) * exp40.NV
            + (budget - 600, 200.0, 200.0, 200.0) + (11.0, 0.2, 0.2, 0.2) + (budget, 0.99))


def run_main_dry(slow=(), steal=(), results=None):
    # slow: configs with mean tick 0.31 s; steal: configs with steal 1.5 % mean; results: (rep, config) -> tuple
    tmp = tempfile.mkdtemp(prefix="test_exp40_")
    redirect_outputs(tmp)
    exp40.OBJ = os.path.abspath(exp40.__file__)
    calls, rep_now, sleeps = [], [0], []
    saved = {k: getattr(exp40, k) for k in ("sudo", "check_servers", "check_guests", "vnet", "attach", "set_cap",
                                            "reference", "run", "host_cpus")}
    exp40.sudo = lambda args: ""
    exp40.host_cpus = lambda: (6, 6)
    exp40.check_servers = lambda: None
    exp40.check_guests = lambda: None
    exp40.vnet = lambda dom: "vnet-" + dom
    exp40.attach = lambda dev, cap: {"dev": dev, "prog": 0, "cfg": dev, "cnt": dev}
    exp40.set_cap = lambda d, mbps: None

    def fake_reference(rep, procs):
        rep_now[0] = rep
        return dict(REFS), 50.0

    def fake_run(mode, param, refs, base_cpu, procs):
        c = (mode, param)
        calls.append((rep_now[0], c, refs))
        if results and (rep_now[0], c) in results:
            return results[(rep_now[0], c)], [fake_ts_row()]
        return (fake_result(0.31 if c in slow else 0.25, (1.5, 2.0) if c in steal else (0.0, 0.0)),
                [fake_ts_row()])

    exp40.reference, exp40.run = fake_reference, fake_run
    real_sleep, time.sleep = time.sleep, lambda s: sleeps.append(s)
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            exp40.main()
    finally:
        time.sleep = real_sleep
        for k, v in saved.items():
            setattr(exp40, k, v)
    return calls, out.getvalue(), tmp, sleeps


def parse_cmd(cmd):
    w = cmd.split()
    delay = float(w[1].rstrip(";")) if w[0] == "sleep" else 0.0
    par = int(w[w.index("-P") + 1]) if "-P" in w else 1
    rate = float(w[w.index("-b") + 1].rstrip("G")) * par if "-b" in w else None
    return delay, float(w[w.index("-t") + 1]), rate, par


class FakeProc:
    def __init__(self, clock, end): self.clock, self.end = clock, end
    def poll(self): return 0 if self.clock[0] >= self.end else None
    def wait(self): self.clock[0] = max(self.clock[0], self.end); return 0
    def terminate(self): pass


def simulate_run(mod, mode, param, seed, vfac=None, read_cost=0.002):
    # the real mod.run() (mod = exp39 or exp40) against fake xdp counters / caps, fake iperf3 and a simulated clock;
    # victim v delivers vfac[v] x its -b rate; flooder g offers FLOOD_GBIT inside its window, passes min(offer, cap)
    rng = random.Random(seed)
    clock = [1000.0]
    vfac = vfac or {v: 1.0 for v in mod.VICTIMS}
    launches, events, capb, cum = {}, [], {}, {}
    cpu_acc = [0, 0, 0]
    real = {k: getattr(mod, k) for k in ("sudo", "counters", "cpu", "ssh")}
    real_time = (time.monotonic, time.sleep)

    def fake_sudo(args):
        assert args[:3] == ["bpftool", "map", "update"], args
        capb[args[4]] = struct.unpack("<Q", bytes.fromhex("".join(args[-8:])))[0]
        events.append(("set", args[4], capb[args[4]], clock[0]))
        return ""

    def fake_counters(d):
        clock[0] += read_cost
        g = d["cfg"][3:]
        c = cum.setdefault(g, [0.0, 0.0, clock[0]])
        if g in mod.VICTIMS:
            if g in launches:
                t0, (delay, secs, rate, _) = launches[g]
                c[0] = vfac[g] * rate * 1e9 / 8 * max(0.0, min(clock[0] - t0, delay + secs) - delay)
        else:
            dt, c[2] = clock[0] - c[2], clock[0]
            if g in launches:
                t0, (delay, secs, _, _) = launches[g]
                if t0 + delay <= clock[0] < t0 + delay + secs:
                    off = FLOOD_GBIT * 1e9 / 8 * dt
                    cap = capb.get(d["cfg"], 0)
                    passed = off if cap == 0 else min(off, cap * dt)
                    c[0] += passed; c[1] += off - passed
        return {0: 0, 1: 0, 2: int(c[0]), 3: int(c[1])}

    def fake_cpu():
        cpu_acc[0] += 100; cpu_acc[1] += 40; cpu_acc[2] += 5
        return cpu_acc[0], cpu_acc[1], cpu_acc[2]

    def fake_ssh(guest, cmd, out):
        p = parse_cmd(cmd)
        launches[guest] = (clock[0], p)
        events.append(("ssh", guest, cmd, clock[0]))
        out.write(json.dumps({"end": {"sum_received": {"bytes": int(vfac.get(guest, 1.0) * (p[2] or FLOOD_GBIT)
                                                                    * 1e9 / 8 * p[1]), "bits_per_second": 1e9}}}))
        out.flush()
        return FakeProc(clock, clock[0] + p[0] + p[1] + 0.3)

    def fake_sleep(s):
        clock[0] += max(0.0, s) + rng.uniform(0.0, 0.04)

    mod.sudo, mod.counters, mod.cpu, mod.ssh = fake_sudo, fake_counters, fake_cpu, fake_ssh
    time.monotonic, time.sleep = (lambda: clock[0]), fake_sleep
    mod.IF.clear()
    for g in mod.VICTIMS + mod.FLOODERS:
        mod.IF[g] = {"dev": f"vnet{g}", "prog": 0, "cfg": f"cfg{g}", "cnt": f"cnt{g}"}
    mod.last_cap.clear()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            r, ts = mod.run(mode, param, dict(REFS), 50.0, [])
    finally:
        time.monotonic, time.sleep = real_time
        for k, v in real.items():
            setattr(mod, k, v)
    return r, ts, events


def col(n):
    return exp40.TS_HEADER.split(",").index(n) - 4


def rcol(n):
    return exp40.RESULT_NAMES.index(n)


def caps_of(row):
    return dict(zip(F, row[exp40.TS_CAP:exp40.TS_CAP + exp40.NF]))


if __name__ == "__main__":
    before39 = sha(EXP39_FILES)
    before40 = sha(OUTS)
    configs = list(exp40.CONFIGS)

    # ================= part 0: configs =================
    assert configs == [("vfrot", "0.8_1_2_0.94"), ("vfrot", "0.8_1_2_0.94_B12000"), ("vfrot", "0.8_1_2_0.94_B20000"),
                       ("vfrot", "0.9_1_2_0.94"), ("vfrot", "0.8_2_2_0.94"), ("vfrot", "0.8_1_1_0.94"),
                       ("vffair", "0.8_1_2_0.94_F0.5"), ("vffair", "0.8_1_2_0.94_F0.25")], configs
    assert len(set(configs)) == 8
    want_p = [(0.8, 1, 2, 0.94, 16000, None), (0.8, 1, 2, 0.94, 12000, None), (0.8, 1, 2, 0.94, 20000, None),
              (0.9, 1, 2, 0.94, 16000, None), (0.8, 2, 2, 0.94, 16000, None), (0.8, 1, 1, 0.94, 16000, None),
              (0.8, 1, 2, 0.94, 16000, 0.5), (0.8, 1, 2, 0.94, 16000, 0.25)]
    assert [exp40.parse_vfrot(*c) for c in configs] == want_p
    assert exp40.parse_vfrot(*REF_CFG)[:4] == exp39.parse_vfrot(REF_CFG[1]) and exp40.VF_BMAX == exp39.VF_BMAX
    bad = [("vfrot", "0.8_1_2_0.94_F0.5"), ("vffair", "0.8_1_2_0.94"), ("vffair", "0.8_1_2_0.94_B12000"),
           ("vfrot", "0.8_1_2_0.94_B2000"), ("vfrot", "0.8_1_2_0.94_B40000"), ("vffair", "0.8_1_2_0.94_F1"),
           ("vffair", "0.8_1_2_0.94_F0"), ("vfrot", "0.8_1_2_0.94_Bx"), ("vfrot", "1.0_1_2_0.94"), ("lqr", "79_4")]
    for m, p in bad:
        try:
            (exp40.setup_configs if m == "lqr" else lambda c: exp40.parse_vfrot(*c[0]))([(m, p)])
            raise AssertionError(f"{m} {p} accepted")
        except SystemExit:
            pass
    try:
        exp40.setup_configs([REF_CFG, REF_CFG]); raise AssertionError("duplicate label accepted")
    except SystemExit as e:
        assert "duplicate" in str(e)
    assert exp40.HEADER.startswith(exp39.HEADER + ","), "exp39 result columns must come first, unchanged"
    assert exp40.HEADER[len(exp39.HEADER) + 1:].split(",") == exp40.EXTRA_NAMES
    assert exp40.TS_HEADER == exp39.TS_HEADER
    n_slots, per_rep, total = exp40.expected_duration(8, REPS)
    _, per_rep_m, total_m = exp40.measured_duration(8, REPS)
    assert n_slots == 17
    print(f"CHECK 0 OK: 8 unique configs parse to {want_p}; config 1 parses exactly as exp39's vfrot; malformed / "
          f"misplaced suffixes, BMAX out of range, F not in (0,1), duplicate labels and other modes are refused; "
          f"results header = exp39 header + {', '.join(exp40.EXTRA_NAMES)}; ts header identical to exp39")
    print(f"  duration: {n_slots} slots/repeat, formula {per_rep / 60:.2f} min/repeat -> {total / 3600:.2f} h; "
          f"exp39 measured rate {per_rep_m / 60:.2f} min/repeat -> {total_m / 3600:.2f} h")

    # ================= part 1: vffair cap logic =================
    rng = random.Random(40)
    n_cases = 0
    for _ in range(5000):
        b = rng.uniform(800.0, 20000.0) if rng.random() > 0.05 else 800.0
        n = rng.randint(1, 4)
        act = sorted(rng.sample(F, n), key=F.index)
        h = rng.choice(act)
        ff = rng.choice([0.25, 0.5, rng.uniform(0.01, 0.99)])
        caps = exp40.fair_alloc(act, b, h, ff)
        cd = dict(zip(F, caps))
        fl = max(exp40.CAP_MIN, ff * b / n)
        assert sum(cd[g] for g in act) <= b + 1e-6, (b, act, h, ff, caps)
        assert all(cd[g] == exp40.CAP_MIN for g in F if g not in act), caps
        if n > 1:
            assert all(abs(cd[g] - fl) < 1e-9 for g in act if g != h), caps
            assert abs(sum(cd[g] for g in act) - b) < 1e-6, "active caps must use the whole budget"
            assert cd[h] >= fl - 1e-9 and cd[h] == max(cd.values())
        else:
            assert cd[h] == exp40.high_cap(b, 1) == b - exp40.CAP_MIN
        n_cases += 1
    assert exp40.fair_alloc(F, 800.0, "C", 0.5) == [200.0] * 4
    assert exp40.fair_alloc(F, 16000.0, "B", 0.5) == [2000.0, 10000.0, 2000.0, 2000.0]
    assert exp40.fair_alloc(F, 16000.0, "D", 0.25) == [1000.0, 1000.0, 1000.0, 13000.0]
    assert exp40.fair_alloc(["A", "C"], 12000.0, "C", 0.5) == [3000.0, 200.0, 9000.0, 200.0]
    for b in (800.0, 1500.0, 3000.0):          # floor <= CAP_MIN -> exactly vfrot's allocation
        for act in (F, ["A", "B"], ["B"]):
            h = act[-1]
            assert exp40.fair_alloc(act, b, h, 0.25 if b < 3000 else 0.05) == \
                [exp40.high_cap(b, len(act)) if g == h else exp40.CAP_MIN for g in F], (b, act)
    print(f"CHECK 1 OK: fair_alloc over {n_cases} random cases (budget 800-20000, 1-4 active, F 0.25/0.5/random): sum "
          "of ACTIVE caps <= budget (== budget with >= 2 active), every active non-holder exactly at the floor "
          "max(200, F x B / n), holder >= floor and the largest cap, inactive flooders 200 (outside the budget, as "
          "vfrot), one active -> B - 200 as vfrot; B=800 -> 4 x 200; 16000/F0.5 -> 10000 + 3 x 2000; 16000/F0.25 -> "
          "13000 + 3 x 1000; floor <= 200 reproduces vfrot's allocation exactly")

    pick = exp40.lowest_delivered_pick({"A": 5.0, "B": 1.0, "C": 3.0, "D": 2.0})
    assert pick(F, "A") == "B" and pick(["A", "C", "D"], "A") == "D"
    tie = exp40.lowest_delivered_pick({"A": 1.0, "B": 1.0, "C": 1.0, "D": 9.0})
    assert tie(F, "A") == "B" and tie(F, "B") == "C" and tie(F, "C") == "A" and tie(["A"], "A") == "A"
    st_ = {"holder": "B", "since": 10.0}
    d = {"A": 0.0, "B": 9.0, "C": 9.0, "D": 9.0}
    exp40.vffair_caps(F, 16000.0, 11.9, 2.0, None, d, st_, 0.5)
    assert st_ == {"holder": "B", "since": 10.0}, "no hand-over before T"
    exp40.vffair_caps(F, 16000.0, 12.0, 2.0, None, d, st_, 0.5)
    assert st_ == {"holder": "A", "since": 12.0}, st_
    exp40.vffair_caps(["B", "C"], 16000.0, 12.25, 2.0, None, {"A": 0.0, "B": 5.0, "C": 4.0, "D": 0.0}, st_, 0.5)
    assert st_["holder"] == "C" and st_["since"] == 12.25, "an inactive holder is replaced immediately"
    assert exp40.vffair_caps([], 16000.0, 13.0, 2.0, [1, 2, 3, 4], d, st_, 0.5) == [1, 2, 3, 4]
    # identical holder sequence: vfrot and vffair driven by the same random (act, delivered, elapsed, budget) inputs
    rng = random.Random(7)
    for rot_t in (1.0, 2.0):
        s1, s2 = {"holder": None, "since": None}, {"holder": None, "since": None}
        dl = {g: 0.0 for g in F}
        c1 = c2 = [exp40.CAP_MIN] * 4
        el, seq1, hand = 0.0, [], 0
        for _ in range(4000):
            el += rng.uniform(0.2, 0.3)
            act = [g for g in F if rng.random() < 0.85]
            for g in act:
                dl[g] += rng.choice([0.0, 1.0, 2.0, rng.uniform(0, 3)])   # includes exact ties
            b = rng.uniform(800, 20000)
            c1 = exp40.pidrotf_caps(act, b, el, rot_t, c1, dl, s1)
            c2 = exp40.vffair_caps(act, b, el, rot_t, c2, dl, s2, rng.choice([0.25, 0.5]))
            assert s1 == s2, (el, s1, s2)
            hand += bool(seq1) and seq1[-1] != s1["holder"]
            seq1.append(s1["holder"])
        assert hand > 100
    print("CHECK 2 OK: rotation picks the lowest-delivered active flooder (A5/B1/C3/D2 -> B), ties go round-robin to a "
          "non-holder, no hand-over before T, an inactive holder is replaced at once, with no flooder active the last caps are kept; "
          "over 2 x 4000 random ticks (T 1 and 2 s, random activity, exact ties) vffair's holder and decision "
          "time equal vfrot's at every tick")

    # ================= part 2: the real run() =================
    exp39.VFROT[REF_CFG[1]] = exp39.parse_vfrot(REF_CFG[1])   # exp39 fills this in its main() (in memory only)
    for seed, fac in ((1, None), (2, {"V1": 1.0, "V2": 0.90}), (3, {"V1": 0.96, "V2": 0.93})):
        r39, ts39, ev39 = simulate_run(exp39, *REF_CFG, seed, fac)
        r40, ts40, ev40 = simulate_run(exp40, *REF_CFG, seed, fac)
        assert len(ts39) == len(ts40) and all(a == b or (a != a and b != b) for x, y in zip(ts39, ts40)
                                              for a, b in zip(x, y)), "ts rows differ from exp39"
        assert [e for e in ev39 if e[0] == "set"] == [e for e in ev40 if e[0] == "set"], "cap writes differ"
        assert all(a == b or (a != a and b != b) for a, b in zip(r39, r40[:exp40.R_X])), "results differ"
    print(f"CHECK 3 OK: exp40 vfrot 0.8_1_2_0.94 == REAL exp39 vfrot tick by tick (all {len(ts40)} ts rows, every cap "
          f"write with its time, all exp39 result columns) for victims 1.00/1.00, 1.00/0.90 and 0.96/0.93")

    for m, p in configs + [ALONE]:
        _, _, ev = simulate_run(exp40, m, p, 1)
        ssh_ev = [e for e in ev if e[0] == "ssh"]
        vc = {e[1]: e[2] for e in ssh_ev if e[1] in exp40.VICTIMS}
        fl = {e[1]: e[2] for e in ssh_ev if e[1] in F}
        assert vc == {"V1": "iperf3 -c 10.10.0.1 -p 5201 -t 40 -b 9G -J",
                      "V2": "iperf3 -c 10.10.0.1 -p 5205 -t 40 -b 9G -J"}, vc
        assert (not fl) if m == "alone" else fl == {g: exp39.flooder_cmds()[g] for g in F}, fl
    print("CHECK 4 OK: every config launches exp39's exact victim and flooder commands (alone: victims only)")

    res = {}
    for m, p in configs:
        res[(m, p)] = simulate_run(exp40, m, p, 4)
    for p, bm in (("0.8_1_2_0.94_B12000", 12000.0), ("0.8_1_2_0.94_B20000", 20000.0), ("0.8_1_2_0.94", 16000.0)):
        r, ts, _ = res[("vfrot", p)]
        b = [row[-2] for row in ts]
        assert max(b) == bm and all(x <= bm for x in b), (p, max(b))
        cs = sum(x >= bm for x in b) / len(b)
        assert abs(r[rcol("budget_ceiling_share")] - cs) < 1e-12 and r[rcol("budget_floor_share")] == 0.0
    ceil = {p: res[("vfrot", p)][0][rcol("budget_ceiling_share")] for p in
            ("0.8_1_2_0.94_B12000", "0.8_1_2_0.94", "0.8_1_2_0.94_B20000")}
    assert ceil["0.8_1_2_0.94_B12000"] > ceil["0.8_1_2_0.94"] > ceil["0.8_1_2_0.94_B20000"] > 0, ceil
    rd, tsd, _ = simulate_run(exp40, "vfrot", "0.8_1_2_0.94_B20000", 4, {"V1": 1.0, "V2": 0.90})
    assert min(row[-2] for row in tsd) == 800.0 and rd[rcol("budget_floor_share")] > 0.3
    # ramp slope (ADD) and cut factor (BETA) from the budget trace
    def first_ramp(ts):
        for a, b in zip(ts, ts[1:]):
            if b[-2] > a[-2] and b[-2] < 15000:
                return (b[-2] - a[-2]) / b[col("tick_s")]
    assert abs(first_ramp(res[("vfrot", "0.8_1_2_0.94")][1]) - 1000.0) < 1e-6
    assert abs(first_ramp(res[("vfrot", "0.8_2_2_0.94")][1]) - 2000.0) < 1e-6
    for p, beta in (("0.8_1_2_0.94", 0.8), ("0.9_1_2_0.94", 0.9)):
        _, ts, _ = simulate_run(exp40, "vfrot", p, 5, {"V1": 0.90, "V2": 1.0})
        cuts = [b[-2] / a[-2] for a, b in zip(ts, ts[1:]) if b[-2] < a[-2] and b[-2] > 800.0 + 1e-6]   # cuts not clipped by the clamp
        assert cuts and all(abs(x - beta) < 1e-9 for x in cuts), (p, cuts[:5])
    def handovers(ts):
        ov, hs = exp40.overlap(), []
        for row in ts:
            if ov[0] + 0.5 < row[0] < ov[1]:
                c = caps_of(row); hs.append((row[0], max(c, key=c.get)))
        times = [b[0] for a, b in zip(hs, hs[1:]) if a[1] != b[1]]
        return times
    h2, h1 = handovers(res[("vfrot", "0.8_1_2_0.94")][1]), handovers(res[("vfrot", "0.8_1_1_0.94")][1])
    gaps1 = [b - a for a, b in zip(h1, h1[1:])]
    assert len(h1) > len(h2) and min(gaps1) >= 1.0 - 1e-9 and min(b - a for a, b in zip(h2, h2[1:])) >= 2.0 - 1e-9
    print(f"CHECK 5 OK: budget never above its BMAX and reaches it (12000/16000/20000), ceiling share = share of all "
          f"ticks at BMAX (here {', '.join(f'{k} {v:.3f}' for k, v in ceil.items())}); a degraded victim drives "
          f"B20000 to the 800 clamp (floor share {rd[rcol('budget_floor_share')]:.3f}); ramp 1000 vs 2000 Mbit/s per s "
          f"for ADD 1/2; every cut x0.8 / x0.9 for BETA 0.8/0.9; T=1: {len(h1)} hand-overs in 18.5-28 s (min gap "
          f"{min(gaps1):.2f} s) vs T=2: {len(h2)}")

    for p, ff in (("0.8_1_2_0.94_F0.5", 0.5), ("0.8_1_2_0.94_F0.25", 0.25)):
        r, ts, _ = res[("vffair", p)]
        n_all = 0
        for row in ts:
            c = caps_of(row); b = row[-2]
            if 18.5 < row[0] < 27.5:             # all four active (D starts at 18 s)
                fl = max(exp40.CAP_MIN, ff * b / 4)
                hi = [g for g in F if c[g] > fl + 1e-9]
                assert len(hi) == 1 and abs(sum(c.values()) - b) < 1e-6, (row[0], c, b)
                assert all(abs(c[g] - fl) < 1e-9 for g in F if g not in hi)
                n_all += 1
            if 0.5 < row[0] < 5.5:               # only A active: A = B - 200, the rest 200 (as vfrot)
                assert c == {"A": b - 200.0, "B": 200.0, "C": 200.0, "D": 200.0}, (row[0], c)
        assert n_all > 30
        hc = [r[rcol(f"highcap{g}_share")] for g in F]
        assert abs(sum(hc) - 1.0) < 1e-9 and all(x > 0 for x in hc), hc
        assert r[rcol("min_flooder_ovl_gbps")] > res[REF_CFG][0][rcol("min_flooder_ovl_gbps")]
    r = res[REF_CFG][0]
    eb = [r[rcol(f"flooder{g}_ebpf_gbps")] for g in F]
    assert abs(r[rcol("flooder_total_gbps")] - sum(eb)) < 1e-12 and r[rcol("min_flooder_own_gbps")] == min(eb)
    ra, _, _ = simulate_run(exp40, *ALONE, 4)
    assert ra[rcol("flooder_total_gbps")] == 0.0 and all(math.isnan(ra[rcol(n)]) for n in exp40.EXTRA_NAMES[1:])
    print("CHECK 6 OK: vffair in the simulated run: in 18.5-27.5 s exactly one holder above the floor, the 3 others "
          "at max(200, F x B / 4), caps sum to B; with only A active A = B - 200 and the others 200 (as vfrot); "
          "high-cap shares sum to 1 over the 4 flooders; min_flooder_ovl above vfrot's; flooder_total = sum of the "
          "eBPF means, min_flooder_own = their minimum; alone: total 0, the rest nan. Shares here: " + ", ".join(
              f"{p}: " + "/".join(f"{res[('vffair', p)][0][rcol(f'highcap{g}_share')]:.2f}" for g in F)
              for p in ("0.8_1_2_0.94_F0.5", "0.8_1_2_0.94_F0.25")))

    row = lambda t, caps, rates, b: (t, 0.25, 0.0, 0.0, 0.0) + (9.0,) * 4 + tuple(caps) + tuple(rates) + (b, 1.0)
    syn = [row(10.0, (15400, 200, 200, 200), (11, 0, 0, 0), 16000.0),
           row(18.0, (15400, 200, 200, 200), (7.7, .2, .2, .2), 16000.0),
           row(19.0, (200, 11400, 200, 200), (.2, 5.7, .2, .2), 12000.0),
           row(20.0, (200, 200, 200, 200), (.2, .2, .2, .2), 800.0),
           row(28.0, (200, 200, 15400, 200), (.2, .2, 15.4, .2), 16000.0)]
    assert exp40.budget_shares(syn, 16000.0) == (3 / 5, 1 / 5)
    assert exp40.highcap_shares(syn) == [1 / 3, 1 / 3, 0.0, 0.0]   # 18 <= t < 28; at B=800 no cap reaches 400
    assert exp40.holder_util(syn, 16000.0) == (1, 0.5, 7.7)
    print("CHECK 7 OK: on a synthetic ts: ceiling share 3/5 and floor share 1/5 over all ticks; high-cap shares over "
          "18 <= t < 28 only (cap >= 0.5 x budget); holder delivered/cap at the ceiling 0.5, max holder rate 7.7")

    # ================= part 3: main(), summary, refusals =================
    calls, log, tmp, sleeps = run_main_dry()
    seqs = [[c for r, c, _ in calls if r == rep] for rep in range(1, REPS + 1)]
    print("\n--- exp40.log header lines (as main() prints them) ---")
    print("\n".join(l for l in log.splitlines() if l.startswith(("SCENARIO", "CONFIGS", "REFERENCE CONFIG", "vfrot:",
                                                                   "vffair:", "METRICS", "HOST CPU", "EXPECTED"))))
    print("\nconfig order per repeat (slot -> config):")
    w = max(len(name(c)) for c in configs + [ALONE])
    print("slot " + " ".join(f"{'r' + str(r):{w}s}" for r in range(1, REPS + 1)))
    for slot in range(n_slots):
        print(f"{slot:4d} " + " ".join(f"{name(seq[slot]):{w}s}" for seq in seqs))
    random.seed(7)
    for rep, seq in enumerate(seqs, 1):
        assert len(seq) == 17 and sorted(c for c in seq if c != ALONE) == sorted(configs), rep
        assert seq[0::2] == [ALONE] * 9 and all(c != ALONE for c in seq[1::2]), rep
        order = configs[:]; random.shuffle(order)
        assert seq == [ALONE] + [x for c in order for x in (c, ALONE)], f"repeat {rep}: order differs"
    assert [s for s in sleeps if s == 600] == [600] * (REPS - 1) and "cool-down 45 s" not in log
    pos = {name(c): len({seq.index(c) for seq in seqs}) for c in configs}
    print(f"\nCHECK 8 OK: 17 slots per repeat, alone at slots 0,2,...,16, each of the 8 configs once; order equals an "
          f"independent random.seed(7) shuffle per repeat; {REPS - 1} x 600 s rests; distinct slot positions per "
          f"config: " + ", ".join(f"{k} {v}" for k, v in pos.items()))

    rows = open(exp40.CSV).read().splitlines()
    assert rows[0] == exp40.HEADER and len(rows) == 1 + REPS * 17
    assert all(len(r.split(",")) == len(rows[0].split(",")) for r in rows)
    labels = {tuple(r.split(",")[2:4]) for r in rows[1:]}
    assert labels == set(configs) | {ALONE}
    tsr = open(exp40.TS_CSV).read().splitlines()
    assert tsr[0] == exp40.TS_HEADER and all(len(r.split(",")) == len(tsr[0].split(",")) for r in tsr)
    assert f"EXPECTED DURATION: 17 slots/repeat, ~{per_rep / 60:.1f} min/repeat (exp39 measured rate " \
           f"~{per_rep_m / 60:.1f}), 6 repeats + 5 x 600 s rest ~ {total / 3600:.2f} h (measured rate " \
           f"~{total_m / 3600:.2f} h)" in log
    assert len([l for l in log.splitlines() if l.startswith("HOLDER rep")]) == REPS * 8
    assert "HOLDER rep 1 slot 1 " in log and "1 ticks at the" in log
    assert "WARNINGS (mean tick > 0.3 s, steal mean > 1% or max > 5%): 0" in log and "WARNING: rep" not in log
    assert "SUMMARY40 from results_dyn40.csv" in log and "PAIRED vs vfrot 0.8_1_2_0.94" in log
    assert all(f"{c[0]:7s} {c[1]:>20s} {REPS:4d}" in log for c in configs)
    print("CHECK 9 OK: CSV/ts row widths match their headers, 1 + 6 x 17 rows, labels unique per config; EXPECTED "
          "DURATION, one HOLDER line per vfrot/vffair slot, SUMMARY40 + PAIRED block at the end of the log")
    print("  CSV header: " + rows[0])

    _, log_w, _, _ = run_main_dry(slow={("vfrot", "0.8_1_2_0.94_B20000")}, steal={("vffair", "0.8_1_2_0.94_F0.5")})
    wl = [l for l in log_w.splitlines() if l.startswith("WARNING: rep")]
    assert len(wl) == 2 * REPS
    assert sum("vfrot 0.8_1_2_0.94_B20000: mean control tick 0.310 s > 0.3 s" in l for l in wl) == REPS
    assert sum("vffair 0.8_1_2_0.94_F0.5: steal mean 1.50% / max 2.00% above 1% / 5%" in l for l in wl) == REPS
    assert f"WARNINGS (mean tick > 0.3 s, steal mean > 1% or max > 5%): {2 * REPS}" in log_w
    print(f"CHECK 10 OK: tick > 0.3 s and steal mean > 1 % each give a WARNING line and count at the end: {wl[0]!r}")

    # summary with known answers: ok_worst ref 0.90/1.00/0.95, B12000 1.00/1.00/0.90 -> +0.017, W/T/L 1/1/1
    known = {}
    okr, okb = [0.90, 1.00, 0.95, 1.0, 1.0, 1.0], [1.00, 1.00, 0.90, 1.0, 1.0, 1.0]
    for rep in range(1, REPS + 1):
        known[(rep, REF_CFG)] = fake_result(slo_ok_worst=okr[rep - 1], jain_fairness=0.6, flooder_total_gbps=18.0)
        known[(rep, ("vfrot", "0.8_1_2_0.94_B12000"))] = fake_result(
            slo_ok_worst=okb[rep - 1], jain_fairness=0.6 + 0.01 * rep, flooder_total_gbps=17.0)
    _, log_s, tmp_s, _ = run_main_dry(results=known)
    order, stats, diffs = exp40.summary_data(exp40.CSV)
    assert order == configs
    dB = diffs[("vfrot", "0.8_1_2_0.94_B12000")]
    assert dB["slo_ok_worst"][3] == (1, 4, 1) and abs(dB["slo_ok_worst"][0] - 0.05 / 6) < 1e-9
    assert dB["jain_fairness"][3] == (6, 0, 0) and abs(dB["jain_fairness"][0] - 0.035) < 1e-9
    assert dB["flooder_total_gbps"][3] == (0, 0, 6) and abs(dB["flooder_total_gbps"][0] + 1.0) < 1e-9
    assert dB["budget_ceiling_share"][3] is None
    assert diffs[("vffair", "0.8_1_2_0.94_F0.5")]["jain_fairness"][3] == (6, 0, 0)   # 0.9 vs 0.6
    assert abs(stats[REF_CFG]["slo_ok_worst"][0] - sum(okr) / len(okr)) < 1e-9
    off = subprocess.run([sys.executable, "-B", os.path.join(HERE, "exp40.py"), "--summary", exp40.CSV],
                         capture_output=True, text=True)
    assert off.returncode == 0 and off.stdout.strip() == exp40.summarize(exp40.CSV) and off.stdout.strip() in log_s
    print("CHECK 11 OK: summary_data/summarize on a run with known values: B12000 vs reference ok_worst W/T/L 1/4/1, "
          "mean diff +0.0083; Jain +0.035 6/0/0; flooder total -1.0 0/0/6; ceiling share no winner; the offline "
          "'python3 exp40.py --summary FILE' prints exactly the block at the end of the log:")
    print("\n".join("  " + l for l in off.stdout.splitlines()))

    for fake in ((4, 4), (None, 6), (8, 8)):
        d = tempfile.mkdtemp(prefix="test_exp40_np_")
        redirect_outputs(d)
        real_hc, real_sudo = exp40.host_cpus, exp40.sudo
        exp40.host_cpus = lambda _f=fake: _f
        exp40.sudo = lambda args: (_ for _ in ()).throw(AssertionError("sudo called before the nproc guard"))
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                exp40.main()
            raise AssertionError(f"main() started with nproc {fake}")
        except SystemExit as e:
            nmsg = str(e)
        finally:
            exp40.host_cpus, exp40.sudo = real_hc, real_sudo
        assert nmsg.startswith(f"REFUSING TO START: nproc = {fake[0]} (os.cpu_count() = {fake[1]}), exp40 requires "
                               "exactly 6"), nmsg
        assert os.listdir(d) == []
    print(f"CHECK 12 OK: main() refuses with nproc 4 / unknown / 8 before sudo and before creating any file")

    real_run = subprocess.run
    for f in OUTS:
        d = tempfile.mkdtemp(prefix="test_exp40_ow_")
        redirect_outputs(d)
        open(os.path.join(d, f), "w").write("KEEP\n")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                exp40.main()
            raise AssertionError(f"main() started although {f} exists")
        except SystemExit as e:
            assert "REFUSING TO START" in str(e) and f in str(e), e
        assert open(os.path.join(d, f)).read() == "KEEP\n" and os.listdir(d) == [f]
        shutil.copy(os.path.join(HERE, "run40.sh"), d)
        p = real_run(["sh", os.path.join(d, "run40.sh")], capture_output=True, text=True)
        assert p.returncode == 1 and p.stdout.strip() == (f"REFUSING TO START: {f} already exists (move or delete "
                                                          "it first; exp40 never appends)"), (p.returncode, p.stdout)
        assert open(os.path.join(d, f)).read() == "KEEP\n" and sorted(os.listdir(d)) == sorted([f, "run40.sh"])
    print("CHECK 13 OK: with any one of " + ", ".join(OUTS) + " present, main() and run40.sh refuse to start and "
          "leave it unchanged")

    full = [5201, 5202, 5203, 5204, 5205, 5206]
    d = tempfile.mkdtemp(prefix="test_exp40_sh_")
    shutil.copy(os.path.join(HERE, "run40.sh"), d)
    shutil.copy(os.path.join(HERE, "exp40.py"), d)    # the planned-duration line imports exp40 from the script dir
    bindir = os.path.join(d, "bin"); os.mkdir(bindir)
    def tools(ports, nproc, py_real):
        for tool, body in (("ss", "printf '%s\\n' " + " ".join(f"'LISTEN 0 5 *:{p} *:*'" for p in ports)),
                           ("nproc", f"echo {nproc}"), ("sudo", f"echo sudo >> {d}/MARK; exit 1"),
                           ("nohup", f"echo nohup >> {d}/MARK; exit 1"), ("pgrep", "exit 1"),
                           ("python3", f'exec {sys.executable} -B "$@"' if py_real else
                            f"echo python3 >> {d}/MARK; exit 1")):
            with open(os.path.join(bindir, tool), "w") as fh:
                fh.write(f"#!/bin/sh\n{body}\n")
            os.chmod(os.path.join(bindir, tool), 0o755)
    env = dict(os.environ, PATH=bindir + ":" + os.environ["PATH"])
    tools(full[:5], 6, False)
    p = real_run(["sh", os.path.join(d, "run40.sh")], capture_output=True, text=True, env=env)
    assert p.returncode == 1 and p.stdout.strip() == ("host CPUs: 6\nno iperf3 server listening on host port 5206; "
                                                      "start it first: iperf3 -s -D -p 5206"), (p.stdout, p.stderr)
    assert not os.path.exists(os.path.join(d, "MARK"))
    tools(full, 4, False)
    p = real_run(["sh", os.path.join(d, "run40.sh")], capture_output=True, text=True, env=env)
    assert p.returncode == 1 and p.stdout.strip() == ("REFUSING TO START: nproc = 4, exp40 requires exactly 6 "
                                                      "(continues exp39 on the 6 vCPU host VM)"), p.stdout
    assert not os.path.exists(os.path.join(d, "MARK"))
    tools(full, 6, True)                      # all checks pass: real python3 for the plan line, fake sudo refuses
    p = real_run(["sh", os.path.join(d, "run40.sh"), "6"], capture_output=True, text=True, env=env)
    plan = p.stdout.strip().splitlines()
    assert p.returncode == 1 and plan[0] == "host CPUs: 6" and plan[1].startswith("planned: SCENARIO steady4, 6 "
           "repeats x 17 slots (8 configs + 9 alone)"), (p.stdout, p.stderr)
    assert open(os.path.join(d, "MARK")).read() == "sudo\n", "must stop at sudo -v (fake, fails) before nohup"
    assert not any(os.path.exists(os.path.join(d, f)) for f in OUTS)
    print(f"CHECK 14 OK: run40.sh: 5206 missing -> exits before python3/sudo/nohup; nproc 4 -> refuses; all checks "
          f"pass -> prints the plan, then asks sudo -v (fake, failing here) and stops before nohup:\n  {plan[1]}")

    assert sha(EXP39_FILES) == before39, "an exp39 file changed"
    assert sha(OUTS) == before40, "an exp40 output file appeared or changed in the testbed"
    print("CHECK 15 OK: sha256 of " + ", ".join(EXP39_FILES) + " unchanged; no exp40 output file in the testbed")
    print("ALL TESTS PASSED")
