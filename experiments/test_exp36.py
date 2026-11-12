#!/usr/bin/env python3
# Pure-python self-test for exp36.py: no VM, no sudo, no network, no traffic, no sleeping.
# Part 1 runs the REAL exp36.main() for REPS repeats with every side effect stubbed (sudo, libvirt/bpftool,
# iperf3 servers, reference(), run(), time.sleep) and output files redirected to a temp dir, and checks the
# slot order against an independent reproduction (random.seed(7), per repeat configs[:]; random.shuffle).
# Part 2 runs the REAL exp36.run() tick loop for each static config with simulated host CPU, victim/flooder
# byte counters and clock jitter (random inputs, a few hundred ticks per run); only sudo, ssh, /proc/stat
# and the clock are faked, so the real set_cap() builds the real bpftool map-update arguments.
import contextlib, io, json, os, random, struct, sys, tempfile, time
sys.argv = sys.argv[:1]          # exp36 reads REPS / SCENARIO from argv: use its defaults (SCENARIO steady3)
import exp36

REPS = 8
ALONE = ("alone", "na")
STATIC = {("static", "3500"): 3500.0, ("static", "4500"): 4500.0,
          ("static", "5500"): 5500.0, ("static", "6500"): 6500.0}


def name(c):
    return f"{c[0]} {c[1]}"


def run_main_dry():
    tmp = tempfile.mkdtemp(prefix="test_exp36_")
    exp36.REPS = REPS
    exp36.CSV, exp36.TS_CSV = os.path.join(tmp, "results_dyn36.csv"), os.path.join(tmp, "ts_dyn36.csv")
    exp36.REF_FILE, exp36.LOG = os.path.join(tmp, "ref_victim36.txt"), os.path.join(tmp, "exp36.log")
    exp36.OBJ = os.path.abspath(exp36.__file__)        # any existing file: main() only checks it exists
    calls, rep_now = [], [0]
    saved = {k: getattr(exp36, k) for k in ("sudo", "check_servers", "vnet", "attach", "set_cap", "reference", "run")}
    exp36.sudo = lambda args: ""
    exp36.check_servers = lambda: None
    exp36.vnet = lambda dom: "vnet-" + dom
    exp36.attach = lambda dev, cap: {"dev": dev, "prog": 0, "cfg": dev, "cnt": dev}
    exp36.set_cap = lambda d, mbps: None

    def fake_reference(rep, procs):
        rep_now[0] = rep
        return 18.0, 50.0

    def fake_run(mode, param, ref, base_cpu, procs, scheds=None):
        calls.append((rep_now[0], (mode, param), scheds))
        return (18.0, 17.5, 1.0) + (1.0, 1.0) * exp36.NF + (0.9, 80.0, 90.0), []

    exp36.reference, exp36.run = fake_reference, fake_run
    real_sleep, time.sleep = time.sleep, lambda s: None
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            exp36.main()
    finally:
        time.sleep = real_sleep
        for k, v in saved.items():
            setattr(exp36, k, v)
    return calls, out.getvalue()


class FakeProc:
    def __init__(self): self.returncode = None
    def poll(self): return None            # never exits: the tick loop ends on its own T + 30 s time bound
    def wait(self): return 0
    def terminate(self): pass


