#!/bin/sh
# usage: ./run5.sh [repeats]   (default 5)
cd "$(dirname "$0")"
nohup python3 -u exp5.py "${1:-5}" > exp5.log 2>&1 &
echo "started exp5.py (pid $!), logging to exp5.log"
