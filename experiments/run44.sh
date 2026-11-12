#!/bin/sh
# usage: ./run44.sh R   (R repeats, default 6; 600 s rest after every repeat except the last, inside exp44.py)
# exp44: where is a FIXED per-tenant CPU target weak? vfrot 0.8_1_2_0.94 (ceiling 16000) vs the faithful port of the
#        authors' LQR (lqr_orig, paper K, T40 / T60 of calib42's cpu_ref) and lqr 85_4, in three scenarios (seed 7,
#        40 s runs, exp43 SLO / RTT SLO / dip metrics):
#          S1 one flooder (A), V1/V2 9 Gbit/s; S2 two flooders (A, B), V1/V2 9 Gbit/s; S3 exp43's four flooders,
#          V1 9 / V2 V2_RATE_S3 Gbit/s, lqr_orig on ALL SIX tenants (victims too).
#        Per repeat and scenario: reference (2 x 20 s + V2 ping), one alone slot, 4 configs shuffled -> 15 slots,
#        ~14.6 min/repeat at exp43's measured rate; R=6 ~2.4 h including 5 x (600 + 45) s rest.
# Refuses (before asking for sudo) if exp44 is running, if any exp44 output exists, unless nproc = 6, if an iperf3
# server is missing, if calib42_result.json is missing / malformed / verdict FAIL, or if an lqr_orig gain has
# K[0] >= 0. exp44.py repeats every check; rep 1 starts with S3, whose reference refuses (before any slot and the
# first rest) if the ping fails, a victim misses 97 % of its rate or V2's vhost CPU is not >= 1.15 x the T40 target (18.9 %).
# Summary: end of exp44.log, or offline with python3 exp44.py --summary results_dyn44.csv
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp44.py" >/dev/null; then echo "exp44.py is already running"; exit 1; fi
if pgrep -f "python3 -u exp43.py" >/dev/null; then echo "exp43.py is running; wait for it to finish"; exit 1; fi
if pgrep -f "calib42.py" >/dev/null; then echo "calib42.py is running; wait for it to finish"; exit 1; fi
for f in results_dyn44.csv ts_dyn44.csv ref_victim44.txt exp44.log; do
  if [ -e "$f" ]; then echo "REFUSING TO START: $f already exists (move or delete it first; exp44 never appends)"; exit 1; fi
done
n=$(nproc)
if [ "$n" != 6 ]; then echo "REFUSING TO START: nproc = $n, exp44 requires exactly 6 (continues exp39-exp43 on the 6 vCPU host VM)"; exit 1; fi
echo "host CPUs: $n"
if [ ! -e calib42_result.json ]; then echo "REFUSING TO START: calib42_result.json not found (run ./calibrate42.sh first)"; exit 1; fi
missing=0
for p in 5201 5202 5203 5204 5205 5206; do
  if ! ss -ltnH | awk '{print $4}' | grep -q ":$p\$"; then echo "no iperf3 server listening on host port $p; start it first: iperf3 -s -D -p $p"; missing=1; fi
done
[ "$missing" = 0 ] || exit 1
python3 -B -c "import exp44; d = exp44.load_calib(); k = exp44.check_gain_sign(exp44.calib_gain(d), 're-identified lqr_orig (ZOH, T60)'); print(f'calibration: verdict {d[\"verdict\"]}, cpu_ref {d[\"cpu_ref\"]:.2f} %, tau {d[\"tau\"]:.3f} s, b {d[\"b\"]:.3f}; re-identified K (ZOH, T60) = [{k[0]:.4f}, {k[1]:.4f}] (K[0] < 0: OK); T40 target {0.4 * d[\"cpu_ref\"]:.2f} %, S3 V2 vhost check >= {exp44.S3_VH_MIN_FRAC * 0.4 * d[\"cpu_ref\"]:.2f} %')" || exit 1
python3 -B -c "import exp44; exp44.check_static(); exp44.setup_configs(exp44.all_configs(), exp44.load_calib()); print('configs: ' + '; '.join(s + ' ' + ', '.join(m + ' ' + p for m, p in exp44.SCEN_CONFIGS[s]) for s in exp44.SCENARIOS) + f'; S3 V2 rate {exp44.V2_RATE_S3:g} Gbit/s')" || exit 1
python3 -B -c "import exp44; n, p, t = exp44.expected_duration(${1:-6}); _, pm, tm = exp44.measured_duration(${1:-6}); print(f'planned: ${1:-6} repeats x {n} slots (3 scenarios x (1 alone + 4 configs)) + 3 references, ~{pm / 60:.1f} min/repeat at exp43 measured rate (nominal ~{p / 60:.1f}; budget {exp44.BUDGET_MIN_PER_REP:g}: ' + ('fits' if pm <= exp44.BUDGET_MIN_PER_REP * 60 else 'DOES NOT FIT') + f') + {${1:-6} - 1} x ({exp44.SESSION_REST_S} + {exp44.COOLDOWN}) s rest ~ {tm / 3600:.2f} h (nominal ~{t / 3600:.2f} h)')" || exit 1
sudo -v || exit 1   # cache sudo credentials now; exp44.py uses sudo -n and never prompts
nohup python3 -u exp44.py "${1:-6}" > exp44.log 2>&1 &
echo "started exp44.py (pid $!), logging to exp44.log"