def simulate_run(mode, param, seed):
    # the real exp36.run() with random host CPU, victim rate, flooder rates and tick jitter
    rng = random.Random(seed)
    clock = [1000.0]
    cum = {}                                  # cfg id -> [passed bytes, dropped bytes]
    events = []                               # ("set", cfg, bytes/s) / ("read", cfg) in call order
    cpu_acc = [0, 0]
    real = {k: getattr(exp36, k) for k in ("sudo", "counters", "cpu", "ssh")}
    real_time = (time.monotonic, time.sleep)

    def fake_sudo(args):
        # only bpftool map update reaches sudo inside run() (via the real set_cap)
        assert args[:3] == ["bpftool", "map", "update"], args
        cfg = args[4]
        events.append(("set", cfg, struct.unpack("<Q", bytes(int(x, 16) for x in args[-8:]))[0]))
        return ""

    def fake_counters(d):
        events.append(("read", d["cfg"]))
        c = cum.setdefault(d["cfg"], [0, 0])
        gbit = rng.uniform(0, 25) if d["cfg"] == "cfgV" else rng.uniform(0, 12)   # any rate, any time
        b = int(gbit * 1e9 / 8 * rng.uniform(0.05, 0.4))
        drop = rng.random()
        c[0] += int(b * (1 - drop)); c[1] += int(b * drop)
        return {0: 0, 1: 0, 2: c[0], 3: c[1]}

    def fake_cpu():
        dt = rng.randint(50, 200)
        cpu_acc[0] += dt; cpu_acc[1] += int(dt * rng.uniform(0.0, 1.0))   # host CPU anywhere 0-100 %
        return cpu_acc[0], cpu_acc[1]

    def fake_ssh(guest, cmd, out):
        if guest != "V":
            # steady3: exactly one iperf3 -J client per flooder -> one JSON document, as in exp34
            assert cmd.count("iperf3 ") == 1 and cmd.endswith(" -J"), cmd
            out.write(json.dumps({"end": {"sum_received": {"bytes": 3_000_000_000, "bits_per_second": 1e9}}}))
            out.flush()
        return FakeProc()

    def fake_sleep(s):
        clock[0] += max(0.0, s) + rng.uniform(0.0, 0.08)              # timing jitter

    exp36.sudo, exp36.counters, exp36.cpu, exp36.ssh = fake_sudo, fake_counters, fake_cpu, fake_ssh
    time.monotonic, time.sleep = (lambda: clock[0]), fake_sleep
    exp36.IF.clear()
    for g in ["V"] + exp36.FLOODERS:
        exp36.IF[g] = {"dev": f"vnet{g}", "prog": 0, "cfg": f"cfg{g}", "cnt": f"cnt{g}"}
    exp36.last_cap.clear()
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            r, ts = exp36.run(mode, param, 18.0, 50.0, [], None)
    finally:
        time.monotonic, time.sleep = real_time
        for k, v in real.items():
            setattr(exp36, k, v)
    return r, ts, events, out.getvalue()


