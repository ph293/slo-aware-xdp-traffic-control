#!/usr/bin/env python3
# Pure-python self-test for exp35.py: no VM, no sudo, no network, no traffic, no sleeping.
# Runs the REAL exp35.main() for REPS repeats with every side effect stubbed (sudo, libvirt/bpftool,
# iperf3 servers, reference(), run(), time.sleep) and output files redirected to a temp dir, and records
# the exact scheds object main() hands to run() for each slot. Then checks the slot order against an
# independent reproduction (random.seed(7), per repeat order = configs[:]; random.shuffle(order)).
import contextlib, io, os, random, sys, tempfile, time
sys.argv = sys.argv[:1]          # exp35 reads REPS / SCENARIO from argv: use its defaults (SCENARIO burst3)
import exp34, exp35

REPS = 8
ALONE = ("alone", "na")


def name(c):
    return f"{c[0]} {c[1]}"


def run_main_dry():
    tmp = tempfile.mkdtemp(prefix="test_exp35_")
    exp35.REPS = REPS
    exp35.CSV, exp35.TS_CSV = os.path.join(tmp, "results_dyn35.csv"), os.path.join(tmp, "ts_dyn35.csv")
    exp35.REF_FILE, exp35.LOG = os.path.join(tmp, "ref_victim35.txt"), os.path.join(tmp, "exp35.log")
    exp35.OBJ = os.path.abspath(exp35.__file__)        # any existing file: main() only checks it exists
    calls, rep_now = [], [0]
    exp35.sudo = lambda args: ""
    exp35.check_servers = lambda: None
    exp35.vnet = lambda dom: dom
    exp35.attach = lambda dev, cap: {"dev": dev, "prog": 0, "cfg": dev, "cnt": dev}
    exp35.set_cap = lambda d, mbps: None

    def fake_reference(rep, procs):
        rep_now[0] = rep
        return 18.0, 50.0

    def fake_run(mode, param, ref, base_cpu, procs, scheds=None):
        calls.append((rep_now[0], (mode, param), scheds))
        return (18.0, 17.5, 1.0) + (0.0, 1.0) * exp35.NF + (0.9, 80.0, 90.0), []

    exp35.reference, exp35.run = fake_reference, fake_run
    real_sleep, time.sleep = time.sleep, lambda s: None
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            exp35.main()
    finally:
        time.sleep = real_sleep
    return calls, out.getvalue()


