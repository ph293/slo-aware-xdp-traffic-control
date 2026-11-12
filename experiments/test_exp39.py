#!/usr/bin/env python3
# Pure-python self-test for exp39.py: no VM, no sudo, no network, no traffic, no sleeping.
# exp39 = exp38 on 6 vCPU: + lqr 90_4 (13 slots), nproc == 6 guard (main() and run39.sh), headroom / core-count
# logging, x0.67 stability row; host_cpus() is stubbed wherever main() runs past the output-file check.
# Part 1 runs the REAL exp39.main() for REPS repeats with every side effect stubbed (sudo, libvirt/bpftool,
# iperf3 servers, guests, reference(), run(), time.sleep) and output files redirected to a temp dir, and checks
# the slot order against an independent reproduction (random.seed(7), per repeat configs[:]; random.shuffle).
# Part 2 runs the REAL exp39.run() tick loop for every config and alone with a simulated clock (with jitter and a
# per-counter-read cost), host CPU with steal, two victim counters that follow given rate profiles and four
# flooder counters that send only inside their iperf3 window and are limited by the cap last written to their
# cfg map; only sudo, ssh, /proc/stat and the clock are faked. Part 3 runs the REAL reference(), check_servers(),
# check_guests(), vnet() errors, the output-file refusals of main() and run39.sh (copied into a temp dir, with
# fake ss/sudo/python3 first on PATH so it can never reach the real sudo), and static checks of make_tenant5/6.sh.
import contextlib, io, json, math, os, random, shutil, struct, subprocess, sys, tempfile, time
sys.argv = sys.argv[:1]          # exp39 reads REPS from argv: use its default (6)
import exp39

HERE = os.path.dirname(os.path.abspath(exp39.__file__))
REPS = 6
ALONE = ("alone", "na")
REFS = {"V1": 9.0, "V2": 9.0}    # references used in the simulated runs
OUTS = ("results_dyn39.csv", "ts_dyn39.csv", "ref_victim39.txt", "exp39.log")
FLOOD_GBIT = 6.0                 # simulated offered rate of an active flooder


def name(c):
    return f"{c[0]} {c[1]}"


def redirect_outputs(tmp):
    exp39.CSV, exp39.TS_CSV = os.path.join(tmp, OUTS[0]), os.path.join(tmp, OUTS[1])
    exp39.REF_FILE, exp39.LOG = os.path.join(tmp, OUTS[2]), os.path.join(tmp, OUTS[3])


def fake_result(tick_mean=0.25):
    # run() result: per victim (mean, p5, ok), ok_worst, per flooder (iperf, ebpf), jain, cpu x2, steal x2, tick x2
    return ((9.0, 8.9, 1.0, 8.8, 8.7, 0.9, 0.9) + (1.0, 1.0) * exp39.NF
            + (0.9, 80.0, 90.0, 1.5, 3.0, tick_mean, tick_mean + 0.1))


def fake_ts_row():
    return ((1.0, 0.25, 0.01, 60.0, 5.0) + (9.0,) * exp39.NV + (9.0,) * exp39.NV
            + (500.0,) * exp39.NF + (1.0,) * exp39.NF + (2000.0, 0.99))


def run_main_dry(slow=(), base_cpus=None):
    # slow: configs whose fake run() reports a mean tick of 0.31 s; base_cpus: fake base_cpu per repeat (default 50)
    tmp = tempfile.mkdtemp(prefix="test_exp39_")
    redirect_outputs(tmp)
    exp39.OBJ = os.path.abspath(exp39.__file__)        # any existing file: main() only checks it exists
    calls, rep_now, sleeps = [], [0], []
    saved = {k: getattr(exp39, k) for k in ("sudo", "check_servers", "check_guests", "vnet", "attach", "set_cap",
                                            "reference", "run", "host_cpus")}
    exp39.sudo = lambda args: ""
    exp39.host_cpus = lambda: (6, 6)
    exp39.check_servers = lambda: None
    exp39.check_guests = lambda: None
    exp39.vnet = lambda dom: "vnet-" + dom
    exp39.attach = lambda dev, cap: {"dev": dev, "prog": 0, "cfg": dev, "cnt": dev}
    exp39.set_cap = lambda d, mbps: None

    def fake_reference(rep, procs):
        rep_now[0] = rep
        return dict(REFS), (base_cpus[rep - 1] if base_cpus else 50.0)

    def fake_run(mode, param, refs, base_cpu, procs):
        calls.append((rep_now[0], (mode, param), refs))
        return fake_result(0.31 if (mode, param) in slow else 0.25), [fake_ts_row()]

    exp39.reference, exp39.run = fake_reference, fake_run
    real_sleep, time.sleep = time.sleep, lambda s: sleeps.append(s)
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            exp39.main()
    finally:
        time.sleep = real_sleep
        for k, v in saved.items():
            setattr(exp39, k, v)
    return calls, out.getvalue(), tmp, sleeps


