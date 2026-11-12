#!/bin/sh
# usage: ./run28.sh R   (R repeats, default 5; 600 s rest after every repeat except the last, inside exp28.py)
# expected: 17 slots x ~51 s + ~38 s reference ~ 15 min per repeat; R=1 ~ 15 min, R=6 ~ 6 x 15 min + 5 x 600 s ~ 2.3 h
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp28.py" >/dev/null; then echo "exp28.py is already running"; exit 1; fi
sudo -v || exit 1   # cache sudo credentials now; exp28.py uses sudo -n and never prompts
nohup python3 -u exp28.py "${1:-5}" > exp28.log 2>&1 &
echo "started exp28.py (pid $!), logging to exp28.log"
