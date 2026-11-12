#!/usr/bin/env python3
# Pure-python self-test for exp37.py: no VM, no sudo, no network, no traffic, no sleeping.
# Part 1 runs the REAL exp37.main() for REPS repeats with every side effect stubbed (sudo, libvirt/bpftool,
# iperf3 servers, reference(), run(), time.sleep) and output files redirected to a temp dir, and checks the
# slot order against an independent reproduction (random.seed(7), per repeat configs[:]; random.shuffle).
# Part 2 runs the REAL exp37.run() tick loop for every config and alone with a simulated clock (with jitter),
# host CPU, flooder counters and a victim counter that follows a given offered-rate profile; only sudo, ssh,
# /proc/stat and the clock are faked. Part 3 runs the REAL reference(), check_servers() and the output-file
# refusals of main() and run37.sh (run37.sh is copied into a temp dir and exits before ss/sudo).
import contextlib, inspect, io, json, math, os, random, shutil, subprocess, sys, tempfile, time
sys.argv = sys.argv[:1]          # exp37 reads REPS / SCENARIO from argv: use its defaults (8, steady3)
import exp37

REPS = 8
ALONE = ("alone", "na")
REFS = {18.0: 18.0, 10.0: 10.0}  # ref18, ref10 used in the simulated runs
OUTS = ("results_dyn37.csv", "ts_dyn37.csv", "ref_victim37.txt", "exp37.log")


def name(c):
    return f"{c[0]} {c[1]}"


def redirect_outputs(tmp):
    exp37.CSV, exp37.TS_CSV = os.path.join(tmp, OUTS[0]), os.path.join(tmp, OUTS[1])
    exp37.REF_FILE, exp37.LOG = os.path.join(tmp, OUTS[2]), os.path.join(tmp, OUTS[3])


def run_main_dry():
    tmp = tempfile.mkdtemp(prefix="test_exp37_")
    redirect_outputs(tmp)
    exp37.OBJ = os.path.abspath(exp37.__file__)        # any existing file: main() only checks it exists
    calls, rep_now = [], [0]
    saved = {k: getattr(exp37, k) for k in ("sudo", "check_servers", "vnet", "attach", "set_cap", "reference", "run")}
    exp37.sudo = lambda args: ""
    exp37.check_servers = lambda: None
    exp37.vnet = lambda dom: "vnet-" + dom
    exp37.attach = lambda dev, cap: {"dev": dev, "prog": 0, "cfg": dev, "cnt": dev}
    exp37.set_cap = lambda d, mbps: None

    def fake_reference(rep, procs):
        rep_now[0] = rep
        return dict(REFS), 50.0

    def fake_run(mode, param, refs, base_cpu, procs, scheds=None):
        calls.append((rep_now[0], (mode, param), scheds, refs))
        row = (1.0, 17.0, 17.5) + (500.0,) * exp37.NF + (1.0,) * exp37.NF + (float("nan"), 1500.0, 10.0)
        return (18.0, 17.5, 1.0) + (1.0, 1.0) * exp37.NF + (0.9, 80.0, 90.0, 1.0, 0.5, 1.0), [row]

    exp37.reference, exp37.run = fake_reference, fake_run
    real_sleep, time.sleep = time.sleep, lambda s: None
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            exp37.main()
    finally:
        time.sleep = real_sleep
        for k, v in saved.items():
            setattr(exp37, k, v)
    return calls, out.getvalue(), tmp


class FakeProc:
    def __init__(self): self.returncode = None
    def poll(self): return None            # never exits: the tick loop ends on its own T + 30 s time bound
    def wait(self): return 0
    def terminate(self): pass


def schedule_of(cmd):
    # (start s, end s, Gbit/s) of one victim iperf3 client command, from its optional sleep, -t and -b
    w = cmd.split()
    delay = float(w[1].rstrip(";")) if w[0] == "sleep" else 0.0
    secs, rate = float(w[w.index("-t") + 1]), float(w[w.index("-b") + 1].rstrip("G"))
    return delay, delay + secs, rate


def cum_gbit(profile, tv):
    # integral (Gbit) of a piecewise-constant profile [(start, end, Gbit/s)] over victim time [0, tv]
    return sum(r * max(0.0, min(tv, e) - s) for s, e, r in profile)


