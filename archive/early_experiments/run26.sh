#!/bin/sh
# usage: ./run26.sh N   (N repeats, default 5)
cd "$(dirname "$0")"
if pgrep -f "python3 -u exp26.py" >/dev/null; then echo "exp26.py is already running"; exit 1; fi
sudo -v || exit 1   # cache sudo credentials now; exp26.py uses sudo -n and never prompts
nohup python3 -u exp26.py "${1:-5}" > exp26.log 2>&1 &
echo "started exp26.py (pid $!), logging to exp26.log"
