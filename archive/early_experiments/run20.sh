#!/bin/sh
# usage: ./run20.sh N   (N repeats, default 5)
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp20.py" >/dev/null; then echo "exp20.py is already running"; exit 1; fi
sudo -v || exit 1   # cache sudo credentials now; exp20.py uses sudo -n and never prompts
nohup python3 -u exp20.py "${1:-5}" > exp20.log 2>&1 &
echo "started exp20.py (pid $!), logging to exp20.log"
