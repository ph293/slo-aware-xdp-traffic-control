#!/bin/sh
# usage: ./run42.sh R   (R repeats, default 6; 600 s rest after every repeat except the last, inside exp42.py)
# exp42: faithful port of the published LQR controller (lqr_orig, Bisevac et al. 2026, Algorithm 1) on the 6 vCPU
#        host VM (refuses unless nproc = 6, here and in exp42.py); scenario, victims, flooders, reference, SLO, seed 7
#        and slot interleave as exp39/exp40. Configs: vfrot 0.8_1_2_0.94 and lqr 85_4 (paired references, exp39 code
#        paths), lqr_orig T40 / T60 / T80 (ARE gain on the calibrated plant) and T60_paperK (K = [-0.2562, -0.3031]).
#        lqr_orig: per-flooder vhost thread CPU, dt 0.5 s, median-of-5 filter, absolute drop fraction actuated by
#        xdp_dropfrac.o (swapped in on the 4 flooders for its slot only). Documented deviation: drop released to 0
#        when the victims' window ends, before the flooder wait (60 s timeout + WARNING).
# expected: 13 slots x ~51 s + ~38 s reference + XDP swaps ~ 11.9 min per repeat (exp39 measured rate ~11.4 min);
#           R=6 ~ 2.0 h including 5 x 600 s rest
# needs ./calibrate42.sh first (calib42_result.json, verdict not FAIL), tenant1-6 running and host iperf3 servers on
# ports 5201-5206 (checked here and again in exp42.py, which also checks ssh + iperf3 on every guest).
# Summary: end of exp42.log, or offline with python3 exp42.py --summary results_dyn42.csv
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp42.py" >/dev/null; then echo "exp42.py is already running"; exit 1; fi
if pgrep -f "calib42.py" >/dev/null; then echo "calib42.py is running; wait for it to finish"; exit 1; fi
for f in results_dyn42.csv ts_dyn42.csv ref_victim42.txt exp42.log; do
  if [ -e "$f" ]; then echo "REFUSING TO START: $f already exists (move or delete it first; exp42 never appends)"; exit 1; fi
done
n=$(nproc)
if [ "$n" != 6 ]; then echo "REFUSING TO START: nproc = $n, exp42 requires exactly 6 (continues exp39 on the 6 vCPU host VM)"; exit 1; fi
echo "host CPUs: $n"
missing=0
for p in 5201 5202 5203 5204 5205 5206; do
  if ! ss -ltnH | awk '{print $4}' | grep -q ":$p\$"; then echo "no iperf3 server listening on host port $p; start it first: iperf3 -s -D -p $p"; missing=1; fi
done
[ "$missing" = 0 ] || exit 1
python3 -c "import exp42; d = exp42.load_calib(); print(f'calibration: verdict {d[\"verdict\"]}, cpu_ref {d[\"cpu_ref\"]:.2f} %, tau {d[\"tau\"]:.3f} s, b {d[\"b\"]:.3f}')" || exit 1
python3 -c "import exp42; n, p, t = exp42.expected_duration(len(exp42.CONFIGS), ${1:-6}); _, pm, tm = exp42.measured_duration(len(exp42.CONFIGS), ${1:-6}); print(f'planned: SCENARIO {exp42.SCENARIO}, ${1:-6} repeats x {n} slots ({len(exp42.CONFIGS)} configs + {n - len(exp42.CONFIGS)} alone), ~{p / 60:.1f} min/repeat (measured rate ~{pm / 60:.1f}) + {${1:-6} - 1} x {exp42.SESSION_REST_S} s rest ~ {t / 3600:.2f} h (measured rate ~{tm / 3600:.2f} h)')" || exit 1
sudo -v || exit 1   # cache sudo credentials now; exp42.py uses sudo -n and never prompts
nohup python3 -u exp42.py "${1:-6}" > exp42.log 2>&1 &
echo "started exp42.py (pid $!), logging to exp42.log"
