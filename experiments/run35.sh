#!/bin/sh
# usage: ./run35.sh R [SCENARIO]   (R repeats, default 5; SCENARIO default burst3, "steady3" = the exp34 scenario;
#                                   600 s rest after every repeat except the last, inside exp35.py)
# expected (victim offers 18 Gbit/s): 11 slots x ~51 s + ~38 s reference ~ 10.0 min per repeat; R=1 ~ 10.0 min, R=6 ~ 6 x 10.0 min + 5 x 600 s ~ 1.8 h
# needs host iperf3 servers on ports 5201-5204 (exp35.py checks and aborts; it does not start them)
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp35.py" >/dev/null; then echo "exp35.py is already running"; exit 1; fi
for f in results_dyn35.csv ts_dyn35.csv ref_victim35.txt exp35.log; do
  if [ -e "$f" ]; then echo "REFUSING TO START: $f already exists (move or delete it first; exp35 never appends)"; exit 1; fi
done
python3 -c "import exp35; s = '${2:-burst3}'; assert s in list(exp35.STEADY) + list(exp35.BURSTY), f'unknown SCENARIO {s}'; n, p, t = exp35.expected_duration(len(exp35.CONFIGS), ${1:-5}); print(f'planned: SCENARIO {s}, ${1:-5} repeats x {n} slots ({len(exp35.CONFIGS)} configs + {n - len(exp35.CONFIGS)} alone), ~{p / 60:.1f} min/repeat + {${1:-5} - 1} x {exp35.SESSION_REST_S} s rest ~ {t / 3600:.2f} h')" || exit 1
sudo -v || exit 1   # cache sudo credentials now; exp35.py uses sudo -n and never prompts
nohup python3 -u exp35.py "${1:-5}" "${2:-burst3}" > exp35.log 2>&1 &
echo "started exp35.py (pid $!), logging to exp35.log"
