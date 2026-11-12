#!/bin/sh
# usage: ./run30.sh R   (R repeats, default 5; 600 s rest after every repeat except the last, inside exp30.py)
# expected: 21 slots x ~51 s + ~38 s reference ~ 18.5 min per repeat; R=1 ~ 18.5 min, R=6 ~ 6 x 18.5 min + 5 x 600 s ~ 2.7 h
# needs host iperf3 servers on ports 5201-5204 (exp30.py checks and aborts; it does not start them)
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp30.py" >/dev/null; then echo "exp30.py is already running"; exit 1; fi
sudo -v || exit 1   # cache sudo credentials now; exp30.py uses sudo -n and never prompts
nohup python3 -u exp30.py "${1:-5}" > exp30.log 2>&1 &
echo "started exp30.py (pid $!), logging to exp30.log"
