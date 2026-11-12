#!/bin/sh
# usage: ./run23.sh N   (N repeats, default 5)
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp23.py" >/dev/null; then echo "exp23.py is already running"; exit 1; fi
sudo -v || exit 1   # cache sudo credentials now; exp23.py uses sudo -n and never prompts
nohup python3 -u exp23.py "${1:-5}" > exp23.log 2>&1 &
echo "started exp23.py (pid $!), logging to exp23.log"
