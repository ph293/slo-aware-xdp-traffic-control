#!/bin/sh
# usage: ./run45.sh R   (R repeats, default 6; 600 s rest after every repeat except the last, inside exp45.py)
# exp45: does an outer victim-feedback loop on lqr_orig's per-tenant targets (hyb, start T60, paper K) fix exp44's S3
#        collapse without losing S1/S2? vfrot 0.8_1_2_0.94 (ceiling 16000) vs lqr_orig T40 / T60 (paper K) vs
#        hyb T60_paperK, in exp44's three scenarios (seed 7, 40 s runs, exp43 SLO / RTT SLO / dip metrics):
#          S1 one flooder (A), V1/V2 9 Gbit/s; S2 two flooders (A, B), V1/V2 9 Gbit/s; S3 exp43's four flooders,
#          V1 9 / V2 V2_RATE_S3 Gbit/s, lqr_orig and hyb on ALL SIX tenants (victims too).
#        Per repeat and scenario: reference (2 x 20 s + V2 ping), one alone slot, 4 configs shuffled -> 15 slots,
#        ~14.7 min/repeat at exp43's measured rate; R=6 ~2.4 h including 5 x (600 + 45) s rest.
# Refuses (before asking for sudo) if exp44/exp45 is running, if any exp45 output exists, unless nproc = 6, if an iperf3
# server is missing, if calib42_result.json is missing / malformed / verdict FAIL, or if an lqr_orig gain has
# K[0] >= 0. exp45.py repeats every check; rep 1 starts with S3, whose reference refuses (before any slot and the
# first rest) if the ping fails, a victim misses 97 % of its rate or V2's vhost CPU is not >= 1.15 x the T40 target (18.9 %).
# Summary: end of exp45.log, or offline with python3 exp45.py --summary results_dyn45.csv
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp45.py" >/dev/null; then echo "exp45.py is already running"; exit 1; fi
if pgrep -f "python3 -u exp44.py" >/dev/null; then echo "exp44.py is running; wait for it to finish"; exit 1; fi
if pgrep -f "python3 -u exp43.py" >/dev/null; then echo "exp43.py is running; wait for it to finish"; exit 1; fi
if pgrep -f "calib42.py" >/dev/null; then echo "calib42.py is running; wait for it to finish"; exit 1; fi
for f in results_dyn45.csv ts_dyn45.csv ref_victim45.txt exp45.log; do
  if [ -e "$f" ]; then echo "REFUSING TO START: $f already exists (move or delete it first; exp45 never appends)"; exit 1; fi
done
n=$(nproc)
if [ "$n" != 6 ]; then echo "REFUSING TO START: nproc = $n, exp45 requires exactly 6 (continues exp39-exp44 on the 6 vCPU host VM)"; exit 1; fi
echo "host CPUs: $n"
if [ ! -e calib42_result.json ]; then echo "REFUSING TO START: calib42_result.json not found (run ./calibrate42.sh first)"; exit 1; fi
missing=0
for p in 5201 5202 5203 5204 5205 5206; do
  if ! ss -ltnH | awk '{print $4}' | grep -q ":$p\$"; then echo "no iperf3 server listening on host port $p; start it first: iperf3 -s -D -p $p"; missing=1; fi
done
[ "$missing" = 0 ] || exit 1
python3 -B -c "import exp45; d = exp45.load_calib(); k = exp45.check_gain_sign(exp45.calib_gain(d), 're-identified lqr_orig (ZOH, T60)'); print(f'calibration: verdict {d[\"verdict\"]}, cpu_ref {d[\"cpu_ref\"]:.2f} %, tau {d[\"tau\"]:.3f} s, b {d[\"b\"]:.3f}; re-identified K (ZOH, T60) = [{k[0]:.4f}, {k[1]:.4f}] (K[0] < 0: OK); T40 target {0.4 * d[\"cpu_ref\"]:.2f} %, S3 V2 vhost check >= {exp45.S3_VH_MIN_FRAC * 0.4 * d[\"cpu_ref\"]:.2f} %')" || exit 1
python3 -B -c "import exp45; exp45.check_static(); c = exp45.load_calib(); exp45.setup_configs(exp45.all_configs(), c); print('configs: ' + '; '.join(s + ' ' + ', '.join(m + ' ' + p for m, p in exp45.SCEN_CONFIGS[s]) for s in exp45.SCENARIOS) + f'; S3 V2 rate {exp45.V2_RATE_S3:g} Gbit/s'); print(exp45.hyb_report('T60_paperK', c))" || exit 1
python3 -B -c "import exp45; n, p, t = exp45.expected_duration(${1:-6}); _, pm, tm = exp45.measured_duration(${1:-6}); print(f'planned: ${1:-6} repeats x {n} slots (3 scenarios x (1 alone + 4 configs)) + 3 references, ~{pm / 60:.1f} min/repeat at exp43 measured rate (nominal ~{p / 60:.1f}; budget {exp45.BUDGET_MIN_PER_REP:g}: ' + ('fits' if pm <= exp45.BUDGET_MIN_PER_REP * 60 else 'DOES NOT FIT') + f') + {${1:-6} - 1} x ({exp45.SESSION_REST_S} + {exp45.COOLDOWN}) s rest ~ {tm / 3600:.2f} h (nominal ~{t / 3600:.2f} h)')" || exit 1
sudo -v || exit 1   # cache sudo credentials now; exp45.py uses sudo -n and never prompts
nohup python3 -u exp45.py "${1:-6}" > exp45.log 2>&1 &
echo "started exp45.py (pid $!), logging to exp45.log"
