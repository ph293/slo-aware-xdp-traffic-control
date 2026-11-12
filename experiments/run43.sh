#!/bin/sh
# usage: ./run43.sh R   (R repeats, default 6; 600 s rest after every repeat except the last, inside exp43.py)
# exp43: vfrot (BMAX 16000 / 24000 / 32000) vs the faithful port of the authors' LQR (lqr_orig: paper K at T40 / T60 /
#        T80, and the re-identified K with the SIGN FIXED (ARE on the ZOH plant of calib42) at T60) and lqr 85_4, on
#        the exp39 steady4 scenario (TCP flooders, seed 7, 40 s runs, alone controls interleaved), plus V2 latency
#        (tenant5 pings 10.10.0.1 every 0.2 s; per-repeat RTT reference, V1 + V2 at 9 Gbit/s, 2 x 20 s) and victim dip
#        metrics. 8 configs -> 17 slots, ~15 min per repeat; R=6 ~2.4 h including 5 x 600 s rest.
# Refuses (before asking for sudo) if exp43 is running, if any exp43 output exists, unless nproc = 6, if an iperf3
# server is missing, if calib42_result.json is missing / malformed / verdict FAIL, or if the re-identified K has
# K[0] >= 0. exp43.py repeats every check; its rep-1 reference ping check runs before the first slot.
# Summary: end of exp43.log, or offline with python3 exp43.py --summary results_dyn43.csv
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp43.py" >/dev/null; then echo "exp43.py is already running"; exit 1; fi
if pgrep -f "calib42.py" >/dev/null; then echo "calib42.py is running; wait for it to finish"; exit 1; fi
for f in results_dyn43.csv ts_dyn43.csv ref_victim43.txt exp43.log; do
  if [ -e "$f" ]; then echo "REFUSING TO START: $f already exists (move or delete it first; exp43 never appends)"; exit 1; fi
done
n=$(nproc)
if [ "$n" != 6 ]; then echo "REFUSING TO START: nproc = $n, exp43 requires exactly 6 (continues exp39-exp42 on the 6 vCPU host VM)"; exit 1; fi
echo "host CPUs: $n"
if [ ! -e calib42_result.json ]; then echo "REFUSING TO START: calib42_result.json not found (run ./calibrate42.sh first)"; exit 1; fi
missing=0
for p in 5201 5202 5203 5204 5205 5206; do
  if ! ss -ltnH | awk '{print $4}' | grep -q ":$p\$"; then echo "no iperf3 server listening on host port $p; start it first: iperf3 -s -D -p $p"; missing=1; fi
done
[ "$missing" = 0 ] || exit 1
python3 -B -c "import exp43; d = exp43.load_calib(); k = exp43.check_gain_sign(exp43.calib_gain(d), 're-identified lqr_orig (ZOH, T60)'); print(f'calibration: verdict {d[\"verdict\"]}, cpu_ref {d[\"cpu_ref\"]:.2f} %, tau {d[\"tau\"]:.3f} s, b {d[\"b\"]:.3f}; re-identified K (ZOH, T60) = [{k[0]:.4f}, {k[1]:.4f}] (K[0] < 0: OK)')" || exit 1
python3 -B -c "import exp43; exp43.setup_configs(exp43.CONFIGS, exp43.load_calib())" || exit 1
python3 -B -c "import exp43; n, p, t = exp43.expected_duration(len(exp43.CONFIGS), ${1:-6}); _, pm, tm = exp43.measured_duration(len(exp43.CONFIGS), ${1:-6}); print(f'planned: SCENARIO {exp43.SCENARIO}, ${1:-6} repeats x {n} slots ({len(exp43.CONFIGS)} configs + {n - len(exp43.CONFIGS)} alone), ~{p / 60:.1f} min/repeat (measured rate ~{pm / 60:.1f}) + {${1:-6} - 1} x {exp43.SESSION_REST_S} s rest ~ {t / 3600:.2f} h (measured rate ~{tm / 3600:.2f} h)')" || exit 1
sudo -v || exit 1   # cache sudo credentials now; exp43.py uses sudo -n and never prompts
nohup python3 -u exp43.py "${1:-6}" > exp43.log 2>&1 &
echo "started exp43.py (pid $!), logging to exp43.log"
