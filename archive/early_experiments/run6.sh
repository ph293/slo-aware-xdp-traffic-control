#!/bin/sh
# usage: ./run6.sh [repeats]   (default 5)
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp6.py" >/dev/null; then echo "exp6.py is already running"; exit 1; fi
sudo -v || exit 1   # cache sudo credentials now; exp6.py uses sudo -n and never prompts
nohup python3 -u exp6.py "${1:-5}" > exp6.log 2>&1 &
echo "started exp6.py (pid $!), logging to exp6.log"