def simulate_run(mode, param, seed, profile=None):
    # the real exp37.run(); the victim counter follows profile (default: the commands run() launches, i.e. the
    # nominal schedule) from the moment the base client is launched; flooders and host CPU random
    rng = random.Random(seed)
    clock = [1000.0]
    cum = {}
    launch, vcmds, events = [None], [], []
    cpu_acc = [0, 0]
    real = {k: getattr(exp37, k) for k in ("sudo", "counters", "cpu", "ssh")}
    real_time = (time.monotonic, time.sleep)

    def fake_sudo(args):
        assert args[:3] == ["bpftool", "map", "update"], args
        events.append(("set", args[4]))
        return ""

    def fake_counters(d):
        events.append(("read", d["cfg"]))
        if d["cfg"] == "cfgV":
            prof = profile if profile is not None else [schedule_of(c) for _, c in vcmds]
            gbit = cum_gbit(prof, clock[0] - launch[0]) if launch[0] is not None else 0.0
            return {0: 0, 1: 0, 2: int(gbit * 1e9 / 8), 3: 0}
        c = cum.setdefault(d["cfg"], [0, 0])
        b = int(rng.uniform(0, 12) * 1e9 / 8 * rng.uniform(0.05, 0.4))
        drop = rng.random()
        c[0] += int(b * (1 - drop)); c[1] += int(b * drop)
        return {0: 0, 1: 0, 2: c[0], 3: c[1]}

    def fake_cpu():
        dt = rng.randint(50, 200)
        cpu_acc[0] += dt; cpu_acc[1] += int(dt * rng.uniform(0.0, 1.0))
        return cpu_acc[0], cpu_acc[1]

    def fake_ssh(guest, cmd, out):
        if guest in ("V", "VX"):
            if guest == "V":
                launch[0] = clock[0]
            vcmds.append((guest, cmd))
        assert cmd.count("iperf3 ") == 1 and cmd.endswith(" -J"), cmd
        out.write(json.dumps({"end": {"sum_received": {"bytes": 3_000_000_000, "bits_per_second": 1e9}}}))
        out.flush()
        return FakeProc()

    def fake_sleep(s):
        clock[0] += max(0.0, s) + rng.uniform(0.0, 0.08)              # timing jitter

    exp37.sudo, exp37.counters, exp37.cpu, exp37.ssh = fake_sudo, fake_counters, fake_cpu, fake_ssh
    time.monotonic, time.sleep = (lambda: clock[0]), fake_sleep
    exp37.IF.clear()
    for g in ["V"] + exp37.FLOODERS:
        exp37.IF[g] = {"dev": f"vnet{g}", "prog": 0, "cfg": f"cfg{g}", "cnt": f"cnt{g}"}
    exp37.last_cap.clear()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            r, ts = exp37.run(mode, param, dict(REFS), 50.0, [], None)
    finally:
        time.monotonic, time.sleep = real_time
        for k, v in real.items():
            setattr(exp37, k, v)
    return r, ts, vcmds, events


def okp(r):
    return r[2], r[6 + 2 * exp37.NF], r[7 + 2 * exp37.NF], r[8 + 2 * exp37.NF]


