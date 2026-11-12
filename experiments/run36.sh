#!/bin/sh
# usage: ./run36.sh R [SCENARIO]   (R repeats, default 5; SCENARIO default steady3 (the exp34 scenario), "burst3" = exp35;
#                                   600 s rest after every repeat except the last, inside exp36.py)
# expected (victim offers 18 Gbit/s): 15 slots x ~51 s + ~38 s reference ~ 13.4 min per repeat; R=1 ~ 13.4 min, R=8 ~ 8 x 13.4 min + 7 x 600 s ~ 2.95 h
# needs host iperf3 servers on ports 5201-5204 (exp36.py checks and aborts; it does not start them)
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp36.py" >/dev/null; then echo "exp36.py is already running"; exit 1; fi
for f in results_dyn36.csv ts_dyn36.csv ref_victim36.txt exp36.log; do
  if [ -e "$f" ]; then echo "REFUSING TO START: $f already exists (move or delete it first; exp36 never appends)"; exit 1; fi
done
python3 -c "import exp36; s = '${2:-steady3}'; assert s in list(exp36.STEADY) + list(exp36.BURSTY), f'unknown SCENARIO {s}'; n, p, t = exp36.expected_duration(len(exp36.CONFIGS), ${1:-5}); print(f'planned: SCENARIO {s}, ${1:-5} repeats x {n} slots ({len(exp36.CONFIGS)} configs + {n - len(exp36.CONFIGS)} alone), ~{p / 60:.1f} min/repeat + {${1:-5} - 1} x {exp36.SESSION_REST_S} s rest ~ {t / 3600:.2f} h')" || exit 1
sudo -v || exit 1   # cache sudo credentials now; exp36.py uses sudo -n and never prompts
nohup python3 -u exp36.py "${1:-5}" "${2:-steady3}" > exp36.log 2>&1 &
echo "started exp36.py (pid $!), logging to exp36.log"
