#!/bin/sh
# usage: ./run19.sh N   (N repeats, default 5)
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp19.py" >/dev/null; then echo "exp19.py is already running"; exit 1; fi
sudo -v || exit 1   # cache sudo credentials now; exp19.py uses sudo -n and never prompts
nohup python3 -u exp19.py "${1:-5}" > exp19.log 2>&1 &
echo "started exp19.py (pid $!), logging to exp19.log"
