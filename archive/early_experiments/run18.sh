#!/bin/sh
# usage: ./run18.sh N   (N repeats, default 5)
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp18.py" >/dev/null; then echo "exp18.py is already running"; exit 1; fi
sudo -v || exit 1   # cache sudo credentials now; exp18.py uses sudo -n and never prompts
nohup python3 -u exp18.py "${1:-5}" > exp18.log 2>&1 &
echo "started exp18.py (pid $!), logging to exp18.log"
