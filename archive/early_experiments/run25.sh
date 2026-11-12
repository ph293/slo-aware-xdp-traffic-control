#!/bin/sh
# usage: ./run25.sh N   (N repeats, default 5)
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp25.py" >/dev/null; then echo "exp25.py is already running"; exit 1; fi
sudo -v || exit 1   # cache sudo credentials now; exp25.py uses sudo -n and never prompts
nohup python3 -u exp25.py "${1:-5}" > exp25.log 2>&1 &
echo "started exp25.py (pid $!), logging to exp25.log"
