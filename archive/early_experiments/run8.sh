#!/bin/sh
# usage: ./run8.sh [repeats]   (default 5)
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp8.py" >/dev/null; then echo "exp8.py is already running"; exit 1; fi
sudo -v || exit 1   # cache sudo credentials now; exp8.py uses sudo -n and never prompts
nohup python3 -u exp8.py "${1:-5}" > exp8.log 2>&1 &
echo "started exp8.py (pid $!), logging to exp8.log"
