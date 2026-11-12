#!/bin/sh
# usage: ./run31.sh R   (R repeats, default 5; 600 s rest after every repeat except the last, inside exp31.py)
# expected: 15 slots x ~51 s + ~38 s reference ~ 13.4 min per repeat; R=1 ~ 13.4 min, R=6 ~ 6 x 13.4 min + 5 x 600 s ~ 2.2 h
# needs host iperf3 servers on ports 5201-5204 (exp31.py checks and aborts; it does not start them)
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp31.py" >/dev/null; then echo "exp31.py is already running"; exit 1; fi
sudo -v || exit 1   # cache sudo credentials now; exp31.py uses sudo -n and never prompts
nohup python3 -u exp31.py "${1:-5}" > exp31.log 2>&1 &
echo "started exp31.py (pid $!), logging to exp31.log"
