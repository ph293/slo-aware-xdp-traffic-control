#!/bin/sh
# usage: ./run24.sh N   (N repeats, default 5)
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp24.py" >/dev/null; then echo "exp24.py is already running"; exit 1; fi
sudo -v || exit 1   # cache sudo credentials now; exp24.py uses sudo -n and never prompts
nohup python3 -u exp24.py "${1:-5}" > exp24.log 2>&1 &
echo "started exp24.py (pid $!), logging to exp24.log"
