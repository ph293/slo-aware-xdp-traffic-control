"""Offline test of the exp30 allocation logic (no testbed: no ssh, sudo, XDP, iperf3).

Simulates the steady3 timeline (A 0-28 s, B 6-28+6 s, C 12-40 s) at DT ticks, feeds synthetic offered
counters to exp30.active_flooders and drives rot_caps / pidrotf_caps / pf_update exactly as run() does.
Usage: python3 test_exp30.py
"""
import ast, random, sys, os
sys.argv = sys.argv[:1]          # exp30 reads REPS from argv[1] at import
import exp30 as X

EPS = 1e-9
DEMAND = {"A": 20000.0, "B": 10000.0, "C": 10000.0}   # offered Mbit/s when active (4 vs 2 streams)
results = []

def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))

def truth(g, t):
    # flooder g active over the tick interval (t - DT, t]: judged at the interval midpoint
    s, d = X.STEADY["steady3"][g]
    return s <= t - X.DT / 2 < s + d

def simulate(mode, rot_t, budget_fn, flicker=0.0, seed=1):
    rng = random.Random(seed)
    offered = {g: [(0.0, 0.0)] for g in X.FLOODERS}
    delivered = {g: 0.0 for g in X.FLOODERS}
    since_reset = {g: 0.0 for g in X.FLOODERS}   # independent recomputation of the pidrotf counters
    pf_all, state = False, {"holder": None, "since": None}
    caps = [X.high_cap(10000.0, 1) if g == X.FLOODERS[0] else X.CAP_MIN for g in X.FLOODERS]
    rows, resets = [], []
    for k in range(1, int(X.T / X.DT) + 1):
        t = k * X.DT
        prev_off = {g: offered[g][-1] for g in X.FLOODERS}
        on = {g: truth(g, t) and rng.random() >= flicker for g in X.FLOODERS}
        passed = {}
        for g in X.FLOODERS:
            c = caps[X.FLOODERS.index(g)]           # caps in effect during this tick
            passed[g] = min(c, DEMAND[g]) * X.DT if on[g] else 0.0     # Mbit delivered this tick
            offered[g].append((t, offered[g][-1][1] + (DEMAND[g] * X.DT * 1e6 / 8 if on[g] else 0.0)))
        act = X.active_flooders(offered, prev_off)
        budget = budget_fn(t, rng)
        h_before = state["holder"]
        decided = False
        if mode == "rot":
            caps = X.rot_caps(act, budget, t, rot_t, caps, state)
        else:
            was_all = pf_all
            pf_all = X.pf_update(delivered, passed, act, pf_all)
            for g in X.FLOODERS:
                since_reset[g] += passed[g]
            if pf_all and not was_all:
                resets.append(t)
                since_reset = {g: 0.0 for g in X.FLOODERS}
            snap = dict(delivered)
            caps = X.pidrotf_caps(act, budget, t, rot_t, caps, delivered, state)
        decided = state["since"] == t
        rows.append(dict(t=t, act=act, on=on, budget=budget, caps=dict(zip(X.FLOODERS, caps)),
                         holder=state["holder"], prev=h_before, decided=decided,
                         dlv=None if mode == "rot" else snap,
                         ref=None if mode == "rot" else dict(since_reset)))
    return rows, resets

