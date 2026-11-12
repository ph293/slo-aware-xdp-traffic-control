#!/usr/bin/env python3
# Pure-python self-test for exp32.py: no VM, no sudo, no network.
# Checks (1) the lqrf CPU pre-filter (median / prediction / blend) on a hand-made CPU sequence,
# and (2) that the config parsing accepts all 7 CONFIGS.
import statistics as st
import exp32


def lqrf_filter(seq):
    # faithful copy of the lqrf CPU pre-filter in exp32.run(): keep the last 5 raw samples,
    # smoothed = median (raw sample itself while < 3 samples), predicted = last + (last - prev)
    # (smoothed while < 2 samples), effective = 0.7*smoothed + 0.3*predicted. Returns the per-step
    # (smoothed, predicted, effective) triples.
    lq_cpu, out = [], []
    for busy in seq:
        lq_cpu.append(busy); del lq_cpu[:-5]
        smoothed = st.median(lq_cpu) if len(lq_cpu) >= 3 else lq_cpu[-1]
        predicted = 2.0 * lq_cpu[-1] - lq_cpu[-2] if len(lq_cpu) >= 2 else smoothed
        out.append((smoothed, predicted, 0.7 * smoothed + 0.3 * predicted))
    return out


def test_lqrf_filter():
    seq = [80, 90, 70, 100, 60, 50]
    got = lqrf_filter(seq)
    # hand-computed (smoothed, predicted, effective):
    expect = [
        (80, 80, 80.0),     # [80]: <3 -> raw; <2 -> smoothed; 0.7*80+0.3*80
        (90, 100, 93.0),    # [80,90]: smoothed=raw 90; pred=2*90-80=100; 0.7*90+0.3*100
        (80, 50, 71.0),     # [80,90,70]: median=80; pred=2*70-90=50; 0.7*80+0.3*50
        (85, 130, 98.5),    # [80,90,70,100]: median=85; pred=2*100-70=130; 0.7*85+0.3*130
        (80, 20, 62.0),     # [80,90,70,100,60]: median=80; pred=2*60-100=20; 0.7*80+0.3*20
        (70, 40, 61.0),     # drop oldest -> [90,70,100,60,50]: median=70; pred=2*50-60=40; 0.7*70+0.3*40
    ]
    for i, ((sm, pr, ef), (esm, epr, eef)) in enumerate(zip(got, expect)):
        assert abs(sm - esm) < 1e-9, f"step {i}: smoothed {sm} != {esm}"
        assert abs(pr - epr) < 1e-9, f"step {i}: predicted {pr} != {epr}"
        assert abs(ef - eef) < 1e-9, f"step {i}: effective {ef} != {eef}"
    print(f"  lqrf filter: OK ({len(seq)} steps; effective = "
          + ", ".join(f"{e[2]:g}" for e in got) + ")")


def test_config_parsing():
    # the 7 CONFIGS of exp32, parsed by the same parse_* helpers exp32.main() uses
    configs = [("lqr", "85_4"), ("lqr", "89_4"), ("lqrf", "85_4"), ("lqrf", "89_4"),
               ("vfrot", "0.8_1_3_0.94"), ("vfrot", "0.8_1_2_0.94"), ("vfrot", "0.8_1_1_0.94")]
    parsed = {}
    for m, p in configs:
        if m in ("lqr", "lqrf"):
            parsed[(m, p)] = exp32.parse_lqr(p)        # raises SystemExit if out of range
        elif m == "vfrot":
            parsed[(m, p)] = exp32.parse_vfrot(p)
        else:
            raise AssertionError(f"unexpected mode {m}")
    assert parsed[("lqr", "85_4")] == (85.0, 4.0)
    assert parsed[("lqr", "89_4")] == (89.0, 4.0)
    assert parsed[("lqrf", "85_4")] == (85.0, 4.0)
    assert parsed[("lqrf", "89_4")] == (89.0, 4.0)
    # vfrot: (beta, add, rot_t, thresh) -- the three differ only in rot_t (3, 2, 1 s)
    assert parsed[("vfrot", "0.8_1_3_0.94")] == (0.8, 1.0, 3.0, 0.94)
    assert parsed[("vfrot", "0.8_1_2_0.94")] == (0.8, 1.0, 2.0, 0.94)
    assert parsed[("vfrot", "0.8_1_1_0.94")] == (0.8, 1.0, 1.0, 0.94)
    # gains compute for the lqr/lqrf RWT without error
    exp32.lqr_gain(4.0)
    print(f"  config parsing: OK (all {len(configs)} configs accepted; "
          f"vfrot rot_t = {[parsed[('vfrot', q)][2] for q in ('0.8_1_3_0.94', '0.8_1_2_0.94', '0.8_1_1_0.94')]})")


if __name__ == "__main__":
    print("self-test exp32:")
    test_lqrf_filter()
    test_config_parsing()
    print("ALL TESTS PASSED")
