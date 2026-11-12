#!/bin/sh
# usage: ./run10.sh [repeats]   (default 5)
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp10.py" >/dev/null; then echo "exp10.py is already running"; exit 1; fi
sudo -v || exit 1   # cache sudo credentials now; exp10.py uses sudo -n and never prompts
nohup python3 -u exp10.py "${1:-5}" > exp10.log 2>&1 &
echo "started exp10.py (pid $!), logging to exp10.log"
