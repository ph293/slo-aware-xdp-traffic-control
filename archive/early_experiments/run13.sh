#!/bin/sh
# usage: ./run13.sh [repeats]   (default 5)
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp13.py" >/dev/null; then echo "exp13.py is already running"; exit 1; fi
sudo -v || exit 1   # cache sudo credentials now; exp13.py uses sudo -n and never prompts
nohup python3 -u exp13.py "${1:-5}" > exp13.log 2>&1 &
echo "started exp13.py (pid $!), logging to exp13.log"