if __name__ == "__main__":
    configs = list(exp36.CONFIGS)
    assert configs == [("static", "3500"), ("static", "4500"), ("static", "5500"), ("static", "6500"),
                       ("lqr", "79_4"), ("lqr", "85_4"), ("vfrot", "0.8_1_2_0.94")], configs
    assert exp36.SCENARIO == "steady3", exp36.SCENARIO
    n_slots, per_rep, total = exp36.expected_duration(len(configs), REPS)
    print(f"SCENARIO {exp36.SCENARIO}; CONFIGS ({len(configs)}): " + ", ".join(name(c) for c in configs))
    print(f"expected_duration: {n_slots} slots/repeat, {per_rep / 60:.2f} min/repeat; {REPS} repeats ~ {total / 3600:.2f} h")

    calls, log = run_main_dry()
    seqs = [[c for r, c, _ in calls if r == rep] for rep in range(1, REPS + 1)]

    print("\n--- exp36.log header lines (as main() prints them) ---")
    print("\n".join(l for l in log.splitlines()
                    if l.startswith(("SCENARIO", "CONFIGS", "TOKEN", "STATIC", "EXPECTED"))))

    print("\nconfig order per repeat (slot -> config):")
    w = max(len(name(c)) for c in configs + [ALONE])
    print("slot " + " ".join(f"{'r' + str(r):{w}s}" for r in range(1, REPS + 1)))
    for slot in range(n_slots):
        print(f"{slot:4d} " + " ".join(f"{name(seq[slot]):{w}s}" for seq in seqs))
    print("\nslot position of each config per repeat:")
    print(f"{'config':{w}s}  " + " ".join(f"r{r:<2d}" for r in range(1, REPS + 1)))
    for c in configs:
        print(f"{name(c):{w}s}  " + " ".join(f"{seq.index(c):3d}" for seq in seqs))

    # ---- schedule checks ----
    for rep, seq in enumerate(seqs, 1):
        assert len(seq) == 15 == n_slots, (rep, len(seq))
        assert sorted(c for c in seq if c != ALONE) == sorted(configs), f"repeat {rep}: configs not each exactly once"
    print("\nCHECK 1 OK: every repeat has 15 slots and each of the 7 configs exactly once")

    for rep, seq in enumerate(seqs, 1):
        assert seq[0::2] == [ALONE] * 8, f"repeat {rep}: alone not at every even slot"
        assert all(c != ALONE for c in seq[1::2]), f"repeat {rep}: alone at an odd slot"
    print("CHECK 2 OK: alone at slots 0,2,...,14 -> before the first config, between every pair, after the last")

    random.seed(7)
    for rep, seq in enumerate(seqs, 1):
        order = configs[:]; random.shuffle(order)
        assert seq == [ALONE] + [x for c in order for x in (c, ALONE)], f"repeat {rep}: order differs"
    print("CHECK 3 OK: main()'s order equals an independent random.seed(7) shuffle per repeat")

    fixed = [name(c) for c in configs if len({seq.index(c) for seq in seqs}) == 1]
    assert not fixed, f"configs always in the same slot: {fixed}"
    print(f"CHECK 4 OK: no config in the same slot in all {REPS} repeats (distinct positions: "
          + ", ".join(f"{name(c)} {len({seq.index(c) for seq in seqs})}" for c in configs) + ")")

    assert all(s is None for _, _, s in calls), "steady3 must not pass burst schedules"
    assert "EXPECTED DURATION: 15 slots/repeat" in log
    assert "SUMMARY" in log and all(f"{c[0]:7s} {c[1]:>12s} {REPS:4d}" in log for c in configs)
    assert f"alone   {'na':>12s} {REPS * 8:4d}" in log
    print("CHECK 5 OK: steady3 passes no burst schedule; EXPECTED DURATION 15 slots/repeat; SUMMARY has the "
          f"7 configs (n={REPS}) plus alone (n={REPS * 8})")

    # ---- static mode: the real run() tick loop with random inputs ----
    n_ticks = 0
    for (m, p), cap in STATIC.items():
        line = (f"STATIC {p}: per-flooder cap {cap:g} Mbit/s (burst {cap / 8 * 0.005:.2f} MB), set once at the start "
                f"of each slot on A = tenant2 vnet-tenant2 (cfg map id vnet-tenant2), B = tenant3 vnet-tenant3 "
                f"(cfg map id vnet-tenant3), C = tenant4 vnet-tenant4 (cfg map id vnet-tenant4); victim tenant1 "
                f"vnet-tenant1 unlimited")
        assert line in log.splitlines(), line
        for seed in range(3):
            r, ts, ev, out = simulate_run(m, p, 100 * int(p) + seed)
            sets = [e for e in ev if e[0] == "set"]
            first_read = next(i for i, e in enumerate(ev) if e[0] == "read")
            # exactly one write per flooder interface, all before the first counter read (i.e. before any
            # flooder or the victim starts), none to the victim, value = cap in bytes/s
            assert sorted(e[1] for e in sets) == ["cfgA", "cfgB", "cfgC"], sets
            assert all(ev.index(e) < first_read for e in sets), "static cap written after the run started"
            assert all(e[2] == int(cap * 1e6 / 8) for e in sets), sets
            assert all(e[2] * 8 / 1e6 == cap for e in sets), sets
            assert len(ts) >= 250, len(ts)
            for row in ts:
                assert list(row[2:2 + exp36.NF]) == [cap] * exp36.NF, row   # caps in effect at every tick
            assert out.splitlines()[0] == (f"static {p}: cap set once for the whole run on A vnetA (cfg map id "
                                           f"cfgA) {cap:g} Mbit/s, B vnetB (cfg map id cfgB) {cap:g} Mbit/s, "
                                           f"C vnetC (cfg map id cfgC) {cap:g} Mbit/s"), out
            iperf = r[3:3 + 2 * exp36.NF:2]
            assert all(x == x for x in iperf), f"steady3 iperf column nan: {r}"
            n_ticks += len(ts)
    print(f"CHECK 6 OK: static over {n_ticks} simulated control ticks (4 configs x 3 seeds, random host CPU 0-100 %, "
          f"victim 0-25 Gbit/s, flooders 0-12 Gbit/s, tick jitter): the per-flooder cap is identical on EVERY tick; "
          f"one bpftool map update per flooder interface (none to the victim), all before the run starts, none after")
    print("CHECK 7 OK: static 3500/4500/5500/6500 write exactly " + ", ".join(
        f"{int(c * 1e6 / 8)} B/s = {c:g} Mbit/s" for c in STATIC.values()) + " to A, B and C; "
        "the per-run log line states the cap per flooder interface; steady3 iperf columns are not nan")

    # positive control: the same harness DOES see lqr move its caps (so check 6 is not vacuous)
    r, ts, ev, _ = simulate_run("lqr", "79_4", 1)
    assert len({tuple(row[2:2 + exp36.NF]) for row in ts}) > 10
    assert len([e for e in ev if e[0] == "set"]) > 3 * exp36.NF
    print(f"CHECK 8 OK: positive control, lqr 79_4 in the same harness changes its caps "
          f"({len({tuple(row[2:5]) for row in ts})} distinct cap vectors over {len(ts)} ticks)")

    # old Gbit/s-style or out-of-range static params are refused before anything starts
    for bad in ("4.5", "0", "70001", "x"):
        exp36.CONFIGS = [("static", bad)]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                exp36.main()
            raise AssertionError(f"static {bad} accepted")
        except SystemExit as e:
            assert "static param" in str(e), e
    exp36.CONFIGS = configs
    print(f"CHECK 9 OK: static params outside [{exp36.CAP_MIN:g}, {exp36.CAP_MAX:g}] Mbit/s (e.g. 4.5, 0, 70001, x) "
          "are refused at start-up")
    print("ALL TESTS PASSED")
