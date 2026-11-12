#!/bin/sh
# usage: ./run39.sh R   (R repeats, default 6; 600 s rest after every repeat except the last, inside exp39.py)
# exp39: rerun of exp38 on the resized host VM (6 vCPU, 8 GB; refuses unless nproc = 6, here and in exp39.py)
#        2 victims tenant1 (.11, port 5201) + tenant5 (.15, port 5205), 9 Gbit/s each, one stream, never limited;
#        4 flooders tenant2/3/4/6 = A/B/C/D (ports 5202/5203/5204/5206, 2 streams each), A 0-28, B 6-34, C 12-40,
#        D 18-40 s (all four 18-28 s); configs static 2500, static 3500, lqr 79_4,
#        lqr 85_4, lqr 90_4, vfrot 0.8_1_2_0.94
# expected: 13 slots x ~51 s + ~38 s reference (both victims together) ~ 11.7 min per repeat; R=6 ~ 6 x 11.7 min + 5 x 600 s ~ 2.00 h
# (exp38 measured ~48.7 s per slot -> ~11.2 min per repeat, ~1.95 h)
# needs tenant5 + tenant6 running (sudo bash make_tenant5.sh / make_tenant6.sh) and host iperf3 servers on ports
# 5201-5206 (checked here and again in exp39.py, which also checks ssh + iperf3 on every guest; nothing is started)
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp39.py" >/dev/null; then echo "exp39.py is already running"; exit 1; fi
for f in results_dyn39.csv ts_dyn39.csv ref_victim39.txt exp39.log; do
  if [ -e "$f" ]; then echo "REFUSING TO START: $f already exists (move or delete it first; exp39 never appends)"; exit 1; fi
done
n=$(nproc)
if [ "$n" != 6 ]; then echo "REFUSING TO START: nproc = $n, exp39 requires exactly 6 (rerun of exp38 after the resize from 4 to 6 vCPU)"; exit 1; fi
echo "host CPUs: $n"
missing=0
for p in 5201 5202 5203 5204 5205 5206; do
  if ! ss -ltnH | awk '{print $4}' | grep -q ":$p\$"; then echo "no iperf3 server listening on host port $p; start it first: iperf3 -s -D -p $p"; missing=1; fi
done
[ "$missing" = 0 ] || exit 1
python3 -c "import exp39; n, p, t = exp39.expected_duration(len(exp39.CONFIGS), ${1:-6}); print(f'planned: SCENARIO {exp39.SCENARIO}, ${1:-6} repeats x {n} slots ({len(exp39.CONFIGS)} configs + {n - len(exp39.CONFIGS)} alone), ~{p / 60:.1f} min/repeat + {${1:-6} - 1} x {exp39.SESSION_REST_S} s rest ~ {t / 3600:.2f} h')" || exit 1
sudo -v || exit 1   # cache sudo credentials now; exp39.py uses sudo -n and never prompts
nohup python3 -u exp39.py "${1:-6}" > exp39.log 2>&1 &
echo "started exp39.py (pid $!), logging to exp39.log"