def generic_checks(rows, rot_t, label):
    a_bad = [r["t"] for r in rows if r["act"] and
             sum(r["caps"][g] for g in r["act"]) > r["budget"] + X.CAP_MIN + EPS]
    check(f"(a) {label}: sum of active caps <= budget + CAP_MIN at every tick", not a_bad,
          f"violations at t={a_bad[:5]}" if a_bad else f"{len(rows)} ticks")
    b_bad = []
    for r in rows:
        if len(r["act"]) >= 2:
            hc = X.high_cap(r["budget"], len(r["act"]))
            high = [g for g in r["act"] if r["caps"][g] > X.CAP_MIN + EPS]
            ok = r["holder"] in r["act"] and r["caps"][r["holder"]] == hc and \
                 (len(high) == 1 if hc > X.CAP_MIN + EPS else not high)
            if not ok: b_bad.append(r["t"])
    check(f"(b) {label}: exactly one holder whenever >= 2 flooders are active", not b_bad,
          f"violations at t={b_bad[:5]}" if b_bad else
          f"{sum(len(r['act']) >= 2 for r in rows)} ticks with >= 2 active")
    c_bad = [r["t"] for r in rows if r["act"] and
             any(r["caps"][g] > X.CAP_MIN + EPS for g in X.FLOODERS if g not in r["act"])]
    held = sum(1 for r in rows if not r["act"])
    check(f"(c) {label}: inactive flooders never hold the high cap", not c_bad,
          (f"violations at t={c_bad[:5]}" if c_bad else "ok")
          + (f"; {held} ticks with no flooder active keep the last caps (as exp29)" if held else ""))
    last, f_bad, forced = None, [], 0
    for r in rows:
        if r["holder"] != r["prev"] and r["prev"] is not None:
            if r["prev"] not in r["act"]:
                forced += 1      # holder went inactive: an immediate hand-over is required by (c)
            elif last is not None and r["t"] - last < rot_t - EPS:
                f_bad.append((r["t"], round(r["t"] - last, 2)))
            last = r["t"]
        elif r["prev"] is None and r["holder"] is not None:
            last = r["t"]
    check(f"(f) {label}: no voluntary hold change faster than T={rot_t:g} s", not f_bad,
          f"violations (t, gap)={f_bad[:5]}" if f_bad else f"{forced} forced hand-overs (holder went inactive)")

def timeline(rows):
    out, cur = [], None
    for r in rows:
        if r["holder"] != cur:
            out.append(f"{r['t']:.2f}:{r['holder']}"); cur = r["holder"]
    return " ".join(out)

def walk(lo=None, hi=16000.0, start=10000.0, step=800.0):
    lo = X.NF * X.CAP_MIN if lo is None else lo
    b = {"v": start}
    def f(t, rng):
        b["v"] = max(lo, min(hi, b["v"] + rng.uniform(-step, step)))
        return b["v"]
    return f

