#!/bin/sh
# usage: ./run27.sh N   (N repeats, default 5; 600 s rest after every repeat except the last, inside exp27.py)
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp27.py" >/dev/null; then echo "exp27.py is already running"; exit 1; fi
sudo -v || exit 1   # cache sudo credentials now; exp27.py uses sudo -n and never prompts
nohup python3 -u exp27.py "${1:-5}" > exp27.log 2>&1 &
echo "started exp27.py (pid $!), logging to exp27.log"