if __name__ == "__main__":
    configs = list(exp35.CONFIGS)
    assert configs == [("pid", "87"), ("lqr", "79_4"), ("lqr", "85_4"), ("lqr", "89_4"),
                       ("vfrot", "0.8_1_2_0.94")], configs
    assert exp35.SCENARIO == "burst3", exp35.SCENARIO
    n_slots, per_rep, total = exp35.expected_duration(len(configs), REPS)
    print(f"SCENARIO {exp35.SCENARIO}; CONFIGS ({len(configs)}): " + ", ".join(name(c) for c in configs))
    print(f"expected_duration: {n_slots} slots/repeat, {per_rep / 60:.2f} min/repeat; {REPS} repeats ~ {total / 3600:.2f} h")

    calls, log = run_main_dry()
    seqs = [[c for r, c, _ in calls if r == rep] for rep in range(1, REPS + 1)]
    scheds_of = [[s for r, c, s in calls if r == rep] for rep in range(1, REPS + 1)]

    print("\n--- exp35.log lines for repeat 1 (as main() prints them) ---")
    print("\n".join(l for l in log.splitlines()
                    if l.startswith("SCENARIO") or l.startswith("rep 1 ") or l.startswith("EXPECTED")))

    print("\nconfig order per repeat (slot -> config):")
    w = max(len(name(c)) for c in configs + [ALONE])
    print("slot " + " ".join(f"{'r' + str(r):{w}s}" for r in range(1, REPS + 1)))
    for slot in range(n_slots):
        print(f"{slot:4d} " + " ".join(f"{name(seq[slot]):{w}s}" for seq in seqs))
    print("\nslot position of each config per repeat:")
    print(f"{'config':{w}s}  " + " ".join(f"r{r:<2d}" for r in range(1, REPS + 1)))
    for c in configs:
        print(f"{name(c):{w}s}  " + " ".join(f"{seq.index(c):3d}" for seq in seqs))

    print("\nburst schedule per repeat and flooder (t rel. flooder launch; window):")
    win = exp35.act_windows()
    for rep in range(1, REPS + 1):
        sc = scheds_of[rep - 1][1]
        for i, g in enumerate(exp35.FLOODERS):
            print(f"r{rep} {g} seed {exp35.burst_seed(rep, i):4d} win {win[g][0]:2d}-{sum(win[g]):2d}: "
                  + f"{', '.join(f'[{s:2d}-{s + d:2d}]' for s, d in sc[g]):38s}"
                  + f" on {sum(d for _, d in sc[g]):2d} s of {win[g][1]} s = "
                  + f"{100 * sum(d for _, d in sc[g]) / win[g][1]:4.1f}% duty")

    # ---- checks ----
    for rep, seq in enumerate(seqs, 1):
        assert len(seq) == 11 == n_slots, (rep, len(seq))
        non_alone = [c for c in seq if c != ALONE]
        assert sorted(non_alone) == sorted(configs), f"repeat {rep}: configs not each exactly once"
    print("\nCHECK 1 OK: every repeat has 11 slots and each of the 5 configs exactly once")

    for rep, seq in enumerate(seqs, 1):
        assert seq[0::2] == [ALONE] * 6, f"repeat {rep}: alone not at every even slot"
        assert all(c != ALONE for c in seq[1::2]), f"repeat {rep}: alone at an odd slot"
    print("CHECK 2 OK: alone at slots 0,2,...,10 -> before the first config, between every pair, after the last")

    random.seed(7)
    for rep, seq in enumerate(seqs, 1):
        order = configs[:]; random.shuffle(order)
        assert seq == [ALONE] + [x for c in order for x in (c, ALONE)], f"repeat {rep}: order differs"
    print("CHECK 3 OK: main()'s order equals an independent random.seed(7) shuffle per repeat "
          "(burst draws do not consume the shared generator)")

    for rep in range(1, REPS + 1):
        objs = [s for r, c, s in calls if r == rep and c != ALONE]
        assert objs and all(s is objs[0] for s in objs), f"repeat {rep}: configs got different schedule objects"
        assert all(s is objs[0] for r, c, s in calls if r == rep), f"repeat {rep}: alone got another object"
        assert objs[0] == {g: exp35.burst_schedule(rep, i, win[g]) for i, g in enumerate(exp35.FLOODERS)}
    print("CHECK 4 OK: within each repeat every run() call (5 configs + 6 alone) receives the SAME schedule "
          "object ('is'), equal to a fresh redraw from the repeat's seeds")

    firsts = [scheds_of[r][0] for r in range(REPS)]
    for g in exp35.FLOODERS:
        assert len({tuple(f[g]) for f in firsts}) == REPS, f"flooder {g}: two repeats share a schedule"
    assert len({id(f) for f in firsts}) == REPS
    print(f"CHECK 5 OK: for every flooder the schedule differs between all {REPS} repeats")

    fixed = [name(c) for c in configs if len({seq.index(c) for seq in seqs}) == 1]
    assert not fixed, f"configs always in the same slot: {fixed}"
    print(f"CHECK 6 OK: no config in the same slot in all {REPS} repeats (distinct positions: "
          + ", ".join(f"{name(c)} {len({seq.index(c) for seq in seqs})}" for c in configs) + ")")

    def whole(x):
        return int(x + 0.5)   # half-up rounding for x >= 0, written independently of exp35

    for rep in range(1, REPS + 1):
        for g, sc in scheds_of[rep - 1][0].items():
            w0, w1 = win[g][0], sum(win[g])
            assert sc, (rep, g)
            assert all(type(s) is int and type(d) is int for s, d in sc), (rep, g, sc)
            assert all(w0 <= s and s + d <= w1 and d >= 1 for s, d in sc), (rep, g, sc)
            assert all(b[0] - (a[0] + a[1]) >= 1 for a, b in zip(sc, sc[1:])), (rep, g, sc)
        assert f"rep {rep} BURST SEEDS: A={1000 * rep}, B={1000 * rep + 1}, C={1000 * rep + 2}" in log
        for i, g in enumerate(exp35.FLOODERS):
            line = (f"rep {rep} burst schedule flooder {g} ({exp35.PAR[g]} streams, seed {1000 * rep + i}, "
                    f"t rel. flooder launch): " + ", ".join(f"[{s}-{s + d}]" for s, d in scheds_of[rep - 1][0][g]))
            assert line in log.splitlines(), line
    print("CHECK 7 OK: all starts/ends are whole seconds, every burst >= 1 s, inside its flooder's window, "
          "bursts of a flooder >= 1 s apart (non-overlapping); seed line and the exact schedule logged per repeat")

    # burst3 schedule == the unrounded exp34-style draw inside the window, with start/end rounded half-up
    for rep in range(1, 201):
        for i, g in enumerate(exp35.FLOODERS):
            w0, w1 = win[g][0], sum(win[g])
            rng = random.Random(1000 * rep + i)
            raw, t = [], w0 + rng.uniform(*exp35.BURST_START)
            while t < w1:
                on = min(rng.uniform(*exp35.BURST_ON), w1 - t)
                raw.append((t, t + on)); t += on + rng.uniform(*exp35.BURST_OFF)
            ref = [(whole(a), whole(b) - whole(a)) for a, b in raw if whole(b) - whole(a) >= 2]
            assert ref == exp35.burst_schedule(rep, i, win[g]), (rep, g)
    print("CHECK 8 OK: for 200 repeats x 3 flooders the burst3 schedule equals the original continuous draw "
          "(same seeds, same random numbers) with start/end rounded to whole seconds")

    # without a window: exp34's own burst_schedule, rounded, equals exp35's (only the rounding is new)
    n_drop = 0
    for rep in range(1, 201):
        for i in range(3):
            a = [(whole(s), whole(s + d) - whole(s)) for s, d in exp34.burst_schedule(rep, i)]
            n_drop += sum(d < 2 for _, d in a)
            assert [x for x in a if x[1] >= 2] == exp35.burst_schedule(rep, i), (rep, i)
    print(f"CHECK 9 OK: exp35.burst_schedule(rep, idx) without a window == exp34's draw rounded to whole seconds "
          f"(200 reps x 3; {n_drop} clipped last bursts rounded below 2 s and dropped)")

    # what iperf3 runs: replay the burst_cmd sleeps and -t values of every repeat and flooder
    for rep in range(1, REPS + 1):
        for g, sc in scheds_of[rep - 1][0].items():
            t, got = 0, []
            for part in exp35.burst_cmd(g, sc, exp35.PAR[g]).split("; "):
                if part.startswith("sleep "):
                    t += int(part.split()[1])        # int(): fails on any fractional sleep
                    part = part.split("; ", 1)[-1]
                    continue
                dur = int(part.split(" -t ")[1].split()[0])   # int(): fails on any fractional -t
                got.append((t, dur)); t += dur
            assert got == sc, (rep, g, got, sc)
    print("CHECK 10 OK: the iperf3 command chain (integer sleeps and -t) replays exactly the logged schedule")

    assert exp35.MIN_BURST == 2.0, exp35.MIN_BURST
    n_b = 0
    for rep in range(1, 201):                         # the 8 test repeats and 192 more
        for i, g in enumerate(exp35.FLOODERS):
            for s, d in exp35.burst_schedule(rep, i, win[g]):
                assert d >= 2, (rep, g, s, d)
                assert win[g][0] <= s and s + d <= sum(win[g]), (rep, g, s, d)
                n_b += 1
    print(f"CHECK 11 OK: MIN_BURST = 2.0; all {n_b} burst3 bursts (200 repeats x 3 flooders) are >= 2 s long "
          f"and lie inside their flooder's window")
    assert "SUMMARY" in log and all(f"{c[0]:7s} {c[1]:>12s} {REPS:4d}" in log for c in configs)
    print("ALL TESTS PASSED")
