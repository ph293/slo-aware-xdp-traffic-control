#!/usr/bin/env python3
# Pure-python self-test for exp34.py: no VM, no sudo, no network.
# Builds the CONFIGS list (parsed by the same parse_* helpers exp34.main() uses) and the per-repeat
# slot order for REPS repeats, reproducing exp34.main(): random.seed(7), then per repeat
# order = configs[:]; random.shuffle(order); seq = [ALONE] + [c, ALONE for c in order].
# (exp34.main() consumes no other global random numbers: reference() and run() use none, and
# burst_schedule() uses its own random.Random.)
import random
import exp34

REPS = 10
ALONE = ("alone", "na")


def build_configs():
    configs = list(exp34.CONFIGS)
    for m, p in configs:
        if m == "pid":
            assert 0 < float(p) <= 100, p
        elif m in ("lqr", "lqrf"):
            target, rwt = exp34.parse_lqr(p)        # raises SystemExit if out of range
            exp34.lqr_gain(rwt)
        elif m == "pidrotf":
            exp34.parse_pidrotf(p)
        elif m == "vfrot":
            exp34.parse_vfrot(p)
        else:
            raise AssertionError(f"unexpected mode {m}")
    return configs


def build_orders(configs, reps):
    random.seed(7)
    seqs = []
    for rep in range(1, reps + 1):
        order = configs[:]; random.shuffle(order)
        seqs.append([ALONE] + [x for c in order for x in (c, ALONE)])
    return seqs


def name(c):
    return f"{c[0]} {c[1]}"


if __name__ == "__main__":
    configs = build_configs()
    print(f"CONFIGS ({len(configs)}): " + ", ".join(name(c) for c in configs))
    n_slots, per_rep, total = exp34.expected_duration(len(configs), REPS)
    print(f"expected_duration: {n_slots} slots/repeat, {per_rep:.1f} s = {per_rep / 60:.2f} min/repeat; "
          f"{REPS} repeats ~ {total / 3600:.2f} h")
    seqs = build_orders(configs, REPS)

    print("\nrepeat -> slot -> config")
    w = max(len(name(c)) for c in configs + [ALONE])
    for rep, seq in enumerate(seqs, 1):
        print(f"\nrepeat {rep}:")
        for slot, c in enumerate(seq):
            print(f"  slot {slot:2d} -> {name(c)}")

    # compact matrix: config slot positions per repeat
    print("\nslot position of each config per repeat:")
    print(f"{'config':{w}s}  " + " ".join(f"r{r:<2d}" for r in range(1, REPS + 1)))
    for c in configs:
        print(f"{name(c):{w}s}  " + " ".join(f"{seq.index(c):3d}" for seq in seqs))

    # checks
    assert len(configs) == 8 and len(set(configs)) == 8
    for rep, seq in enumerate(seqs, 1):
        assert len(seq) == 17 == n_slots, (rep, len(seq))
        non_alone = [c for c in seq if c != ALONE]
        assert sorted(non_alone) == sorted(configs), f"repeat {rep}: configs not each exactly once"
        assert seq[0::2] == [ALONE] * 9, f"repeat {rep}: alone not at every even slot"
        assert all(c != ALONE for c in seq[1::2]), f"repeat {rep}: alone at an odd slot"
    print("\nCHECK 1 OK: every repeat has 17 slots and each of the 8 configs exactly once")
    print("CHECK 2 OK: alone at slots 0,2,...,16 -> an alone control before the first, between every "
          "two consecutive configs, and after the last")
    fixed = [name(c) for c in configs if len({seq.index(c) for seq in seqs}) == 1]
    assert not fixed, f"configs always in the same slot: {fixed}"
    print("CHECK 3 OK: no config sits in the same slot in all 10 repeats (distinct positions: "
          + ", ".join(f"{name(c)} {len({seq.index(c) for seq in seqs})}" for c in configs) + ")")
    print("ALL TESTS PASSED")