def parse_cmd(cmd):
    # (delay s, duration s, total Gbit/s or None, streams) of one iperf3 client command
    w = cmd.split()
    delay = float(w[1].rstrip(";")) if w[0] == "sleep" else 0.0
    par = int(w[w.index("-P") + 1]) if "-P" in w else 1
    rate = float(w[w.index("-b") + 1].rstrip("G")) * par if "-b" in w else None
    return delay, float(w[w.index("-t") + 1]), rate, par


class FakeProc:
    # exits when the simulated clock passes the end of its iperf3 run
    def __init__(self, clock, end): self.clock, self.end = clock, end
    def poll(self): return 0 if self.clock[0] >= self.end else None
    def wait(self): self.clock[0] = max(self.clock[0], self.end); return 0
    def terminate(self): pass


def simulate_run(mode, param, seed, vfac=None, read_cost=0.002, refs=None):
    # the real exp39.run(); victim v delivers vfac[v] x its -b rate from its launch for its -t seconds; flooder g
    # offers FLOOD_GBIT inside its window (sleep + -t after launch) and passes min(offered, its current cap)
    rng = random.Random(seed)
    clock = [1000.0]
    vfac = vfac or {v: 1.0 for v in exp39.VICTIMS}
    launches, events, capb = {}, [], {}
    cum = {}                                   # cfg -> [passed bytes, dropped bytes, last read time]
    cpu_acc = [0, 0, 0]
    real = {k: getattr(exp39, k) for k in ("sudo", "counters", "cpu", "ssh")}
    real_time = (time.monotonic, time.sleep)

    def fake_sudo(args):
        assert args[:3] == ["bpftool", "map", "update"], args
        capb[args[4]] = struct.unpack("<Q", bytes.fromhex("".join(args[-8:])))[0]
        events.append(("set", args[4], capb[args[4]], clock[0]))
        return ""

    def fake_counters(d):
        clock[0] += read_cost
        g = d["cfg"][3:]
        events.append(("read", g, clock[0]))
        c = cum.setdefault(g, [0.0, 0.0, clock[0]])
        if g in exp39.VICTIMS:
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
        # every reading: +100 jiffies, 40 idle, 5 steal -> busy 60 %, steal 5 %
        cpu_acc[0] += 100; cpu_acc[1] += 40; cpu_acc[2] += 5
        return cpu_acc[0], cpu_acc[1], cpu_acc[2]

    def fake_ssh(guest, cmd, out):
        assert cmd.count("iperf3 ") == 1 and cmd.endswith(" -J"), cmd
        p = parse_cmd(cmd)
        launches[guest] = (clock[0], p)
        events.append(("ssh", guest, cmd, clock[0]))
        out.write(json.dumps({"end": {"sum_received": {"bytes": int(vfac.get(guest, 1.0) * (p[2] or FLOOD_GBIT)
                                                                        * 1e9 / 8 * p[1]),
                                                       "bits_per_second": 1e9}}}))
        out.flush()
        return FakeProc(clock, clock[0] + p[0] + p[1] + 0.3)

    def fake_sleep(s):
        clock[0] += max(0.0, s) + rng.uniform(0.0, 0.04)              # timing jitter

    exp39.sudo, exp39.counters, exp39.cpu, exp39.ssh = fake_sudo, fake_counters, fake_cpu, fake_ssh
    time.monotonic, time.sleep = (lambda: clock[0]), fake_sleep
    exp39.IF.clear()
    for g in exp39.VICTIMS + exp39.FLOODERS:
        exp39.IF[g] = {"dev": f"vnet{g}", "prog": 0, "cfg": f"cfg{g}", "cnt": f"cnt{g}"}
    exp39.last_cap.clear()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            r, ts = exp39.run(mode, param, dict(refs or REFS), 50.0, [])
    finally:
        time.monotonic, time.sleep = real_time
        for k, v in real.items():
            setattr(exp39, k, v)
    return r, ts, events


def col(name):
    return exp39.TS_HEADER.split(",").index(name) - 4      # ts rows start at t (after rep,slot,mode,param)


def rcol(name):
    return exp39.HEADER.split(",").index(name) - 4         # run() results start at victim1_mean_gbps


