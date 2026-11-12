#!/bin/sh
# usage: ./run12.sh [repeats]   (default 5)
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp12.py" >/dev/null; then echo "exp12.py is already running"; exit 1; fi
sudo -v || exit 1   # cache sudo credentials now; exp12.py uses sudo -n and never prompts
nohup python3 -u exp12.py "${1:-5}" > exp12.log 2>&1 &
echo "started exp12.py (pid $!), logging to exp12.log"
