#!/bin/sh
# usage: ./run11.sh [repeats]   (default 5)
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp11.py" >/dev/null; then echo "exp11.py is already running"; exit 1; fi
sudo -v || exit 1   # cache sudo credentials now; exp11.py uses sudo -n and never prompts
nohup python3 -u exp11.py "${1:-5}" > exp11.log 2>&1 &
echo "started exp11.py (pid $!), logging to exp11.log"
