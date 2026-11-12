#!/bin/sh
# usage: ./run15.sh [repeats]   (default 5)
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp15.py" >/dev/null; then echo "exp15.py is already running"; exit 1; fi
sudo -v || exit 1   # cache sudo credentials now; exp15.py uses sudo -n and never prompts
nohup python3 -u exp15.py "${1:-5}" > exp15.log 2>&1 &
echo "started exp15.py (pid $!), logging to exp15.log"