def main():
    windows = [(0, 6, {"A"}), (6, 12, {"A", "B"}), (12, 28, {"A", "B", "C"}), (28, 34, {"B", "C"}), (34, 40, {"C"})]
    act_bad = [(r["t"], r["act"]) for r in simulate("rot", 3.0, lambda t, rng: 10000.0)[0]
               if set(r["act"]) != {g for g in X.FLOODERS if r["on"][g]}]
    print("activity detection (exp30.active_flooders on synthetic offered counters):")
    check("activity: active_flooders matches the steady3 pattern at every tick", not act_bad, str(act_bad[:5]))

    print("\nrot 10_3 (TOTAL 10 Gbit/s, T 3 s):")
    rows, _ = simulate("rot", 3.0, lambda t, rng: 10000.0)
    print("  holder timeline: " + timeline(rows))
    generic_checks(rows, 3.0, "rot")
    d_bad = []
    for s, e, S in windows:
        seen = {r["holder"] for r in rows if s < r["t"] - X.DT / 2 < e}
        if seen != S: d_bad.append((s, e, sorted(seen), sorted(S)))
    check("(d) rot: rotation visits every active flooder in each activity window (and only those)",
          not d_bad, str(d_bad) if d_bad else "windows " + ", ".join(f"{s}-{e}:{''.join(sorted(S))}" for s, e, S in windows))

    print("\npidrot 85_3 (budget random walk in [NF*CAP_MIN, 16000], T 3 s, rot allocation):")
    rows, _ = simulate("rot", 3.0, walk(), seed=2)
    print("  holder timeline: " + timeline(rows))
    generic_checks(rows, 3.0, "pidrot")
    rows, _ = simulate("rot", 3.0, walk(start=600.0, step=150.0), seed=3)
    generic_checks(rows, 3.0, "pidrot near minimum budget")

    print("\npidrotf 85_3 (budget random walk, T 3 s, lowest cumulative delivered gets the high cap):")
    rows, resets = simulate("pidrotf", 3.0, walk(), seed=4)
    print("  holder timeline: " + timeline(rows))
    generic_checks(rows, 3.0, "pidrotf")
    first_all = next(r["t"] for r in rows if len(r["act"]) == X.NF)
    check("(e) pidrotf: counters reset exactly once, at the first tick with all three active",
          resets == [first_all], f"resets at t={resets}, first all-active tick t={first_all}")
    e_bad = []
    for r in rows:
        if r["dlv"] is not None and any(abs(r["dlv"][g] - r["ref"][g]) > 1e-6 for g in X.FLOODERS):
            e_bad.append(("counter", r["t"]))
        if r["decided"] and r["act"]:
            low = min(r["dlv"][g] for g in r["act"])
            tied = [g for g in r["act"] if r["dlv"][g] == low]
            want_ok = r["holder"] in tied and (len(tied) == 1 or r["holder"] != r["prev"] or tied == [r["prev"]])
            if not want_ok: e_bad.append(("pick", r["t"], r["holder"], r["dlv"]))
    n_dec = sum(1 for r in rows if r["decided"])
    check("(e) pidrotf: every decision gives the hold to the lowest-cumulative active flooder (ties -> not holder)",
          not e_bad, str(e_bad[:3]) if e_bad else f"{n_dec} decisions checked, counters match an independent sum")
    tie = [r for r in rows if r["decided"] and r["t"] >= first_all][:1]
    if tie:
        r = tie[0]
        print(f"  first decision after the reset: t={r['t']:.2f} delivered(Mbit)="
              + ", ".join(f"{g}={r['dlv'][g]:.0f}" for g in X.FLOODERS) + f" -> holder {r['holder']} (was {r['prev']})")

    print("\nvfrot 0.8_1_3_0.94 allocation (= pidrotf rule) under activity flicker (5% of ticks dropped):")
    for mode, label in (("rot", "rot+flicker"), ("pidrotf", "pidrotf+flicker")):
        rows, _ = simulate(mode, 3.0, walk(), flicker=0.05, seed=5)
        generic_checks(rows, 3.0, label)

    # informational: N = 2 equivalence with the exp29 functions (exp29.py read via ast, not imported)
    print("\nN=2 comparison with exp29 rot_caps / pidrotf_caps (informational, not PASS/FAIL):")
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "exp29.py")).read()
    ns = {"CAP_MIN": X.CAP_MIN}
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name in ("rot_caps", "pidrotf_caps"):
            exec(compile(ast.Module([node], []), "exp29.py", "exec"), ns)
    saved = X.FLOODERS, X.NF
    X.FLOODERS, X.NF = ["A", "B"], 2
    try:
        old_steady = X.STEADY["steady"]
        for name in ("rot", "pidrotf"):
            st30, st29 = {"holder": None, "since": None}, {"holder": None, "slot": None}
            c30 = c29 = [9800.0, X.CAP_MIN]; dl = {"A": 0.0, "B": 0.0}; same = n = 0
            for k in range(1, 161):
                t = k * X.DT
                act = [g for g in "AB" if old_steady[g][0] <= t - X.DT / 2 < sum(old_steady[g])]
                for g in act: dl[g] += min(c30[X.FLOODERS.index(g)], DEMAND[g]) * X.DT
                if name == "rot":
                    c30 = X.rot_caps(act, 10000.0, t, 3.0, c30, st30)
                    c29 = ns["rot_caps"](act, 10000.0 - X.CAP_MIN, t, 3.0, c29)
                else:
                    c30 = X.pidrotf_caps(act, 10000.0, t, 3.0, c30, dl, st30)
                    c29 = ns["pidrotf_caps"](act, 10000.0 - X.CAP_MIN, t, 3.0, c29, dl, st29)
                n += 1; same += list(c30) == list(c29)
            print(f"  {name}: exp30 caps == exp29 caps on {same}/{n} ticks of the exp29 steady pattern")
    finally:
        X.FLOODERS, X.NF = saved

    npass = sum(ok for _, ok in results)
    print(f"\n{npass}/{len(results)} checks PASS" + ("" if npass == len(results) else "  <-- FAILURES"))
    return 0 if npass == len(results) else 1

if __name__ == "__main__":
    sys.exit(main())
