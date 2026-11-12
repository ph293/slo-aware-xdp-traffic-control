#!/bin/sh
# usage: ./run29.sh R   (R repeats, default 5; 600 s rest after every repeat except the last, inside exp29.py)
# expected: 17 slots x ~51 s + ~38 s reference ~ 15 min per repeat; R=1 ~ 15 min, R=6 ~ 6 x 15 min + 5 x 600 s ~ 2.3 h
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp29.py" >/dev/null; then echo "exp29.py is already running"; exit 1; fi
sudo -v || exit 1   # cache sudo credentials now; exp29.py uses sudo -n and never prompts
nohup python3 -u exp29.py "${1:-5}" > exp29.log 2>&1 &
echo "started exp29.py (pid $!), logging to exp29.log"