if __name__ == "__main__":
    configs = list(exp37.CONFIGS)
    assert configs == [("static", "3500"), ("static", "4500"), ("static", "5500"),
                       ("lqr", "79_4"), ("lqr", "85_4"), ("vfrot", "0.8_1_2_0.94")], configs
    assert exp37.SCENARIO == "steady3" and exp37.REPS == 8, (exp37.SCENARIO, exp37.REPS)
    assert (exp37.V_BASE, exp37.V_EXTRA, exp37.SWITCHES, exp37.SLO_GUARD) == (10.0, 8.0, (14, 26), 1.0)
    assert not hasattr(exp37, "VF_SWITCH_LEAD") and not hasattr(exp37, "VICTIM_RATE_GBIT")
    n_slots, per_rep, total = exp37.expected_duration(len(configs), REPS)
    print(f"SCENARIO {exp37.SCENARIO}; CONFIGS ({len(configs)}): " + ", ".join(name(c) for c in configs))
    print(f"expected_duration: {n_slots} slots/repeat, {per_rep / 60:.2f} min/repeat; {REPS} repeats ~ {total / 3600:.2f} h")

    # ================= part 1: main() slot order and log =================
    calls, log, tmp = run_main_dry()
    seqs = [[c for r, c, _, _ in calls if r == rep] for rep in range(1, REPS + 1)]
    print("\n--- exp37.log header lines (as main() prints them) ---")
    print("\n".join(l for l in log.splitlines()
                    if l.startswith(("SCENARIO", "CONFIGS", "VICTIM", "REF CMD", "vfrot:", "EXPECTED"))))
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
    print("CHECK 2 OK: alone at slots 0,2,...,12 (before, between, after the configs); order equals an "
          "independent random.seed(7) shuffle per repeat")

    fixed = [name(c) for c in configs if len({seq.index(c) for seq in seqs}) == 1]
    assert not fixed, f"configs always in the same slot: {fixed}"
    print(f"CHECK 3 OK: no config in the same slot in all {REPS} repeats (distinct positions: "
          + ", ".join(f"{name(c)} {len({seq.index(c) for seq in seqs})}" for c in configs) + ")")

    lines = log.splitlines()
    assert "vfrot: SLO target switches at nominal 14/26 s = tenant-declared SLO update; static/lqr never see it" in lines
    want = {"VICTIM CMD base: ssh alpine@10.10.0.11 'iperf3 -c 10.10.0.1 -p 5201 -t 40 -b 10G -J'",
            "VICTIM CMD extra1: ssh alpine@10.10.0.11 'iperf3 -c 10.10.0.1 -p 5205 -t 14 -b 8G -J'",
            "VICTIM CMD extra2: ssh alpine@10.10.0.11 'sleep 26; iperf3 -c 10.10.0.1 -p 5205 -t 14 -b 8G -J'",
            "REF CMD ref18 base: ssh alpine@10.10.0.11 'iperf3 -c 10.10.0.1 -p 5201 -t 10 -b 10G -J'",
            "REF CMD ref18 extra: ssh alpine@10.10.0.11 'iperf3 -c 10.10.0.1 -p 5205 -t 10 -b 8G -J'",
            "REF CMD ref10 base: ssh alpine@10.10.0.11 'iperf3 -c 10.10.0.1 -p 5201 -t 10 -b 10G -J'"}
    assert want <= set(lines), want - set(lines)
    assert "EXPECTED DURATION: 13 slots/repeat" in log
    assert "SUMMARY" in log and all(f"{c[0]:7s} {c[1]:>12s} {REPS:4d}" in log for c in configs)
    assert f"alone   {'na':>12s} {REPS * 7:4d}" in log
    assert all(refs == REFS for _, _, _, refs in calls), "run() must get both references"
    hdr, row = open(exp37.CSV).read().splitlines()[:2]
    assert hdr == exp37.HEADER and len(row.split(",")) == len(hdr.split(",")), (hdr, row)
    assert row.endswith(",1.000,0.500,1.000,18.000,10.000"), row
    tsh, tsr = open(exp37.TS_CSV).read().splitlines()[:2]
    assert tsh == exp37.TS_HEADER and len(tsr.split(",")) == len(tsh.split(",")), (tsh, tsr)
    print("CHECK 4 OK: log has the vfrot tenant-declared line and the exact base/extra1/extra2 and ref18/ref10 "
          "commands; run() gets {18: ref18, 10: ref10}; CSV ends slo_ok_p1,p2,p3,ref18,ref10; ts row widths match")

    # ================= part 2: the real run() =================
    # victim schedule as launched by run()
    _, ts, vcmds, _ = simulate_run("static", "3500", 1)
    assert [g for g, _ in vcmds] == ["V", "VX", "VX"], vcmds
    sch = [schedule_of(c) for _, c in vcmds]
    assert sch == [(0.0, 40.0, 10.0), (0.0, 14.0, 8.0), (26.0, 40.0, 8.0)], sch
    for k in range(0, 400):
        tv = k / 10
        base = sum(r for s, e, r in sch[:1] if s <= tv < e)
        extra = sum(r for s, e, r in sch[1:] if s <= tv < e)
        assert base == 10.0, tv
        assert extra == (8.0 if tv < 14 or tv >= 26 else 0.0), tv
        assert exp37.victim_offered(tv) == base + extra, tv
    assert exp37.victim_offered(-0.01) == exp37.victim_offered(40.0) == 0.0
    print("CHECK 5 OK: run() launches base 'iperf3 -p 5201 -t 40 -b 10G' + extra 'iperf3 -p 5205 -t 14 -b 8G' + "
          "'sleep 26; iperf3 -p 5205 -t 14 -b 8G' together: base on [0,40), extra on [0,14) and [26,40) only "
          "(checked every 0.1 s)")

    # vfrot reference function
    for tv in [-1, 0, 5, 13.99, 13.999999, 14, 14.001, 20, 25.99, 25.999999, 26, 26.001, 35, 39.99, 45]:
        assert exp37.vfrot_ref(REFS, tv) == (10.0 if 14 <= tv < 26 else 18.0), tv
    assert exp37.vfrot_ref({18.0: 17.3, 10.0: 9.8}, 20) == 9.8 and exp37.vfrot_ref({18.0: 17.3, 10.0: 9.8}, 30) == 17.3
    real_vref, vref_calls = exp37.vfrot_ref, {}

    def counting_vref(refs, tv, _m=[None]):
        vref_calls[_m[0]] = vref_calls.get(_m[0], 0) + 1
        return real_vref(refs, tv)
    exp37.vfrot_ref = counting_vref
    for m, p in configs + [ALONE]:
        counting_vref.__defaults__[0][0] = m
        _, ts, _, _ = simulate_run(m, p, 7)
        v_ref_col = [row[-1] for row in ts]
        if m == "vfrot":
            # logged reference: ref10 on ticks whose victim time is in [14, 26), else ref18
            tv = [row[0] - exp37.OFFSET for row in ts]
            assert all(v == (10.0 if 14 <= t < 26 else 18.0) for t, v in zip(tv, v_ref_col) if abs(t - 14) > 0.1
                       and abs(t - 26) > 0.1), "vfrot v_ref column"
        else:
            assert all(math.isnan(v) for v in v_ref_col), (m, "v_ref logged")
    exp37.vfrot_ref = real_vref
    assert set(vref_calls) == {"vfrot"} and vref_calls["vfrot"] > 250, vref_calls
    print(f"CHECK 6 OK: vfrot_ref = ref10 for 14 <= t < 26 and ref18 otherwise (edges 13.999999/14/25.999999/26); "
          f"in the real run() only vfrot calls it ({vref_calls['vfrot']} calls), static/lqr/alone never "
          f"(their v_ref column is nan)")

    # SLO_GUARD: same dropped windows for every mode
    assert list(inspect.signature(exp37.slo_windows).parameters) == ["vfirst", "tsw"]
    real_sw, seen = exp37.slo_windows, []
    exp37.slo_windows = lambda vfirst, tsw: seen.append((vfirst - tsw, real_sw(vfirst, tsw))) or seen[-1][1]
    drops = {}
    for seed in (3, 4, 5):
        for m, p in configs + [ALONE]:
            seen.clear()
            simulate_run(m, p, seed)
            (lag, sw), = seen
            drops[(seed, m, p)] = (round(lag, 9), tuple(j for j, (_, _, ph) in enumerate(sw) if ph is None))
        per_seed = {v for k, v in drops.items() if k[0] == seed}
        assert len(per_seed) == 1, f"seed {seed}: dropped windows differ between modes: {per_seed}"
        (lag, dropped), = per_seed
        assert 0 < lag < 1 and dropped == (12, 13, 14, 24, 25, 26), (lag, dropped)
    exp37.slo_windows = real_sw
    print("CHECK 7 OK: slo_windows(vfirst, tsw) takes no mode; for 3 seeds the dropped windows are identical for "
          "static x3, lqr x2, vfrot and alone: j = 12,13,14 and 24,25,26 (overlapping 13-15 s and 25-27 s), "
          "33 of 39 windows kept")

    # per-phase slo_ok against the matching reference
    S = [(0, 14, 18.0), (14, 26, 10.0), (26, 40, 18.0)]
    cases = {"nominal x0.97": ([(s, e, 0.97 * r) for s, e, r in S], (1.0, 1.0, 1.0, 1.0)),
             "flat 9.7": ([(0, 40, 9.7)], (9 / 33, 0.0, 1.0, 0.0)),        # >= 0.95 x ref10, < 0.95 x ref18
             "flat 18": ([(0, 40, 18.0)], (1.0, 1.0, 1.0, 1.0)),
             "nominal x0.93": ([(s, e, 0.93 * r) for s, e, r in S], (0.0, 0.0, 0.0, 0.0))}
    for label, (prof, want_ok) in cases.items():
        for m, p in configs + [ALONE]:
            r, _, _, _ = simulate_run(m, p, 11, prof)
            got = okp(r)
            assert all(abs(a - b) < 1e-9 for a, b in zip(got, want_ok)), (label, m, p, got, want_ok)
    W = [(17.2, 1), (17.0, 1), (9.6, 2), (9.4, 2), (17.2, 3), (9.9, 3), (5.0, None)]
    assert exp37.slo_ok(W, REFS) == (3 / 6, 1 / 2, 1 / 2, 1 / 2), exp37.slo_ok(W, REFS)
    print("CHECK 8 OK: slo_ok_p1/p3 use 0.95 x ref18 and p2 uses 0.95 x ref10 (victim at 0.97 x schedule -> "
          "1/1/1; flat 9.7 Gbit/s -> p1 0, p2 1, p3 0, overall 9/33; 0.93 x schedule -> all 0); dropped windows "
          "do not count; same result for every mode")

    # ================= part 3: reference(), servers, no overwrite =================
    ref_cmds, vbytes, cpu_t = [], [0], [0]
    real = {k: getattr(exp37, k) for k in ("counters", "cpu", "ssh", "set_cap")}
    exp37.set_cap = lambda d, c: None
    exp37.cpu = lambda: (cpu_t.__setitem__(0, cpu_t[0] + 100) or cpu_t[0], cpu_t[0] // 2)
    exp37.counters = lambda d: {2: vbytes[0]}

    def ref_ssh(guest, cmd, out):
        # each client delivers 0.98 x its -b rate over REF_T s on the victim vnet counter
        ref_cmds.append((guest, cmd)); rate = schedule_of(cmd)[2]
        vbytes[0] += int(0.98 * rate * exp37.REF_T * 1e9 / 8)
        out.write(json.dumps({"end": {"sum_received": {"bytes": 1, "bits_per_second": rate * 1e9}}})); out.flush()
        return FakeProc()

    exp37.ssh = ref_ssh
    real_sleep, time.sleep = time.sleep, lambda s: None
    redirect_outputs(tempfile.mkdtemp(prefix="test_exp37_ref_"))
    exp37.IF["V"] = {"cfg": "cfgV"}
    with contextlib.redirect_stdout(io.StringIO()):
        refs, base_cpu = exp37.reference(1, [])
    time.sleep = real_sleep
    for k, v in real.items():
        setattr(exp37, k, v)
    assert ref_cmds == [("V", "iperf3 -c 10.10.0.1 -p 5201 -t 10 -b 10G -J"),
                        ("VX", "iperf3 -c 10.10.0.1 -p 5205 -t 10 -b 8G -J")] * 2 + \
                       [("V", "iperf3 -c 10.10.0.1 -p 5201 -t 10 -b 10G -J")] * 2, ref_cmds
    assert sorted(refs) == [10.0, 18.0] and abs(refs[18.0] - 17.64) < 1e-6 and abs(refs[10.0] - 9.8) < 1e-6, refs
    rows = [l for l in open(exp37.REF_FILE) if not l.startswith("#")]
    assert [l.split(", ")[2] for l in rows] == ["18", "10"], rows
    print(f"CHECK 9 OK: reference() runs ref18 = base 10G + extra 8G together (2 x 10 s) then ref10 = base alone "
          f"(2 x 10 s), returns both (here {refs[18.0]:.2f}/{refs[10.0]:.2f}) and writes one ref_victim37 row each")

    real_run = subprocess.run
    ss_out = "\n".join(f"LISTEN 0 5 *:{p} *:*" for p in (5201, 5202, 5203, 5204))
    subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a, 0, ss_out, "")
    try:
        exp37.check_servers(); raise AssertionError("missing 5205 not detected")
    except SystemExit as e:
        assert "no iperf3 server listening on host port(s) 5205" in str(e) and "iperf3 -s -D -p 5205" in str(e), e
        msg = str(e)
    subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a, 0, ss_out + "\nLISTEN 0 5 *:5205 *:*", "")
    with contextlib.redirect_stdout(io.StringIO()) as o:
        exp37.check_servers()
    subprocess.run = real_run
    assert "ports 5201, 5202, 5203, 5204, 5205" in o.getvalue()
    print(f"CHECK 10 OK: check_servers() aborts without 5205 ({msg!r}) and passes with 5201-5205")

    for f in OUTS:
        d = tempfile.mkdtemp(prefix="test_exp37_ow_")
        redirect_outputs(d)
        open(os.path.join(d, f), "w").write("KEEP\n")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                exp37.main()
            raise AssertionError(f"main() started although {f} exists")
        except SystemExit as e:
            assert "REFUSING TO START" in str(e) and f in str(e), e
        assert open(os.path.join(d, f)).read() == "KEEP\n" and os.listdir(d) == [f], os.listdir(d)
        # run37.sh in a copy dir: must refuse before ss, python or sudo
        shutil.copy(os.path.join(os.path.dirname(os.path.abspath(exp37.__file__)), "run37.sh"), d)
        p = real_run(["sh", os.path.join(d, "run37.sh")], capture_output=True, text=True)
        assert p.returncode == 1 and p.stdout.strip() == (f"REFUSING TO START: {f} already exists (move or delete "
                                                          "it first; exp37 never appends)"), (p.returncode, p.stdout)
        assert open(os.path.join(d, f)).read() == "KEEP\n" and sorted(os.listdir(d)) == sorted([f, "run37.sh"])
    print("CHECK 11 OK: with any one of " + ", ".join(OUTS) + " present, main() and run37.sh refuse to start and "
          "leave it unchanged (no other file created)")
    print("ALL TESTS PASSED")
