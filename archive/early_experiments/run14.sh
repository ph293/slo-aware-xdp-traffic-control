#!/bin/sh
# usage: ./run14.sh [repeats]   (default 5)
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp14.py" >/dev/null; then echo "exp14.py is already running"; exit 1; fi
sudo -v || exit 1   # cache sudo credentials now; exp14.py uses sudo -n and never prompts
nohup python3 -u exp14.py "${1:-5}" > exp14.log 2>&1 &
echo "started exp14.py (pid $!), logging to exp14.log"