if __name__ == "__main__":
    configs = list(exp39.CONFIGS)
    assert configs == [("static", "2500"), ("static", "3500"), ("lqr", "79_4"), ("lqr", "85_4"), ("lqr", "90_4"),
                       ("vfrot", "0.8_1_2_0.94")], configs
    assert exp39.NPROC_REQUIRED == 6 and exp39.VF_B0 == 10000.0 and exp39.VF_BMAX == 16000.0 and exp39.K_CPU == 0.93
    assert exp39.REPS == 6 and exp39.SESSION_REST_S == 600 and exp39.T == 40 and exp39.V_RATE == 9.0
    assert exp39.STEADY[exp39.SCENARIO] == {"A": (0, 28), "B": (6, 28), "C": (12, 28), "D": (18, 22)}
    assert exp39.overlap() == (18.0, 28.0) and exp39.PAR == {g: 2 for g in "ABCD"}
    assert {g: exp39.GUESTS[g] for g in exp39.GUESTS} == {
        "V1": ("tenant1", "10.10.0.11", 5201), "V2": ("tenant5", "10.10.0.15", 5205),
        "A": ("tenant2", "10.10.0.12", 5202), "B": ("tenant3", "10.10.0.13", 5203),
        "C": ("tenant4", "10.10.0.14", 5204), "D": ("tenant6", "10.10.0.16", 5206)}
    n_slots, per_rep, total = exp39.expected_duration(len(configs), REPS)
    print(f"SCENARIO {exp39.SCENARIO}; CONFIGS ({len(configs)}): " + ", ".join(name(c) for c in configs))
    print(f"expected_duration: {n_slots} slots/repeat, {per_rep / 60:.2f} min/repeat; {REPS} repeats ~ {total / 3600:.2f} h")

    # ================= part 1: main() slot order, log, CSV =================
    calls, log, tmp, sleeps = run_main_dry()
    seqs = [[c for r, c, _ in calls if r == rep] for rep in range(1, REPS + 1)]
    print("\n--- exp39.log header lines (as main() prints them) ---")
    print("\n".join(l for l in log.splitlines()
                    if l.startswith(("SCENARIO", "CONFIGS", "VICTIM", "FLOODER CMD", "REF CMD", "vfrot:", "vfrot NOTE", "HOST CPU",
                                     "EXPECTED", "INTERFACES", "LQR"))))
    print("\nconfig order per repeat (slot -> config):")
    w = max(len(name(c)) for c in configs + [ALONE])
    print("slot " + " ".join(f"{'r' + str(r):{w}s}" for r in range(1, REPS + 1)))
    for slot in range(n_slots):
        print(f"{slot:4d} " + " ".join(f"{name(seq[slot]):{w}s}" for seq in seqs))

    for rep, seq in enumerate(seqs, 1):
        assert len(seq) == 13 == n_slots, (rep, len(seq))
        assert sorted(c for c in seq if c != ALONE) == sorted(configs), f"repeat {rep}: configs not each exactly once"
    print("\nCHECK 1 OK: every repeat has 13 slots and each of the 6 configs exactly once")

    for rep, seq in enumerate(seqs, 1):
        assert seq[0::2] == [ALONE] * 7, f"repeat {rep}: alone not at every even slot"
        assert all(c != ALONE for c in seq[1::2]), f"repeat {rep}: alone at an odd slot"
    random.seed(7)
    for rep, seq in enumerate(seqs, 1):
        order = configs[:]; random.shuffle(order)
        assert seq == [ALONE] + [x for c in order for x in (c, ALONE)], f"repeat {rep}: order differs"
    rests = [s for s in sleeps if s == 600]
    assert len(rests) == REPS - 1 and log.count("session rest 600 s") == REPS - 1, len(rests)
    assert "cool-down 45 s before repeat" not in log
    pos = {name(c): len({seq.index(c) for seq in seqs}) for c in configs}
    print("CHECK 2 OK: alone at slots 0,2,...,12; order equals an independent random.seed(7) shuffle per repeat; "
          f"{REPS - 1} x 600 s rests, no extra cool-down; distinct slot positions per config: "
          + ", ".join(f"{k} {v}" for k, v in pos.items()))

    lines = log.splitlines()
    want = {"VICTIM CMD V1: ssh alpine@10.10.0.11 'iperf3 -c 10.10.0.1 -p 5201 -t 40 -b 9G -J'",
            "VICTIM CMD V2: ssh alpine@10.10.0.15 'iperf3 -c 10.10.0.1 -p 5205 -t 40 -b 9G -J'",
            "FLOODER CMD A: ssh alpine@10.10.0.12 'iperf3 -c 10.10.0.1 -p 5202 -t 28 -P 2 -J'",
            "FLOODER CMD B: ssh alpine@10.10.0.13 'sleep 6; iperf3 -c 10.10.0.1 -p 5203 -t 28 -P 2 -J'",
            "FLOODER CMD C: ssh alpine@10.10.0.14 'sleep 12; iperf3 -c 10.10.0.1 -p 5204 -t 28 -P 2 -J'",
            "FLOODER CMD D: ssh alpine@10.10.0.16 'sleep 18; iperf3 -c 10.10.0.1 -p 5206 -t 22 -P 2 -J'",
            "REF CMD V1: ssh alpine@10.10.0.11 'iperf3 -c 10.10.0.1 -p 5201 -t 10 -b 9G -J' (both together, 2 runs)",
            "REF CMD V2: ssh alpine@10.10.0.15 'iperf3 -c 10.10.0.1 -p 5205 -t 10 -b 9G -J' (both together, 2 runs)"}
    assert want <= set(lines), want - set(lines)
    assert "EXPECTED DURATION: 13 slots/repeat, ~11.7 min/repeat, 6 repeats + 5 x 600 s rest ~ 2.00 h" in log
    assert "SUMMARY" in log and all(f"{c[0]:7s} {c[1]:>12s} {REPS:4d}" in log for c in configs)
    assert f"alone   {'na':>12s} {REPS * 7:4d}" in log
    assert all(refs == REFS for _, _, refs in calls), "run() must get both references"
    assert "TICK WARNINGS (mean tick > 0.3 s): 0" in log and "WARNING: rep" not in log
    rows = open(exp39.CSV).read().splitlines()
    assert rows[0] == exp39.HEADER and len(rows) == 1 + REPS * 13
    assert all(len(r.split(",")) == len(rows[0].split(",")) for r in rows), "CSV row width"
    assert rows[1].endswith(",0.2500,0.3500,9.000,9.000"), rows[1]
    tsr = open(exp39.TS_CSV).read().splitlines()
    assert tsr[0] == exp39.TS_HEADER and all(len(r.split(",")) == len(tsr[0].split(",")) for r in tsr), "ts width"
    print("CHECK 3 OK: log has the exact victim (9G, one stream), flooder (2 streams, sleeps 0/6/12/18, -t 28/28/28/22) "
          "and reference commands; run() gets both references; CSV/ts row widths match their headers")
    print("  CSV header: " + rows[0])
    print("  ts header:  " + tsr[0])

    _, log_w, _, _ = run_main_dry(slow={("lqr", "85_4")})
    wl = [l for l in log_w.splitlines() if l.startswith("WARNING: rep")]
    assert len(wl) == REPS and all("lqr 85_4: mean control tick 0.310 s > 0.3 s" in l for l in wl), wl
    assert f"TICK WARNINGS (mean tick > 0.3 s): {REPS}" in log_w
    print(f"CHECK 4 OK: a slot with mean tick 0.31 s gets a WARNING line ({REPS} of {REPS * 13} slots here) and the "
          f"end-of-run count; 0.25 s gets none. Example: {wl[0]!r}")

    # exp39: core count, vfrot ceiling note, x0.67 stability rows, per-repeat and rep-1 headroom, summary headroom
    assert "host CPUs: nproc 6, os.cpu_count() 6;" in log
    assert ("vfrot NOTE: budget start 10000 and ceiling 16000 Mbit/s kept from exp38 (4 vCPU) for comparability; "
            "with more CPU room on 6 vCPU the 16000 Mbit/s ceiling could become binding") in log
    for p in ("79_4", "85_4", "90_4"):
        assert f"LQR {p}: target {p[:2]}% host CPU on 6 CPUs (nproc), RWT 4," in log
        rows_s = [l for l in lines if l.startswith(f"LQR {p}: plant gain x")]
        assert [l.split()[4].rstrip(":") for l in rows_s] == ["x0.33", "x0.5", "x0.67", "x2", "x3"], rows_s
        assert all(l.endswith("stable yes") for l in rows_s), rows_s
    hr = "lqr 79_4 79 -> +29.0, lqr 85_4 85 -> +35.0, lqr 90_4 90 -> +40.0"
    blk = [i for i, l in enumerate(lines) if l.startswith("LQR headroom over measured base CPU")]
    assert len(blk) == 1 and lines[blk[0]] == ("LQR headroom over measured base CPU (rep 1, base_cpu 50.0%, 6 CPUs): "
                                               + hr + " points; compare +29/+35 at base 50% (exp34-37)"), blk
    i_ref1 = next(i for i, l in enumerate(lines) if l.startswith("REFERENCE rep 1:"))
    i_slot0 = next(i for i, l in enumerate(lines) if l.startswith("  1    0 alone"))
    assert i_ref1 < blk[0] < i_slot0, (i_ref1, blk[0], i_slot0)
    tl = [l for l in lines if l.startswith("LQR TARGETS rep")]
    assert tl == [f"LQR TARGETS rep {r}: base_cpu 50.0% on 6 CPUs; target -> headroom: {hr}" for r in range(1, 7)], tl
    _, log_b, _, _ = run_main_dry(base_cpus=[51.0, 49.5, 52.0, 50.0, 50.5, 48.0])
    tb = [l for l in log_b.splitlines() if l.startswith("LQR TARGETS rep")]
    assert tb[1] == ("LQR TARGETS rep 2: base_cpu 49.5% on 6 CPUs; target -> headroom: "
                     "lqr 79_4 79 -> +29.5, lqr 85_4 85 -> +35.5, lqr 90_4 90 -> +40.5"), tb
    assert "(rep 1, base_cpu 51.0%, 6 CPUs): lqr 79_4 79 -> +28.0, lqr 85_4 85 -> +34.0, lqr 90_4 90 -> +39.0" in log_b
    assert "rep 6 = V1 9.000/V2 9.000 (base_cpu 48.0%; lqr 79_4 79 -> +31.0, lqr 85_4 85 -> +37.0, " \
           "lqr 90_4 90 -> +42.0)" in log_b
    print("CHECK 4b OK: header logs nproc/os.cpu_count, the vfrot ceiling note (B0 10000, BMAX 16000 unchanged) and "
          "for each of 79_4/85_4/90_4 the stability rows x0.33/x0.5/x0.67/x2/x3 (all stable); per-repeat LQR TARGETS "
          "line with that repeat's base_cpu; the rep-1 headroom block right after REFERENCE rep 1, before slot 0, and "
          "per-repeat headroom in the final REFERENCES line. Example: " + repr(lines[blk[0]]))
    for l in lines:
        if l.startswith("LQR 79_4: a=") or l.startswith("LQR 79_4: plant gain"):
            print("  " + l)

    # exp39: nproc guard (before sudo, before any output file is created)
    for fake in ((4, 4), (None, 6), (8, 8)):
        d = tempfile.mkdtemp(prefix="test_exp39_np_")
        redirect_outputs(d)
        real_hc, real_sudo = exp39.host_cpus, exp39.sudo
        exp39.host_cpus = lambda _f=fake: _f
        exp39.sudo = lambda args: (_ for _ in ()).throw(AssertionError("sudo called before the nproc guard"))
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                exp39.main()
            raise AssertionError(f"main() started with nproc {fake}")
        except SystemExit as e:
            nmsg = str(e)
        finally:
            exp39.host_cpus, exp39.sudo = real_hc, real_sudo
        assert nmsg.startswith(f"REFUSING TO START: nproc = {fake[0]} (os.cpu_count() = {fake[1]}), exp39 requires "
                               "exactly 6") and "resized from 4 to 6 vCPU" in nmsg, nmsg
        assert os.listdir(d) == [], os.listdir(d)
    print(f"CHECK 4c OK: main() refuses with nproc 4 / unknown / 8 before sudo and before creating any file: {nmsg!r}")
    print(f"  real host: host_cpus() = {exp39.host_cpus()}")

    # ================= part 2: the real run() =================
    for m, p in configs + [ALONE]:
        _, _, ev = simulate_run(m, p, 1)
        ssh_ev = [e for e in ev if e[0] == "ssh"]
        fl = {e[1]: e[2] for e in ssh_ev if e[1] in exp39.FLOODERS}
        vc = {e[1]: e[2] for e in ssh_ev if e[1] in exp39.VICTIMS}
        assert vc == {"V1": "iperf3 -c 10.10.0.1 -p 5201 -t 40 -b 9G -J",
                      "V2": "iperf3 -c 10.10.0.1 -p 5205 -t 40 -b 9G -J"}, vc
        if m == "alone":
            assert not fl, fl
        else:
            assert fl == {g: l.split("'")[1] for g, l in
                          ((l.split()[2][0], l) for l in sorted(want) if l.startswith("FLOODER CMD"))}, fl
            tv = [e[3] for e in ssh_ev if e[1] in exp39.VICTIMS]
            tf = [e[3] for e in ssh_ev if e[1] in exp39.FLOODERS]
            assert max(tf) < min(tv) and all(0.5 <= t - tf[0] < 0.6 for t in tv), (tf, tv)
    print("CHECK 5 OK: run() launches V1 and V2 together 0.5 s after the four flooders (alone: victims only), with "
          "the exact commands")

    for p in ("2500", "3500"):
        _, ts, ev = simulate_run("static", p, 2)
        sets = [e for e in ev if e[0] == "set"]
        first_read = next(i for i, e in enumerate(ev) if e[0] == "read")
        assert sorted(e[1] for e in sets) == [f"cfg{g}" for g in exp39.FLOODERS], sets
        assert all(ev.index(e) < first_read for e in sets) and all(e[2] == int(float(p) * 1e6 / 8) for e in sets)
        assert all(row[col("capA_mbit"):col("capD_mbit") + 1] == (float(p),) * 4 for row in ts)
    print("CHECK 6 OK: static 2500/3500 write each of the 4 flooder caps exactly once, before the first counter "
          "read; victims are never capped; caps constant in every tick row (10 / 14 Gbit/s total)")

    for p in ("79_4", "85_4", "90_4"):
        _, ts, _ = simulate_run("lqr", p, 3)
        assert all(all(abs(c - row[col("budget_mbit")] / 4) < 1e-9 for c in row[col("capA_mbit"):col("capD_mbit") + 1])
                   for row in ts)
        assert all(math.isnan(row[col("worst_ratio")]) for row in ts)
    print("CHECK 7 OK: lqr 79_4/85_4/90_4 caps = budget / 4 on every tick (equal split over 4 flooders); worst_ratio not logged")

    vf = ("vfrot", "0.8_1_2_0.94")
    cases = {"both 1.00": {"V1": 1.0, "V2": 1.0}, "V2 0.90": {"V1": 1.0, "V2": 0.90},
             "V1 0.90": {"V1": 0.90, "V2": 1.0}, "both 0.96": {"V1": 0.96, "V2": 0.96}}
    out = {}
    for label, fac in cases.items():
        r, ts, _ = simulate_run(*vf, 4, fac)
        b, wr = [row[col("budget_mbit")] for row in ts], [row[col("worst_ratio")] for row in ts]
        k0 = next(i for i, x in enumerate(wr) if not math.isnan(x))
        assert all(x == 10000.0 for x in b[:k0]), "budget must hold until both 1 s windows are inside the runs"
        late = [x for x, row in zip(wr, ts) if 10 < row[0] < 38]
        assert all(abs(x - min(fac.values())) < 0.02 for x in late), (label, min(late), max(late))
        # budget at the last tick before 39.5 s (flooder time): after the victims stop (40.5 s) their trailing 1 s
        # window falls and vfrot cuts, as in exp37; no flooder sends then
        b_end = [x for x, row in zip(b, ts) if row[0] < 39.5][-1]
        out[label] = (min(b[k0:]), max(b[k0:]), b_end, min(late), max(late))
    assert out["both 1.00"][2] == 16000.0 and out["both 0.96"][2] == 16000.0
    assert out["V2 0.90"][2] == 800.0 and out["V1 0.90"][2] == 800.0
    assert out["V2 0.90"][:3] == out["V1 0.90"][:3], "the worse victim must count, whichever it is"
    print("CHECK 8 OK: vfrot feedback = worst victim (min of v_s/own ref, logged as worst_ratio); budget held at 10000 "
          "until both victims' 1 s windows are inside their runs; end budget: "
          + ", ".join(f"{k} -> {v[2]:g} (worst {v[3]:.3f}-{v[4]:.3f})" for k, v in out.items())
          + "; V1 or V2 degraded give the identical budget trace")

    _, ts, _ = simulate_run(*vf, 5)
    caps = lambda row: dict(zip(exp39.FLOODERS, row[col("capA_mbit"):col("capD_mbit") + 1]))
    holders = {}
    for row in ts:
        hi = [g for g, c in caps(row).items() if c > exp39.CAP_MIN]
        assert len(hi) <= 1, (row[0], caps(row))
        if 18.5 < row[0] < 27.5:
            assert abs(sum(caps(row).values()) - row[col("budget_mbit")]) < 1e-6, (row[0], caps(row))
            holders.setdefault(hi[0], []).append(round(row[0], 2))
        if 0.5 < row[0] < 5.5:
            assert hi == ["A"], (row[0], caps(row))
    assert sorted(holders) == ["A", "B", "C", "D"], holders
    print("CHECK 9 OK: vfrot rotation over 4 holders: never more than one high cap; only A holds before 6 s; in "
          "18-28 s (all four active) the caps sum exactly to the budget (holder B - 3 x 200) and all of A, B, C, D "
          "hold at some tick (first tick: " + ", ".join(f"{g} {v[0]:g} s" for g, v in sorted(holders.items())) + ")")

    for label, fac, want_ok in (("1.00/0.96", {"V1": 1.0, "V2": 0.96}, (1.0, 1.0, 1.0)),
                                ("1.00/0.94", {"V1": 1.0, "V2": 0.94}, (1.0, 0.0, 0.0)),
                                ("0.94/1.00", {"V1": 0.94, "V2": 1.0}, (0.0, 1.0, 0.0))):
        for m, p in configs + [ALONE]:
            r, _, _ = simulate_run(m, p, 6, fac)
            got = (r[rcol("slo_ok_v1")], r[rcol("slo_ok_v2")], r[rcol("slo_ok_worst")])
            assert got == want_ok, (label, m, p, got)
            assert abs(r[rcol("victim1_mean_gbps")] - 9 * fac["V1"]) < 1e-9
    print("CHECK 10 OK: slo_ok per victim against 0.95 x its own reference, slo_ok_worst = the smaller (V1/V2 at "
          "1.00/0.96 -> 1/1/1, 1.00/0.94 -> 1/0/0, 0.94/1.00 -> 0/1/0), for every mode and alone")

    r, ts, _ = simulate_run(*vf, 7)
    tm, tx = r[rcol("tick_mean_s")], r[rcol("tick_max_s")]
    assert 0.25 <= tm < 0.27 and tx < 0.30, (tm, tx)
    assert r[rcol("steal_mean")] == 5.0 and r[rcol("cpu_mean")] == 60.0
    assert ts[0][col("tick_s")] > 0.5, "first tick spans the launch"
    rs, tss, _ = simulate_run(*vf, 7, read_cost=0.07)
    assert rs[rcol("tick_mean_s")] > 0.30 and all(abs(row[col("sample_s")] - 0.42) < 1e-6 for row in tss)
    print(f"CHECK 11 OK: tick length logged per tick (tick_s, sample_s) and per slot (normal reads: mean {tm:.3f} s, "
          f"max {tx:.3f} s; 6 reads x 70 ms: mean {rs[rcol('tick_mean_s')]:.3f} s -> above the 0.30 s warning); "
          "first tick (spans the launch) excluded from the slot stats; steal % = steal / total (5 % here), "
          "cpu 60 %")

    # ================= part 3: reference(), servers, guests, no overwrite, scripts =================
    ref_ev, vbytes, cpu_t = [], {"V1": 0, "V2": 0}, [0]
    real = {k: getattr(exp39, k) for k in ("counters", "cpu", "ssh", "set_cap")}
    exp39.set_cap = lambda d, c: None
    exp39.cpu = lambda: (cpu_t.__setitem__(0, cpu_t[0] + 100) or cpu_t[0], cpu_t[0] // 2, cpu_t[0] // 50)
    exp39.counters = lambda d: {2: vbytes[d["cfg"]]}
    deliver = {"V1": 0.98, "V2": 0.95}

    class RefProc:
        def __init__(self, g, rate): self.g, self.rate = g, rate
        def wait(self):
            ref_ev.append(("wait", self.g)); vbytes[self.g] += int(deliver[self.g] * self.rate * exp39.REF_T * 1e9 / 8)
        def poll(self): return 0

    def ref_ssh(guest, cmd, out):
        ref_ev.append(("ssh", guest, cmd)); rate = parse_cmd(cmd)[2]
        out.write(json.dumps({"end": {"sum_received": {"bytes": 1, "bits_per_second": rate * 1e9}}})); out.flush()
        return RefProc(guest, rate)

    exp39.ssh = ref_ssh
    real_sleep, time.sleep = time.sleep, lambda s: None
    redirect_outputs(tempfile.mkdtemp(prefix="test_exp39_ref_"))
    exp39.IF.clear()
    exp39.IF.update({g: {"cfg": g} for g in exp39.VICTIMS + exp39.FLOODERS})
    with contextlib.redirect_stdout(io.StringIO()):
        refs, base_cpu = exp39.reference(1, [])
        exp39.reference(2, [])
    time.sleep = real_sleep
    for k, v in real.items():
        setattr(exp39, k, v)
    one = [("ssh", "V1", "iperf3 -c 10.10.0.1 -p 5201 -t 10 -b 9G -J"),
           ("ssh", "V2", "iperf3 -c 10.10.0.1 -p 5205 -t 10 -b 9G -J"), ("wait", "V1"), ("wait", "V2")]
    assert ref_ev == one * 4, ref_ev
    assert abs(refs["V1"] - 8.82) < 1e-6 and abs(refs["V2"] - 8.55) < 1e-6 and base_cpu == 50.0, refs
    rows = [l for l in open(exp39.REF_FILE) if not l.startswith("#")]
    assert [l.split(", ")[:4:2] for l in rows] == [["1", "V1"], ["1", "V2"], ["2", "V1"], ["2", "V2"]], rows
    print(f"CHECK 12 OK: reference() launches V1 and V2 together (both ssh before any wait), 2 x 10 s, returns each "
          f"victim's own median (here {refs['V1']:.2f}/{refs['V2']:.2f}) and writes one ref_victim39 row per victim "
          f"per repeat; steal column: {rows[0].split(', ')[6]} %")

    real_run = subprocess.run
    full = [5201, 5202, 5203, 5204, 5205, 5206]
    ss = lambda ports: "\n".join(f"LISTEN 0 5 *:{p} *:*" for p in ports)
    msgs = []
    for miss in ([5206], [5205, 5206]):
        subprocess.run = lambda *a, _p=[p for p in full if p not in miss], **k: subprocess.CompletedProcess(a, 0, ss(_p), "")
        try:
            exp39.check_servers(); raise AssertionError(f"missing {miss} not detected")
        except SystemExit as e:
            assert ("port(s) " + ", ".join(map(str, miss))) in str(e), e
            assert all(f"iperf3 -s -D -p {p}" in str(e) for p in miss), e
            msgs.append(str(e))
    subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a, 0, ss(full), "")
    with contextlib.redirect_stdout(io.StringIO()) as o:
        exp39.check_servers()
    subprocess.run = real_run
    assert "ports 5201, 5202, 5203, 5204, 5205, 5206" in o.getvalue()
    print(f"CHECK 13 OK: check_servers() requires 5201-5206 and prints the exact start command: {msgs[0]!r}")

    seen_ssh = []
    def guest_run(cmd, **k):
        seen_ssh.append(cmd)
        ok = "alpine@10.10.0.16" not in cmd
        return subprocess.CompletedProcess(cmd, 0 if ok else 255, "/usr/bin/iperf3\n" if ok else "", "")
    subprocess.run = guest_run
    try:
        exp39.check_guests(); raise AssertionError("unreachable tenant6 not detected")
    except SystemExit as e:
        gmsg = str(e)
    finally:
        subprocess.run = real_run
    assert "tenant6 (10.10.0.16) -- create it with: sudo bash make_tenant6.sh" in gmsg and "tenant5" not in gmsg, gmsg
    assert len(seen_ssh) == 6 and all(c[:5] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
                                      and c[-1] == "command -v iperf3" for c in seen_ssh), seen_ssh
    real_sudo = exp39.sudo
    def no_dom(args):
        raise subprocess.CalledProcessError(1, args, "", "error: failed to get domain 'tenant5'\n")
    exp39.sudo = no_dom
    try:
        exp39.vnet("tenant5"); raise AssertionError("missing domain not detected")
    except SystemExit as e:
        vmsg = str(e)
    finally:
        exp39.sudo = real_sudo
    assert "failed to get domain 'tenant5'" in vmsg and "sudo bash make_tenant5.sh" in vmsg, vmsg
    print(f"CHECK 14 OK: check_guests() ssh-checks all 6 guests non-interactively ({gmsg!r}); vnet() on an undefined "
          f"domain: {vmsg!r}")

    for f in OUTS:
        d = tempfile.mkdtemp(prefix="test_exp39_ow_")
        redirect_outputs(d)
        open(os.path.join(d, f), "w").write("KEEP\n")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                exp39.main()
            raise AssertionError(f"main() started although {f} exists")
        except SystemExit as e:
            assert "REFUSING TO START" in str(e) and f in str(e), e
        assert open(os.path.join(d, f)).read() == "KEEP\n" and os.listdir(d) == [f], os.listdir(d)
        shutil.copy(os.path.join(HERE, "run39.sh"), d)
        p = real_run(["sh", os.path.join(d, "run39.sh")], capture_output=True, text=True)
        assert p.returncode == 1 and p.stdout.strip() == (f"REFUSING TO START: {f} already exists (move or delete "
                                                          "it first; exp39 never appends)"), (p.returncode, p.stdout)
        assert open(os.path.join(d, f)).read() == "KEEP\n" and sorted(os.listdir(d)) == sorted([f, "run39.sh"])
    print("CHECK 15 OK: with any one of " + ", ".join(OUTS) + " present, main() and run39.sh refuse to start and "
          "leave it unchanged (no other file created)")

    # run39.sh port check, with fake ss / sudo / python3 / nohup first on PATH (they only record that they ran)
    d = tempfile.mkdtemp(prefix="test_exp39_sh_")
    shutil.copy(os.path.join(HERE, "run39.sh"), d)
    bindir = os.path.join(d, "bin"); os.mkdir(bindir)
    for tool, body in (("ss", "printf '%s\\n' " + " ".join(f"'LISTEN 0 5 *:{p} *:*'" for p in full[:5])),
                       ("nproc", "echo 6"),
                       ("sudo", f"echo sudo >> {d}/MARK; exit 1"), ("python3", f"echo python3 >> {d}/MARK; exit 1"),
                       ("nohup", f"echo nohup >> {d}/MARK; exit 1")):
        with open(os.path.join(bindir, tool), "w") as fh:
            fh.write(f"#!/bin/sh\n{body}\n")
        os.chmod(os.path.join(bindir, tool), 0o755)
    p = real_run(["sh", os.path.join(d, "run39.sh")], capture_output=True, text=True,
                 env=dict(os.environ, PATH=bindir + ":" + os.environ["PATH"]))
    assert p.returncode == 1 and p.stdout.strip() == ("host CPUs: 6\nno iperf3 server listening on host port 5206; "
                                                      "start it first: iperf3 -s -D -p 5206"), (p.returncode, p.stdout, p.stderr)
    assert not os.path.exists(os.path.join(d, "MARK")), open(os.path.join(d, "MARK")).read()
    print(f"CHECK 16 OK: run39.sh with 5206 missing prints {p.stdout.strip()!r} and exits before python3/sudo/nohup")
    with open(os.path.join(bindir, "nproc"), "w") as fh:
        fh.write("#!/bin/sh\necho 4\n")
    p = real_run(["sh", os.path.join(d, "run39.sh")], capture_output=True, text=True,
                 env=dict(os.environ, PATH=bindir + ":" + os.environ["PATH"]))
    assert p.returncode == 1 and p.stdout.strip() == ("REFUSING TO START: nproc = 4, exp39 requires exactly 6 (rerun of "
                                                      "exp38 after the resize from 4 to 6 vCPU)"), (p.returncode, p.stdout)
    assert not os.path.exists(os.path.join(d, "MARK")), open(os.path.join(d, "MARK")).read()
    print(f"CHECK 16b OK: run39.sh with nproc 4 prints {p.stdout.strip()!r} and exits before ss/python3/sudo/nohup")

    for n in (5, 6):
        s = open(os.path.join(HERE, f"make_tenant{n}.sh")).read()
        for line in (f"NAME=tenant{n}", f"IP=10.10.0.1{n}", f"MAC=52:54:00:10:00:1{n} ", f"EXPECT_DEV=vnet{n - 1} ",
                     f'META{n}="$TB/meta-data{n}"', f'SEED="$IMG_DIR/seed{n}.iso"', "SRC=tenant3", "VCPUS=1",
                     "MEM_KIB=524288"):
            assert line in s, (n, line)
        for bad in ("tenant4", "10.10.0.14", "52:54:00:10:00:14", "seed4", "data4", "config4", "META4"):
            assert bad not in s, (n, bad)
        assert real_run(["bash", "-n", os.path.join(HERE, f"make_tenant{n}.sh")]).returncode == 0
    print("CHECK 17 OK: make_tenant5.sh / make_tenant6.sh: names, IPs .15/.16, MACs :15/:16, seed/cloud-init files "
          "5/6, expected vnet4/vnet5 (informational), clone source tenant3, 1 vCPU, 512 MiB; no tenant4 leftovers; "
          "bash -n OK (not executed)")
    print("ALL TESTS PASSED")
