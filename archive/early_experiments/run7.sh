#!/bin/sh
# usage: ./run7.sh [repeats]   (default 5)
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp7.py" >/dev/null; then echo "exp7.py is already running"; exit 1; fi
sudo -v || exit 1   # cache sudo credentials now; exp7.py uses sudo -n and never prompts
nohup python3 -u exp7.py "${1:-5}" > exp7.log 2>&1 &
echo "started exp7.py (pid $!), logging to exp7.log"
