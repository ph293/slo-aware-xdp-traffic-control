#!/bin/sh
# usage: ./run17.sh N   (N repeats, default 5)
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp17.py" >/dev/null; then echo "exp17.py is already running"; exit 1; fi
sudo -v || exit 1   # cache sudo credentials now; exp17.py uses sudo -n and never prompts
nohup python3 -u exp17.py "${1:-5}" > exp17.log 2>&1 &
echo "started exp17.py (pid $!), logging to exp17.log"
