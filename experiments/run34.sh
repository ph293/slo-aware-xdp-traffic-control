#!/bin/sh
# usage: ./run34.sh R   (R repeats, default 5; 600 s rest after every repeat except the last, inside exp34.py)
# expected (victim offers 18 Gbit/s): 17 slots x ~51 s + ~38 s reference ~ 15.1 min per repeat; R=1 ~ 15.1 min, R=6 ~ 6 x 15.1 min + 5 x 600 s ~ 2.3 h
# needs host iperf3 servers on ports 5201-5204 (exp34.py checks and aborts; it does not start them)
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp34.py" >/dev/null; then echo "exp34.py is already running"; exit 1; fi
for f in results_dyn34.csv ts_dyn34.csv ref_victim34.txt exp34.log; do
  if [ -e "$f" ]; then echo "REFUSING TO START: $f already exists (move or delete it first; exp34 never appends)"; exit 1; fi
done
python3 -c "import exp34; n, p, t = exp34.expected_duration(len(exp34.CONFIGS), ${1:-5}); print(f'planned: ${1:-5} repeats x {n} slots, ~{p / 60:.1f} min/repeat + {${1:-5} - 1} x {exp34.SESSION_REST_S} s rest ~ {t / 3600:.2f} h')" || exit 1
sudo -v || exit 1   # cache sudo credentials now; exp34.py uses sudo -n and never prompts
nohup python3 -u exp34.py "${1:-5}" > exp34.log 2>&1 &
echo "started exp34.py (pid $!), logging to exp34.log"
