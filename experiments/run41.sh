#!/bin/sh
# usage: ./run41.sh R   (R repeats, default 6; 600 s rest after every repeat except the last, inside exp41.py)
# exp41: mixed traffic on the 6 vCPU host VM (refuses unless nproc = 6, here and in exp41.py); scenario windows,
#        reference, SLO, seed 7 and slot interleave as exp39/exp40; controllers unchanged
#        victims tenant1 (.11, port 5201) 9 Gbit/s TCP + tenant5 (.15, port 5205) 2 Gbit/s TCP + ping every 0.2 s,
#        never limited; flooders tenant2/3/4/6 = A/B/C/D (ports 5202/5203/5204/5206): A TCP 2 streams 0-28 s,
#        B UDP -b 6G 6-34 s, C bursty TCP 2 s on / 2 s off 12-40 s (7 runs), D TCP 8 streams 18-40 s;
#        configs vfrot 0.8_1_2_0.94, vffair 0.8_1_2_0.94_F0.25, lqr 79_4 / 85_4 / 90_4, static 2500 / 3500
# expected: 15 slots x ~49-51 s + ~58 s reference ~ 13.2-13.7 min per repeat; R=6 ~ 2.15-2.2 h incl. 5 x 600 s rest
# run ./pilot41.sh first. Needs tenant1-6 running and host iperf3 servers on ports 5201-5206 (checked here and
# again in exp41.py, which also checks ssh + iperf3 on every guest and ping on tenant5; nothing is started).
# Summary: end of exp41.log, or offline with python3 exp41.py --summary results_dyn41.csv
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp41.py" >/dev/null; then echo "exp41.py is already running"; exit 1; fi
if pgrep -f "pilot41.py" >/dev/null; then echo "pilot41.py is running; wait for it to finish"; exit 1; fi
for f in results_dyn41.csv ts_dyn41.csv ref_victim41.txt exp41.log; do
  if [ -e "$f" ]; then echo "REFUSING TO START: $f already exists (move or delete it first; exp41 never appends)"; exit 1; fi
done
n=$(nproc)
if [ "$n" != 6 ]; then echo "REFUSING TO START: nproc = $n, exp41 requires exactly 6 (continues exp39/exp40 on the 6 vCPU host VM)"; exit 1; fi
echo "host CPUs: $n"
missing=0
for p in 5201 5202 5203 5204 5205 5206; do
  if ! ss -ltnH | awk '{print $4}' | grep -q ":$p\$"; then echo "no iperf3 server listening on host port $p; start it first: iperf3 -s -D -p $p"; missing=1; fi
done
[ "$missing" = 0 ] || exit 1
python3 -c "import exp41; n, p, t = exp41.expected_duration(len(exp41.CONFIGS), ${1:-6}); _, pm, tm = exp41.measured_duration(len(exp41.CONFIGS), ${1:-6}); print(f'planned: SCENARIO {exp41.SCENARIO} mixed traffic, ${1:-6} repeats x {n} slots ({len(exp41.CONFIGS)} configs + {n - len(exp41.CONFIGS)} alone), ~{p / 60:.1f} min/repeat (measured rate ~{pm / 60:.1f}) + {${1:-6} - 1} x {exp41.SESSION_REST_S} s rest ~ {t / 3600:.2f} h (measured rate ~{tm / 3600:.2f} h)')" || exit 1
sudo -v || exit 1   # cache sudo credentials now; exp41.py uses sudo -n and never prompts
nohup python3 -u exp41.py "${1:-6}" > exp41.log 2>&1 &
echo "started exp41.py (pid $!), logging to exp41.log"
