#!/bin/sh
# usage: ./run21.sh N   (N repeats, default 5)
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp21.py" >/dev/null; then echo "exp21.py is already running"; exit 1; fi
sudo -v || exit 1   # cache sudo credentials now; exp21.py uses sudo -n and never prompts
nohup python3 -u exp21.py "${1:-5}" > exp21.log 2>&1 &
echo "started exp21.py (pid $!), logging to exp21.log"
