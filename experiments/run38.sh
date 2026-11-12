#!/bin/sh
# usage: ./run38.sh R   (R repeats, default 6; 600 s rest after every repeat except the last, inside exp38.py)
# exp38: 2 victims tenant1 (.11, port 5201) + tenant5 (.15, port 5205), 9 Gbit/s each, one stream, never limited;
#        4 flooders tenant2/3/4/6 = A/B/C/D (ports 5202/5203/5204/5206, 2 streams each), A 0-28, B 6-34, C 12-40,
#        D 18-40 s (all four 18-28 s); configs static 2500, static 3500, lqr 79_4, lqr 85_4, vfrot 0.8_1_2_0.94
# expected: 11 slots x ~51 s + ~38 s reference (both victims together) ~ 10.0 min per repeat; R=6 ~ 6 x 10.0 min + 5 x 600 s ~ 1.83 h
# needs tenant5 + tenant6 running (sudo bash make_tenant5.sh / make_tenant6.sh) and host iperf3 servers on ports
# 5201-5206 (checked here and again in exp38.py, which also checks ssh + iperf3 on every guest; nothing is started)
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp38.py" >/dev/null; then echo "exp38.py is already running"; exit 1; fi
for f in results_dyn38.csv ts_dyn38.csv ref_victim38.txt exp38.log; do
  if [ -e "$f" ]; then echo "REFUSING TO START: $f already exists (move or delete it first; exp38 never appends)"; exit 1; fi
done
missing=0
for p in 5201 5202 5203 5204 5205 5206; do
  if ! ss -ltnH | awk '{print $4}' | grep -q ":$p\$"; then echo "no iperf3 server listening on host port $p; start it first: iperf3 -s -D -p $p"; missing=1; fi
done
[ "$missing" = 0 ] || exit 1
python3 -c "import exp38; n, p, t = exp38.expected_duration(len(exp38.CONFIGS), ${1:-6}); print(f'planned: SCENARIO {exp38.SCENARIO}, ${1:-6} repeats x {n} slots ({len(exp38.CONFIGS)} configs + {n - len(exp38.CONFIGS)} alone), ~{p / 60:.1f} min/repeat + {${1:-6} - 1} x {exp38.SESSION_REST_S} s rest ~ {t / 3600:.2f} h')" || exit 1
sudo -v || exit 1   # cache sudo credentials now; exp38.py uses sudo -n and never prompts
nohup python3 -u exp38.py "${1:-6}" > exp38.log 2>&1 &
echo "started exp38.py (pid $!), logging to exp38.log"
