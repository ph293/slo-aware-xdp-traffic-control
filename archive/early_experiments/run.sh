#!/bin/sh
# usage: ./run.sh [repeats]   (default 5)
cd "$(dirname "$0")"
nohup python3 -u exp4.py "${1:-5}" > exp4.log 2>&1 &
echo "started exp4.py (pid $!), logging to exp4.log"
