#!/bin/sh
# usage: ./run9.sh [repeats]   (default 5)
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp9.py" >/dev/null; then echo "exp9.py is already running"; exit 1; fi
sudo -v || exit 1   # cache sudo credentials now; exp9.py uses sudo -n and never prompts
nohup python3 -u exp9.py "${1:-5}" > exp9.log 2>&1 &
echo "started exp9.py (pid $!), logging to exp9.log"
