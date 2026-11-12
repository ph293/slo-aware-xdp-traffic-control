#!/bin/sh
# usage: ./run37.sh R [SCENARIO]   (R repeats, default 8; SCENARIO default steady3 (the exp34 scenario);
#                                   600 s rest after every repeat except the last, inside exp37.py)
# exp37: victim base 10 Gbit/s for [0,40) s + extra 8 Gbit/s in [0,14) and [26,40) s -> offers 18/10/18 Gbit/s
# expected: 13 slots x ~51 s + ~76 s references (ref18 + ref10) ~ 12.3 min per repeat; R=8 ~ 8 x 12.3 min + 7 x 600 s ~ 2.81 h
# needs host iperf3 servers on ports 5201-5205 (5205 = victim extra group; exp37.py checks and aborts; it does not start them)
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp37.py" >/dev/null; then echo "exp37.py is already running"; exit 1; fi
for f in results_dyn37.csv ts_dyn37.csv ref_victim37.txt exp37.log; do
  if [ -e "$f" ]; then echo "REFUSING TO START: $f already exists (move or delete it first; exp37 never appends)"; exit 1; fi
done
for p in 5201 5202 5203 5204 5205; do
  if ! ss -ltnH | awk '{print $4}' | grep -q ":$p\$"; then echo "no iperf3 server listening on host port $p; start it first: iperf3 -s -D -p $p"; exit 1; fi
done
python3 -c "import exp37; s = '${2:-steady3}'; assert s in list(exp37.STEADY) + list(exp37.BURSTY), f'unknown SCENARIO {s}'; n, p, t = exp37.expected_duration(len(exp37.CONFIGS), ${1:-8}); print(f'planned: SCENARIO {s}, ${1:-8} repeats x {n} slots ({len(exp37.CONFIGS)} configs + {n - len(exp37.CONFIGS)} alone), ~{p / 60:.1f} min/repeat + {${1:-8} - 1} x {exp37.SESSION_REST_S} s rest ~ {t / 3600:.2f} h')" || exit 1
sudo -v || exit 1   # cache sudo credentials now; exp37.py uses sudo -n and never prompts
nohup python3 -u exp37.py "${1:-8}" "${2:-steady3}" > exp37.log 2>&1 &
echo "started exp37.py (pid $!), logging to exp37.log"
