#!/bin/sh
# usage: ./run40.sh R   (R repeats, default 6; 600 s rest after every repeat except the last, inside exp40.py)
# exp40: vfrot sensitivity study + vffair on the 6 vCPU host VM (refuses unless nproc = 6, here and in exp40.py);
#        scenario, victims, flooders, reference, SLO, seed 7 and slot interleave as exp39
#        2 victims tenant1 (.11, port 5201) + tenant5 (.15, port 5205), 9 Gbit/s each, one stream, never limited;
#        4 flooders tenant2/3/4/6 = A/B/C/D (ports 5202/5203/5204/5206, 2 streams each), A 0-28, B 6-34, C 12-40,
#        D 18-40 s (all four 18-28 s); configs vfrot 0.8_1_2_0.94 (= exp39), _B12000, _B20000, 0.9_1_2_0.94,
#        0.8_2_2_0.94, 0.8_1_1_0.94, vffair 0.8_1_2_0.94_F0.5, vffair 0.8_1_2_0.94_F0.25
# expected: 17 slots x ~51 s + ~38 s reference ~ 15.1 min per repeat (exp39 measured rate 48.8 s/slot: ~14.5 min);
#           R=6 ~ 2.34 h (measured rate ~2.28 h) including 5 x 600 s rest
# needs tenant1-6 running and host iperf3 servers on ports 5201-5206 (checked here and again in exp40.py, which
# also checks ssh + iperf3 on every guest; nothing is started). Summary: end of exp40.log, or offline with
# python3 exp40.py --summary results_dyn40.csv
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp40.py" >/dev/null; then echo "exp40.py is already running"; exit 1; fi
for f in results_dyn40.csv ts_dyn40.csv ref_victim40.txt exp40.log; do
  if [ -e "$f" ]; then echo "REFUSING TO START: $f already exists (move or delete it first; exp40 never appends)"; exit 1; fi
done
n=$(nproc)
if [ "$n" != 6 ]; then echo "REFUSING TO START: nproc = $n, exp40 requires exactly 6 (continues exp39 on the 6 vCPU host VM)"; exit 1; fi
echo "host CPUs: $n"
missing=0
for p in 5201 5202 5203 5204 5205 5206; do
  if ! ss -ltnH | awk '{print $4}' | grep -q ":$p\$"; then echo "no iperf3 server listening on host port $p; start it first: iperf3 -s -D -p $p"; missing=1; fi
done
[ "$missing" = 0 ] || exit 1
python3 -c "import exp40; n, p, t = exp40.expected_duration(len(exp40.CONFIGS), ${1:-6}); _, pm, tm = exp40.measured_duration(len(exp40.CONFIGS), ${1:-6}); print(f'planned: SCENARIO {exp40.SCENARIO}, ${1:-6} repeats x {n} slots ({len(exp40.CONFIGS)} configs + {n - len(exp40.CONFIGS)} alone), ~{p / 60:.1f} min/repeat (measured rate ~{pm / 60:.1f}) + {${1:-6} - 1} x {exp40.SESSION_REST_S} s rest ~ {t / 3600:.2f} h (measured rate ~{tm / 3600:.2f} h)')" || exit 1
sudo -v || exit 1   # cache sudo credentials now; exp40.py uses sudo -n and never prompts
nohup python3 -u exp40.py "${1:-6}" > exp40.log 2>&1 &
echo "started exp40.py (pid $!), logging to exp40.log"
