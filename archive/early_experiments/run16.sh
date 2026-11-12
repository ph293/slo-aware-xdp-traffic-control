#!/bin/sh
# usage: ./run16.sh N   (N repeats, default 5)
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp16.py" >/dev/null; then echo "exp16.py is already running"; exit 1; fi
sudo -v || exit 1   # cache sudo credentials now; exp16.py uses sudo -n and never prompts
nohup python3 -u exp16.py "${1:-5}" > exp16.log 2>&1 &
echo "started exp16.py (pid $!), logging to exp16.log"
