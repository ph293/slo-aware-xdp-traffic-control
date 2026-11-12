#!/bin/sh
# usage: ./run22.sh N   (N repeats, default 5)
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp22.py" >/dev/null; then echo "exp22.py is already running"; exit 1; fi
sudo -v || exit 1   # cache sudo credentials now; exp22.py uses sudo -n and never prompts
nohup python3 -u exp22.py "${1:-5}" > exp22.log 2>&1 &
echo "started exp22.py (pid $!), logging to exp22.log"
